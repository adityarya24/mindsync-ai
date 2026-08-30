"""Shared helpers for native usage readers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mindsync.dispatch.usage.types import UsageWindow

_MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0


class AuthRequired(Exception):
    """Provider rejected the stored credential."""


class ProviderFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def coerce_percent(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number < 0.0 or number > 100.0:
        return None
    return number


def remaining_to_used(fraction: Any) -> float | None:
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        return None
    if fraction < 0.0 or fraction > 1.0:
        return None
    return coerce_percent((1.0 - float(fraction)) * 100.0)


def parse_reset_at(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1e12:
            timestamp = timestamp / 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return parse_reset_at(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def session_weekly_windows(
    *,
    session_percent: float | None,
    session_reset: datetime | None,
    weekly_percent: float | None,
    weekly_reset: datetime | None,
) -> list[UsageWindow]:
    primary = None
    weekly = None
    if session_percent is not None:
        primary = UsageWindow(
            id="primary",
            label="Primary",
            used_percent=session_percent,
            reset_at=session_reset,
        )
    if weekly_percent is not None:
        weekly = UsageWindow(
            id="weekly",
            label="Weekly",
            used_percent=weekly_percent,
            reset_at=weekly_reset,
        )
    if primary is None and weekly is not None:
        return [weekly.model_copy(update={"id": "primary", "label": "Primary"})]
    windows: list[UsageWindow] = []
    if primary is not None:
        windows.append(primary)
    if weekly is not None:
        windows.append(weekly)
    return windows


def http_json(
    url: str,
    headers: dict[str, str],
    *,
    method: str = "GET",
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise AuthRequired from exc
        raise ProviderFailure("provider request failed") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise ProviderFailure("provider request failed") from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ProviderFailure("provider response too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ProviderFailure("provider response malformed")
    return payload
