"""Job store under ~/.claude/agent-dispatch/jobs/ with PID reconciliation."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mindsync.dispatch.proc import is_alive, names_match, process_name
from mindsync.storage import atomic_private_write, file_lock

_JOB_ID_RE = re.compile(r"^[0-9a-z]+-[0-9a-f]+$", re.I)


def jobs_root() -> Path:
    env = os.environ.get("AGENT_DISPATCH_HOME")
    home = Path(env) if env else Path.home() / ".claude" / "agent-dispatch"
    return home / "jobs"


def _active_auto_root() -> Path:
    return jobs_root().parent / "active-auto-jobs"


def _register_active_auto_job(job_id: str) -> None:
    root = _active_auto_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    atomic_private_write(root / job_id, job_id + "\n")


def _initialize_active_auto_index() -> None:
    root = _active_auto_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    marker = root / ".initialized"
    if marker.is_file():
        return
    for job in list_jobs():
        if job.get("routing") and job.get("status") in {"pending", "running"}:
            _register_active_auto_job(str(job["id"]))
    atomic_private_write(marker, "1\n")


def count_active_auto_jobs() -> int:
    """Count routed pending/running jobs without rescanning completed history."""
    _initialize_active_auto_index()
    active = 0
    for slot in _active_auto_root().iterdir():
        if slot.name.startswith("."):
            continue
        try:
            job_paths(slot.name)
        except ValueError:
            slot.unlink(missing_ok=True)
            continue
        meta = get_job(slot.name)
        if meta is not None:
            meta = reconcile_job(meta)
        if meta and meta.get("routing") and meta.get("status") in {"pending", "running"}:
            active += 1
        else:
            slot.unlink(missing_ok=True)
    return active


def job_paths(job_id: str) -> dict[str, Path]:
    if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
        raise ValueError(f"Invalid job id: {job_id}")
    directory = jobs_root() / job_id
    return {
        "dir": directory,
        "meta": directory / "meta.json",
        "prompt": directory / "prompt.txt",
        "stdout": directory / "stdout.log",
        "stderr": directory / "stderr.log",
        "result": directory / "result.md",
        "supervisorLog": directory / "supervisor.log",
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _private_write(path: Path, text: str) -> None:
    """Atomically replace a private dispatch file in its existing directory."""
    atomic_private_write(path, text)


def _write_meta(job_id: str, meta: dict[str, Any]) -> None:
    _private_write(job_paths(job_id)["meta"], json.dumps(meta, indent=2))


def write_job_file(job_id: str, name: str, text: str) -> None:
    paths = job_paths(job_id)
    if name not in {"stdout", "stderr", "result", "supervisorLog"}:
        raise ValueError(f"Invalid job file: {name}")
    _private_write(paths[name], text)


def create_job(
    *,
    agent: str,
    prompt: str,
    cwd: str,
    model: str | None = None,
    effort: str | None = None,
    effective_effort: str | None = None,
    role: str | None = None,
    write: bool = False,
    checks: list[str] | None = None,
    publisher_agent: str = "dispatch",
    routing: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    root = jobs_root()
    root.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] | None = None
    for attempt in range(5):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        job_id = f"{stamp}-{secrets.token_hex(3)}"
        paths = job_paths(job_id)
        try:
            paths["dir"].mkdir(exist_ok=False)
            try:
                paths["dir"].chmod(0o700)
            except OSError:
                pass
        except FileExistsError:
            if attempt < 4:
                continue
            raise
        meta = {
            "id": job_id,
            "agent": agent,
            "role": role,
            "prompt": prompt,
            "cwd": cwd,
            "model": model,
            "effort": effort,
            "effectiveEffort": effective_effort,
            "warnings": warnings or [],
            "write": write,
            "checks": checks or [],
            "checkResults": [],
            "diff": None,
            "status": "pending",
            "pid": None,
            "spawnedName": None,
            "startedAt": utc_now(),
            "endedAt": None,
            "exitCode": None,
            "timedOut": False,
            "repoRoot": None,
            "worktreePath": None,
            "branch": None,
            "baseCommit": None,
            "worktreeKept": None,
            "publisherAgent": publisher_agent,
            "routing": routing,
        }
        if routing:
            _register_active_auto_job(job_id)
        _write_meta(job_id, meta)
        _private_write(paths["prompt"], prompt)
        return meta
    raise RuntimeError("Failed to allocate job id")


def get_job(job_id: str) -> dict[str, Any] | None:
    try:
        meta_path = job_paths(job_id)["meta"]
        if not meta_path.is_file():
            return None
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def update_job(
    job_id: str,
    patch: dict[str, Any],
    *,
    expected_status: str | set[str] | None = None,
) -> dict[str, Any]:
    """Atomically merge a job patch, optionally as a status transition.

    ``expected_status`` makes lifecycle races deterministic: cancellation and
    completion can both attempt a transition, but only the one that still sees
    the expected current state is applied.
    """
    job_paths(job_id)  # validate before using the id in a lock name
    with file_lock(f"dispatch-job-{job_id}"):
        existing = get_job(job_id)
        if existing is None:
            raise ValueError(f"No such job: {job_id}")
        if expected_status is not None:
            allowed = {expected_status} if isinstance(expected_status, str) else expected_status
            if existing.get("status") not in allowed:
                return existing
        meta = {**existing, **patch}
        _write_meta(job_id, meta)
        return meta


def reconcile_job(meta: dict[str, Any]) -> dict[str, Any]:
    """If a running job's PID is dead or renamed, mark it failed (with re-read)."""
    if meta.get("status") != "running":
        return meta
    pid = meta.get("pid")
    alive = (
        pid is not None
        and is_alive(int(pid))
        and names_match(process_name(int(pid)), meta.get("spawnedName"))
    )
    if alive:
        return meta
    fresh = get_job(str(meta["id"]))
    if fresh is None or fresh.get("status") != "running":
        return fresh if fresh is not None else meta
    return update_job(
        str(meta["id"]),
        {"status": "failed", "endedAt": utc_now()},
        expected_status="running",
    )


def list_jobs() -> list[dict[str, Any]]:
    root = jobs_root()
    if not root.is_dir():
        return []
    jobs: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        meta = get_job(entry.name)
        if not meta:
            continue
        try:
            jobs.append(reconcile_job(meta))
        except Exception:
            continue
    jobs.sort(key=lambda m: m.get("startedAt") or "", reverse=True)
    return jobs
