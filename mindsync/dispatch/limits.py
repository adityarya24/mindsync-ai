"""Narrow reactive quota classification and provider-account cooldowns.

When a provider CLI does not expose an authoritative reset timestamp, adapters fall
back to ``quotaCooldownSeconds`` as an operator-set estimate — not provider truth.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mindsync.config import dispatch_home
from mindsync.dispatch.adapters import AdapterConfig
from mindsync.storage import atomic_private_write, file_lock

_MAX_STDERR_TAIL = 16_000
_MAX_RESET_HORIZON = timedelta(days=7)
_CLAUDE_RESET_PATTERN = r"(?im)^Claude AI usage limit reached\|[0-9]{10}\s*$"
_CLAUDE_RESET_LINE = re.compile(
    r"(?im)^Claude AI usage limit reached\|([0-9]{10})\s*$"
)


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


def _write(data: dict[str, dict[str, Any]]) -> None:
    path = _cooldown_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_private_write(path, json.dumps(data, indent=2))


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
    # Provider failures belong on stderr. Agent-authored stdout may quote an
    # error while reviewing logs or tests; treating that as account state would
    # rotate a healthy job and cool the wrong provider account.
    text = stderr[-16_000:]
    for pattern in adapter.quotaErrorPatterns:
        if re.search(pattern, text):
            return {
                "kind": "quota_exhausted",
                "scope": quota_scope(adapter),
                "pattern": pattern,
            }
    return None


def extract_reactive_reset_at(
    adapter: AdapterConfig, *, stderr: str = ""
) -> datetime | None:
    """Parse an authoritative provider reset timestamp from bounded stderr only."""
    if not adapter.quotaErrorPatterns:
        return None
    text = stderr[-_MAX_STDERR_TAIL:]
    if not text.strip():
        return None

    for pattern in adapter.quotaErrorPatterns:
        if not re.search(pattern, text):
            continue
        reset_at = _parse_allowlisted_reset(text, pattern)
        if reset_at is not None:
            return reset_at
    return None


def _parse_claude_epoch_seconds(raw: str) -> int | None:
    if len(raw) != 10 or not raw.isdigit():
        return None
    timestamp = int(raw)
    # Reject millisecond-shaped values and other non-second epochs early.
    if timestamp < 1_000_000_000 or timestamp >= 10_000_000_000:
        return None
    return timestamp


def _parse_allowlisted_reset(text: str, matched_pattern: str) -> datetime | None:
    if matched_pattern != _CLAUDE_RESET_PATTERN:
        return None
    match = _CLAUDE_RESET_LINE.search(text)
    if match is None:
        return None
    timestamp = _parse_claude_epoch_seconds(match.group(1))
    if timestamp is None:
        return None
    try:
        reset_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return _validate_future_reset(reset_at)


def _validate_future_reset(value: datetime) -> datetime | None:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if value <= now:
        return None
    if value - now > _MAX_RESET_HORIZON:
        return None
    return value


def list_cooldowns() -> list[dict[str, str]]:
    now = datetime.now(timezone.utc)
    active: list[dict[str, str]] = []
    with file_lock("dispatch-quota-cooldowns"):
        data = _read()
        changed = False
        for scope, raw in list(data.items()):
            until = _parse_time(raw.get("until")) if isinstance(raw, dict) else None
            if until is None or until <= now:
                data.pop(scope, None)
                changed = True
                continue
            active.append(
                {
                    "scope": scope,
                    "until": until.isoformat(),
                    "reason": str(raw.get("reason") or "provider quota exhausted"),
                }
            )
        if changed:
            _write(data)
    return sorted(active, key=lambda item: item["scope"])


def clear_cooldowns(scope: str | None = None) -> int:
    """Clear one explicit account cooldown, or all when scope is omitted."""
    with file_lock("dispatch-quota-cooldowns"):
        data = _read()
        if scope is None:
            count = len(data)
            data = {}
        else:
            count = int(scope in data)
            data.pop(scope, None)
        _write(data)
    return count


def mark_cooling(adapter: AdapterConfig) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=adapter.quotaCooldownSeconds)
    return mark_cooling_until(adapter, until, reason="provider quota exhausted")


def mark_cooling_until(
    adapter: AdapterConfig,
    until: datetime,
    *,
    reason: str = "usage threshold reached",
) -> dict[str, str]:
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    else:
        until = until.astimezone(timezone.utc)
    scope = quota_scope(adapter)
    entry = {
        "scope": scope,
        "until": until.isoformat(),
        "reason": reason,
    }
    with file_lock("dispatch-quota-cooldowns"):
        data = _read()
        data[scope] = entry
        _write(data)
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
                _write(data)
            return None
    return f"provider account cooling until {until.isoformat()}"
