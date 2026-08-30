"""Claude Code OAuth usage reader."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode

from mindsync.dispatch.usage.common import (
    AuthRequired,
    ProviderFailure,
    coerce_percent,
    http_json,
    parse_reset_at,
    session_weekly_windows,
)
from mindsync.dispatch.usage.types import UsageReadResult
from mindsync.storage import atomic_private_write

READER_NAME = "claude-oauth"
PROVIDER = "claude"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
FetchFn = Callable[..., dict[str, Any]]


class ClaudeOAuthUsageReader:
    """Read Claude session and weekly usage from local CLI OAuth."""

    def __init__(
        self,
        *,
        claude_home: Path | None = None,
        account_scope: str | None = None,
        fetch_fn: FetchFn | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self._claude_home = claude_home
        self._account_scope = account_scope
        self._fetch_fn = fetch_fn
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def reader(self) -> str:
        return READER_NAME

    def read(self) -> UsageReadResult:
        try:
            cred_path = self._resolve_home() / ".credentials.json"
            if not cred_path.is_file():
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="claude auth source missing",
                    reader=READER_NAME,
                )
            packed = json.loads(cred_path.read_text(encoding="utf-8"))
            oauth = packed.get("claudeAiOauth") if isinstance(packed, dict) else None
            if not isinstance(oauth, dict):
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="claude credentials unavailable",
                    reader=READER_NAME,
                )
            access = oauth.get("accessToken")
            refresh = oauth.get("refreshToken")
            if not isinstance(access, str) and not isinstance(refresh, str):
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="claude credentials unavailable",
                    reader=READER_NAME,
                )
            if not isinstance(access, str) or self._expired(oauth.get("expiresAt")):
                access = self._refresh(cred_path, packed, oauth)
            payload = self._fetch(USAGE_URL, self._usage_headers(access))
            windows = self._parse_windows(payload)
            if not windows:
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="claude usage data malformed",
                    reader=READER_NAME,
                )
            return UsageReadResult.available(
                provider=PROVIDER,
                account_scope=self._scope(),
                reader=READER_NAME,
                source="claude-oauth-usage",
                windows=windows,
            )
        except AuthRequired:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="claude credentials unauthenticated",
                reader=READER_NAME,
            )
        except ProviderFailure as exc:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason=exc.reason.replace("provider", "claude provider", 1)
                if exc.reason.startswith("provider")
                else "claude provider request failed",
                reader=READER_NAME,
            )
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="claude usage data malformed",
                reader=READER_NAME,
            )

    def _resolve_home(self) -> Path:
        if self._claude_home is not None:
            return self._claude_home.expanduser()
        override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        if override:
            return Path(override).expanduser()
        return Path.home() / ".claude"

    def _scope(self) -> str:
        return self._account_scope or "anthropic:default"

    def _expired(self, expires_at: Any, skew_seconds: int = 60) -> bool:
        if not isinstance(expires_at, (int, float)):
            return False
        value = float(expires_at)
        if value > 1e12:
            value = value / 1000.0
        return value <= datetime.now(timezone.utc).timestamp() + skew_seconds

    def _usage_headers(self, access: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "x-app": "cli",
            "User-Agent": "claude-cli/2.1.246 (external, cli)",
        }

    def _refresh(self, cred_path: Path, packed: dict[str, Any], oauth: dict[str, Any]) -> str:
        refresh = oauth.get("refreshToken")
        if not isinstance(refresh, str) or not refresh.strip():
            raise AuthRequired
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": CLIENT_ID,
            }
        ).encode()
        payload = self._fetch(
            TOKEN_URL,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "claude-cli/2.1.246 (external, cli)",
            },
            method="POST",
            body=body,
        )
        access = payload.get("access_token")
        if not isinstance(access, str) or not access.strip():
            raise AuthRequired
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        expires_in = payload.get("expires_in") if isinstance(payload.get("expires_in"), int) else 28800
        oauth["accessToken"] = access
        if isinstance(payload.get("refresh_token"), str):
            oauth["refreshToken"] = payload["refresh_token"]
        oauth["expiresAt"] = now_ms + int(expires_in) * 1000
        packed["claudeAiOauth"] = oauth
        atomic_private_write(cred_path, json.dumps(packed, indent=2) + "\n")
        return access

    def _fetch(
        self,
        url: str,
        headers: dict[str, str],
        *,
        method: str = "GET",
        body: bytes | None = None,
    ) -> dict[str, Any]:
        if self._fetch_fn is not None:
            try:
                try:
                    return self._fetch_fn(url, headers, method=method, body=body)
                except TypeError:
                    return self._fetch_fn(url, headers)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AuthRequired from exc
                raise ProviderFailure("claude provider request failed") from exc
        timeout = self._request_timeout_seconds
        return http_json(
            url,
            headers,
            method=method,
            body=body,
            timeout=30.0 if timeout is None else max(0.05, min(float(timeout), 30.0)),
        )

    def _parse_windows(self, payload: dict[str, Any]) -> list:
        session = weekly = None
        session_reset = weekly_reset = None
        limits = payload.get("limits")
        if isinstance(limits, list):
            for item in limits:
                if not isinstance(item, dict):
                    continue
                percent = coerce_percent(item.get("percent", item.get("utilization")))
                if percent is None:
                    continue
                if item.get("kind") == "session":
                    session = percent
                    session_reset = parse_reset_at(item.get("resets_at"))
                elif item.get("kind") == "weekly_all":
                    weekly = percent
                    weekly_reset = parse_reset_at(item.get("resets_at"))
        five = payload.get("five_hour")
        if session is None and isinstance(five, dict):
            session = coerce_percent(five.get("utilization", five.get("percent")))
            session_reset = parse_reset_at(five.get("resets_at"))
        seven = payload.get("seven_day")
        if weekly is None and isinstance(seven, dict):
            weekly = coerce_percent(seven.get("utilization", seven.get("percent")))
            weekly_reset = parse_reset_at(seven.get("resets_at"))
        return session_weekly_windows(
            session_percent=session,
            session_reset=session_reset,
            weekly_percent=weekly,
            weekly_reset=weekly_reset,
        )
