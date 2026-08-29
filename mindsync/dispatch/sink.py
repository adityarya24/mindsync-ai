"""Opt-in argv completion sink for persisted job.completed / job.failed events."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from mindsync.bus.models import Event, EventType
from mindsync.dispatch.publish import public_task
from mindsync.orchestration import load_policy
from mindsync.storage import atomic_private_write, file_lock

_DELIVERED_NAME = "completion-sink-delivered.json"
_OUTBOX_NAME = "completion-sink-outbox.json"
_MAX_DELIVERED = 200
_MAX_OUTBOX = 200
_SINK_TIMEOUT_SECONDS = 5.0
_DRAIN_BUDGET_SECONDS = 5.0
_MAX_DRAIN_ATTEMPTS = 8
_MAX_SUMMARY_CHARS = 240
_COMPLETION_TYPES = {EventType.JOB_COMPLETED.value, EventType.JOB_FAILED.value}
_PROJECTION_KEYS = (
    "event_id",
    "job_id",
    "status",
    "summary",
    "public_task_summary",
    "pr_url",
)


def _delivered_path() -> Path:
    from mindsync.config import settings

    return settings.home / _DELIVERED_NAME


def _outbox_path() -> Path:
    from mindsync.config import settings

    return settings.home / _OUTBOX_NAME


def _load_delivered() -> list[str]:
    path = _delivered_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str) and item and item not in ids:
            ids.append(item)
    return ids[-_MAX_DELIVERED:]


def _save_delivered(ids: list[str]) -> None:
    from mindsync.config import settings

    settings.ensure_dirs()
    atomic_private_write(
        _delivered_path(),
        json.dumps(ids[-_MAX_DELIVERED:], indent=2) + "\n",
    )


def _normalize_projection(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return None
    projection: dict[str, str] = {}
    for key in _PROJECTION_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value:
            projection[key] = value
    if "event_id" not in projection:
        return None
    return projection


def _load_outbox() -> list[dict[str, str]]:
    path = _outbox_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        projection = _normalize_projection(item)
        if projection is None:
            continue
        event_id = projection["event_id"]
        if event_id in seen:
            continue
        seen.add(event_id)
        items.append(projection)
    return items[-_MAX_OUTBOX:]


def _save_outbox(items: list[dict[str, str]]) -> None:
    from mindsync.config import settings

    settings.ensure_dirs()
    atomic_private_write(
        _outbox_path(),
        json.dumps(items[-_MAX_OUTBOX:], indent=2, ensure_ascii=True) + "\n",
    )


def _sink_cmd() -> list[str] | None:
    try:
        cmd = load_policy().completionSinkCmd
    except ValueError:
        return None
    if not cmd:
        return None
    return cmd


def _bounded(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:limit]


def public_completion_projection(event: Event, meta: dict[str, Any] | None) -> dict[str, str]:
    """Allowlisted public fields only — never prompt, logs, or credentials."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    job_id = _bounded(payload.get("job_id") or (meta or {}).get("id"), 80) or "unknown"
    status = _bounded(payload.get("status") or (meta or {}).get("status"), 32) or "unknown"
    if status == "done":
        summary = f"Job {job_id} completed."
    else:
        summary = f"Job {job_id} ended ({status})."
    projection: dict[str, str] = {
        "event_id": event.event_id,
        "job_id": job_id,
        "status": status,
        "summary": summary[:_MAX_SUMMARY_CHARS],
    }
    if not meta:
        return projection
    task = public_task(str(meta.get("taskPrompt") or ""))
    if task:
        projection["public_task_summary"] = task[:_MAX_SUMMARY_CHARS]
    pr = meta.get("pullRequest")
    if isinstance(pr, dict):
        url = _bounded(pr.get("url"), 500)
        if url and url.startswith(("https://", "http://")):
            projection["pr_url"] = url
    return projection


def _try_send(cmd: list[str], projection: dict[str, str], *, timeout: float) -> bool:
    if timeout <= 0:
        return False
    encoded = json.dumps(projection, ensure_ascii=True) + "\n"
    try:
        subprocess.run(
            cmd,
            input=encoded.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=True,
            shell=False,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError, ValueError):
        return False
    return True


def _drain_locked(cmd: list[str]) -> None:
    delivered = _load_delivered()
    pending = [
        projection
        for projection in _load_outbox()
        if projection["event_id"] not in delivered
    ]
    remaining: list[dict[str, str]] = []
    deadline = time.monotonic() + _DRAIN_BUDGET_SECONDS
    attempts = 0
    for index, projection in enumerate(pending):
        leftover = deadline - time.monotonic()
        if attempts >= _MAX_DRAIN_ATTEMPTS or leftover <= 0:
            remaining.extend(pending[index:])
            break
        timeout = min(_SINK_TIMEOUT_SECONDS, leftover)
        attempts += 1
        if _try_send(cmd, projection, timeout=timeout):
            delivered.append(projection["event_id"])
            continue
        remaining.extend(pending[index:])
        break
    _save_outbox(remaining)
    if delivered:
        _save_delivered(delivered)


def _enqueue_locked(projection: dict[str, str]) -> None:
    event_id = projection.get("event_id")
    if not event_id:
        return
    delivered = _load_delivered()
    if event_id in delivered:
        return
    pending = _load_outbox()
    if any(item.get("event_id") == event_id for item in pending):
        return
    pending.append(projection)
    _save_outbox(pending)


def drain_completion_outbox() -> None:
    """Retry pending allowlisted projections. Never raises."""
    cmd = _sink_cmd()
    if not cmd:
        return
    try:
        with file_lock("completion-sink"):
            _drain_locked(cmd)
    except Exception:
        return


def deliver_completion_event(event: Event) -> None:
    """Persist-before-send, then drain. Never raises. Never changes job status."""
    event_type = str(event.event_type)
    if event_type not in _COMPLETION_TYPES:
        drain_completion_outbox()
        return
    cmd = _sink_cmd()
    if not cmd:
        return
    event_id = event.event_id
    if not event_id:
        return

    payload = event.payload if isinstance(event.payload, dict) else {}
    job_id = payload.get("job_id")
    meta: dict[str, Any] | None = None
    if isinstance(job_id, str) and job_id:
        try:
            from mindsync.dispatch import store

            meta = store.get_job(job_id)
        except Exception:
            meta = None

    projection = public_completion_projection(event, meta)
    try:
        with file_lock("completion-sink"):
            _enqueue_locked(projection)
            _drain_locked(cmd)
    except Exception:
        return
