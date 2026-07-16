"""Local JSON/JSONL storage with exclusive file locks."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterable

from mindsync.config import settings


@contextmanager
def file_lock(name: str, timeout: float | None = None) -> Generator[None, None, None]:
    """Cross-platform exclusive lock via O_EXCL lockfiles."""
    settings.ensure_dirs()
    timeout = settings.lock_timeout_seconds if timeout is None else timeout
    lock_path = settings.lock_dir / f"{name}.lock"
    deadline = time.time() + timeout
    fd: int | None = None

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            break
        except FileExistsError:
            if time.time() >= deadline:
                # Stale lock recovery: if lock is older than 60s, steal it.
                try:
                    age = time.time() - lock_path.stat().st_mtime
                    if age > 60:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                raise TimeoutError(f"Could not acquire lock: {lock_path}") from None
            time.sleep(0.05)

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _default_state() -> dict[str, Any]:
    return {
        "active_project": "unknown",
        "active_branch": "unknown",
        "last_updated_by": None,
        "timestamp": _utc_now(),
        "custom_names": {},
        "agents_focus": {},
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict[str, Any]:
    settings.ensure_dirs()
    path = settings.state_file
    if not path.exists():
        state = _default_state()
        save_state(state)
        return state
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        data.setdefault("agents_focus", {})
        data.setdefault("custom_names", {})
        return data
    except (OSError, json.JSONDecodeError):
        return _default_state()


def save_state(state: dict[str, Any]) -> None:
    settings.ensure_dirs()
    path = settings.state_file
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def locked_state() -> Generator[dict[str, Any], None, None]:
    """Load-modify-save under exclusive lock."""
    with file_lock("state"):
        state = load_state()
        yield state
        save_state(state)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    settings.ensure_dirs()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def log_audit(agent_name: str, action: str, details: str) -> None:
    append_jsonl(
        settings.audit_file,
        {
            "timestamp": _utc_now(),
            "agent": agent_name,
            "action": action,
            "details": details,
        },
    )


def enqueue_fact(fact: dict[str, Any]) -> None:
    with file_lock("queue"):
        append_jsonl(settings.offline_queue_file, fact)


def read_queue() -> list[dict[str, Any]]:
    path = settings.offline_queue_file
    if not path.exists() or path.stat().st_size == 0:
        return []
    facts: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                facts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return facts


def rewrite_queue(facts: Iterable[dict[str, Any]]) -> None:
    settings.ensure_dirs()
    path = settings.offline_queue_file
    tmp = path.with_suffix(".jsonl.tmp")
    with file_lock("queue"):
        with open(tmp, "w", encoding="utf-8") as fh:
            for fact in facts:
                fh.write(json.dumps(fact, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def read_compiled_truth() -> dict[str, str]:
    settings.ensure_dirs()
    out: dict[str, str] = {}
    for path in sorted(settings.compiled_truth_dir.glob("*.md")):
        try:
            out[path.stem] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return out
