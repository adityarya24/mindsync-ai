"""Cursor account usage reader from the local IDE/CLI session."""

from __future__ import annotations

import json
import os
import sqlite3
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

READER_NAME = "cursor-oauth"
PROVIDER = "cursor"
USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
FetchFn = Callable[..., dict[str, Any]]
TokenLoader = Callable[[], str | None]


class CursorOAuthUsageReader:
    """Read Cursor plan usage from the local session token."""

    def __init__(
        self,
        *,
        cursor_db: Path | None = None,
        account_scope: str | None = None,
        fetch_fn: FetchFn | None = None,
        token_loader: TokenLoader | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self._cursor_db = cursor_db
        self._account_scope = account_scope
        self._fetch_fn = fetch_fn
        self._token_loader = token_loader
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def reader(self) -> str:
        return READER_NAME

    def read(self) -> UsageReadResult:
        try:
            token = self._token()
            if not token:
                db_path = self._db_path()
                missing = self._token_loader is None and not db_path.is_file()
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="cursor auth source missing" if missing else "cursor credentials unavailable",
                    reader=READER_NAME,
                )
            payload = self._fetch(token)
            plan = payload.get("planUsage")
            if not isinstance(plan, dict):
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="cursor usage data malformed",
                    reader=READER_NAME,
                )
            auto = coerce_percent(plan.get("autoPercentUsed"))
            total = coerce_percent(plan.get("totalPercentUsed"))
            reset = parse_reset_at(payload.get("billingCycleEnd"))
            windows = session_weekly_windows(
                session_percent=auto,
                session_reset=reset,
                weekly_percent=total,
                weekly_reset=reset,
            )
            if not windows:
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="cursor usage data malformed",
                    reader=READER_NAME,
                )
            return UsageReadResult.available(
                provider=PROVIDER,
                account_scope=self._scope(),
                reader=READER_NAME,
                source="cursor-oauth-period-usage",
                windows=windows,
            )
        except AuthRequired:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="cursor credentials unauthenticated",
                reader=READER_NAME,
            )
        except ProviderFailure:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="cursor provider request failed",
                reader=READER_NAME,
            )
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError, sqlite3.Error):
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="cursor usage data malformed",
                reader=READER_NAME,
            )

    def _scope(self) -> str:
        return self._account_scope or "cursor:default"

    def _db_path(self) -> Path | None:
        if self._cursor_db is not None:
            return self._cursor_db.expanduser()
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"

    def _token(self) -> str | None:
        if self._token_loader is not None:
            loaded = self._token_loader()
            return loaded.strip() if isinstance(loaded, str) and loaded.strip() else None
        db_path = self._db_path()
        if db_path is None or not db_path.is_file():
            return None
        uri = db_path.resolve().as_posix()
        con = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key=?",
                ("cursorAuth/accessToken",),
            ).fetchone()
        finally:
            con.close()
        if not row or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        text = str(value).strip()
        return text or None

    def _fetch(self, token: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connect-Protocol-Version": "1",
            "User-Agent": "Mozilla/5.0",
        }
        if self._fetch_fn is not None:
            try:
                return self._fetch_fn(USAGE_URL, headers, method="POST", body=b"{}")
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AuthRequired from exc
                raise ProviderFailure("cursor provider request failed") from exc
        timeout = 30.0 if self._request_timeout_seconds is None else max(0.05, min(float(self._request_timeout_seconds), 30.0))
        return http_json(USAGE_URL, headers, method="POST", body=b"{}", timeout=timeout)
