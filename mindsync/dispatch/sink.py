"""Opt-in argv completion sink for persisted job.completed / job.failed events."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from mindsync.bus.models import Event, EventType
from mindsync.dispatch.publish import public_task
from mindsync.orchestration import load_policy
from mindsync.storage import atomic_private_write, file_lock

_DELIVERED_NAME = "completion-sink-delivered.json"
_MAX_DELIVERED = 200
_SINK_TIMEOUT_SECONDS = 5.0
_MAX_SUMMARY_CHARS = 240
_COMPLETION_TYPES = {EventType.JOB_COMPLETED.value, EventType.JOB_FAILED.value}


def _delivered_path() -> Path:
    from mindsync.config import settings

    return settings.home / _DELIVERED_NAME


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


def deliver_completion_event(event: Event) -> None:
    """Best-effort sink after the event is already persisted. Never raises."""
    event_type = str(event.event_type)
    if event_type not in _COMPLETION_TYPES:
        return
    try:
        cmd = load_policy().completionSinkCmd
    except ValueError:
        return
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
    encoded = json.dumps(projection, ensure_ascii=True) + "\n"

    with file_lock("completion-sink"):
        delivered = _load_delivered()
        if event_id in delivered:
            return
        try:
            subprocess.run(
                cmd,
                input=encoded.encode("utf-8"),
                capture_output=True,
                timeout=_SINK_TIMEOUT_SECONDS,
                check=True,
                shell=False,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError, ValueError):
            return
        delivered.append(event_id)
        _save_delivered(delivered)
