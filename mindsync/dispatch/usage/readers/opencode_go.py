"""OpenCode Go subscription usage reader."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from mindsync.dispatch.usage.common import (
    AuthRequired,
    ProviderFailure,
    coerce_percent,
    http_json,
    parse_reset_at,
    session_weekly_windows,
)
from mindsync.dispatch.usage.types import UsageReadResult

READER_NAME = "opencode-go"
PROVIDER = "opencode"
USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
FetchFn = Callable[..., dict[str, Any]]


class OpenCodeGoUsageReader:
    """Read OpenCode Go rolling/weekly usage from the local Go API key."""

    def __init__(
        self,
        *,
        opencode_home: Path | None = None,
        account_scope: str | None = None,
        fetch_fn: FetchFn | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self._opencode_home = opencode_home
        self._account_scope = account_scope
        self._fetch_fn = fetch_fn
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def reader(self) -> str:
        return READER_NAME

    def read(self) -> UsageReadResult:
        try:
            key = self._load_key()
            if not key:
                auth_path = self._auth_path()
                reason = (
                    "opencode auth source missing"
                    if not auth_path.is_file()
                    else "opencode credentials unavailable"
                )
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason=reason,
                    reader=READER_NAME,
                )
            payload = self._fetch(key)
            usage = payload.get("usage") if isinstance(payload, dict) else None
            if not isinstance(usage, dict):
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="opencode usage data malformed",
                    reader=READER_NAME,
                )
            rolling = _window(usage.get("rolling") or usage.get("rollingUsage"))
            weekly = _window(usage.get("weekly") or usage.get("weeklyUsage"))
            monthly = _window(usage.get("monthly") or usage.get("monthlyUsage"))
            plan = monthly if monthly and (weekly is None or monthly[0] > weekly[0]) else weekly
            windows = session_weekly_windows(
                session_percent=None if rolling is None else rolling[0],
                session_reset=None if rolling is None else rolling[1],
                weekly_percent=None if plan is None else plan[0],
                weekly_reset=None if plan is None else plan[1],
            )
            if not windows:
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="opencode usage data malformed",
                    reader=READER_NAME,
                )
            return UsageReadResult.available(
                provider=PROVIDER,
                account_scope=self._scope(),
                reader=READER_NAME,
                source="opencode-go-usage",
                windows=windows,
            )
        except AuthRequired:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="opencode credentials unauthenticated",
                reader=READER_NAME,
            )
        except ProviderFailure:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="opencode provider request failed",
                reader=READER_NAME,
            )
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="opencode usage data malformed",
                reader=READER_NAME,
            )

    def _scope(self) -> str:
        return self._account_scope or "opencode-go:default"

    def _auth_path(self) -> Path:
        if self._opencode_home is not None:
            return self._opencode_home.expanduser() / "auth.json"
        override = os.environ.get("XDG_DATA_HOME", "").strip()
        root = Path(override).expanduser() if override else Path.home() / ".local" / "share"
        return root / "opencode" / "auth.json"

    def _load_key(self) -> str | None:
        path = self._auth_path()
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        for name in ("opencode-go", "opencode"):
            entry = raw.get(name)
            if isinstance(entry, dict):
                key = entry.get("key")
                if isinstance(key, str) and key.strip():
                    return key.strip()
        return None

    def _fetch(self, key: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenCode/1.0",
        }
        if self._fetch_fn is not None:
            try:
                return self._fetch_fn(USAGE_URL, headers)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AuthRequired from exc
                raise ProviderFailure("opencode provider request failed") from exc
        timeout = 30.0 if self._request_timeout_seconds is None else max(0.05, min(float(self._request_timeout_seconds), 30.0))
        return http_json(USAGE_URL, headers, timeout=timeout)


def _window(raw: Any) -> tuple[float, Any] | None:
    if not isinstance(raw, dict):
        return None
    percent = coerce_percent(raw.get("percent", raw.get("usagePercent")))
    if percent is None:
        return None
    return percent, parse_reset_at(raw.get("resetsAt") or raw.get("resetAt"))
