"""Narrow reactive quota classification and provider-account cooldowns."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mindsync.config import dispatch_home
from mindsync.dispatch.adapters import AdapterConfig
from mindsync.storage import atomic_private_write, file_lock


def _cooldown_path() -> Path:
    return dispatch_home() / "quota-cooldowns.json"


def _read() -> dict[str, dict[str, Any]]:
    path = _cooldown_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def quota_scope(adapter: AdapterConfig) -> str:
    """Return an explicit account scope, degrading to one roster entry."""
    return adapter.quotaScope or f"agent:{adapter.name}"


def classify_quota_exhaustion(
    adapter: AdapterConfig, *, stdout: str = "", stderr: str = ""
) -> dict[str, str] | None:
    """Match only adapter-owned quota signatures; generic failures never rotate."""
    if not adapter.quotaErrorPatterns:
        return None
    text = f"{stderr}\n{stdout}"[-16_000:]
    for pattern in adapter.quotaErrorPatterns:
        if re.search(pattern, text):
            return {
                "kind": "quota_exhausted",
                "scope": quota_scope(adapter),
                "pattern": pattern,
            }
    return None


def mark_cooling(adapter: AdapterConfig) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=adapter.quotaCooldownSeconds)
    scope = quota_scope(adapter)
    entry = {
        "scope": scope,
        "until": until.isoformat(),
        "reason": "provider quota exhausted",
    }
    with file_lock("dispatch-quota-cooldowns"):
        data = _read()
        data[scope] = entry
        atomic_private_write(_cooldown_path(), json.dumps(data, indent=2))
    return entry


def cooldown_reason(adapter: AdapterConfig) -> str | None:
    scope = quota_scope(adapter)
    now = datetime.now(timezone.utc)
    with file_lock("dispatch-quota-cooldowns"):
        data = _read()
        entry = data.get(scope)
        until = _parse_time(entry.get("until")) if isinstance(entry, dict) else None
        if until is None or until <= now:
            if scope in data:
                data.pop(scope, None)
                atomic_private_write(_cooldown_path(), json.dumps(data, indent=2))
            return None
    return f"provider account cooling until {until.isoformat()}"
