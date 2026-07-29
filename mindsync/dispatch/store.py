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

_JOB_ID_RE = re.compile(r"^[0-9a-z]+-[0-9a-f]+$", re.I)


def jobs_root() -> Path:
    env = os.environ.get("AGENT_DISPATCH_HOME")
    home = Path(env) if env else Path.home() / ".claude" / "agent-dispatch"
    return home / "jobs"


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


def create_job(
    *,
    agent: str,
    prompt: str,
    cwd: str,
    model: str | None = None,
    write: bool = False,
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
        except FileExistsError:
            if attempt < 4:
                continue
            raise
        meta = {
            "id": job_id,
            "agent": agent,
            "prompt": prompt,
            "cwd": cwd,
            "model": model,
            "write": write,
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
        }
        paths["meta"].write_text(json.dumps(meta, indent=2), encoding="utf-8")
        paths["prompt"].write_text(prompt, encoding="utf-8")
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


def update_job(job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    existing = get_job(job_id)
    if existing is None:
        raise ValueError(f"No such job: {job_id}")
    meta = {**existing, **patch}
    job_paths(job_id)["meta"].write_text(json.dumps(meta, indent=2), encoding="utf-8")
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
