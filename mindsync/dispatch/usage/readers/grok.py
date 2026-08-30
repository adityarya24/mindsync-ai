"""Grok CLI OAuth billing reader."""

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

READER_NAME = "grok-oauth"
PROVIDER = "grok"
USAGE_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
FetchFn = Callable[..., dict[str, Any]]


class GrokOAuthUsageReader:
    """Read Grok weekly credit usage from the local CLI session."""

    def __init__(
        self,
        *,
        grok_home: Path | None = None,
        account_scope: str | None = None,
        fetch_fn: FetchFn | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self._grok_home = grok_home
        self._account_scope = account_scope
        self._fetch_fn = fetch_fn
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def reader(self) -> str:
        return READER_NAME

    def read(self) -> UsageReadResult:
        try:
            session = self._load_session()
            if session is None:
                reason = (
                    "grok auth source missing"
                    if not (self._resolve_home() / "auth.json").is_file()
                    else "grok credentials unavailable"
                )
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason=reason,
                    reader=READER_NAME,
                )
            payload = self._fetch(session)
            config = payload.get("config") if isinstance(payload, dict) else None
            if not isinstance(config, dict):
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="grok usage data malformed",
                    reader=READER_NAME,
                )
            weekly = coerce_percent(config.get("creditUsagePercent"))
            session_used = None
            products = config.get("productUsage")
            if isinstance(products, list):
                for product in products:
                    if not isinstance(product, dict):
                        continue
                    session_used = coerce_percent(
                        product.get("usagePercent", product.get("creditUsagePercent"))
                    )
                    if session_used is not None:
                        break
            period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
            weekly_reset = parse_reset_at(period.get("end") or config.get("billingPeriodEnd"))
            windows = session_weekly_windows(
                session_percent=session_used,
                session_reset=None,
                weekly_percent=weekly,
                weekly_reset=weekly_reset,
            )
            if not windows:
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="grok usage data malformed",
                    reader=READER_NAME,
                )
            return UsageReadResult.available(
                provider=PROVIDER,
                account_scope=self._scope(),
                reader=READER_NAME,
                source="grok-oauth-billing",
                windows=windows,
            )
        except AuthRequired:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="grok credentials unauthenticated",
                reader=READER_NAME,
            )
        except ProviderFailure:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="grok provider request failed",
                reader=READER_NAME,
            )
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="grok usage data malformed",
                reader=READER_NAME,
            )

    def _resolve_home(self) -> Path:
        if self._grok_home is not None:
            return self._grok_home.expanduser()
        override = os.environ.get("GROK_HOME") or os.environ.get("XAI_HOME") or ""
        if override.strip():
            return Path(override).expanduser()
        return Path.home() / ".grok"

    def _scope(self) -> str:
        return self._account_scope or "xai:default"

    def _load_session(self) -> dict[str, str] | None:
        auth_path = self._resolve_home() / "auth.json"
        if not auth_path.is_file():
            return None
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        for entry in raw.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("auth_mode") in {"api_key", "web_login"}:
                continue
            token = entry.get("key") or entry.get("access_token")
            if isinstance(token, str) and token.strip():
                user_id = entry.get("user_id")
                return {
                    "token": token.strip(),
                    "user_id": user_id.strip() if isinstance(user_id, str) else "",
                }
        return None

    def _fetch(self, session: dict[str, str]) -> dict[str, Any]:
        version_path = self._resolve_home() / ".metadata_version"
        version = "1.0.13"
        if version_path.is_file():
            text = version_path.read_text(encoding="utf-8").strip()
            if text:
                version = text
        headers = {
            "Authorization": f"Bearer {session['token']}",
            "Accept": "application/json",
            "User-Agent": f"grok-cli/{version}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-grok-client-version": version,
            "x-grok-client-mode": "interactive",
        }
        if session["user_id"]:
            headers["x-userid"] = session["user_id"]
        if self._fetch_fn is not None:
            try:
                return self._fetch_fn(USAGE_URL, headers)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AuthRequired from exc
                raise ProviderFailure("grok provider request failed") from exc
        timeout = 30.0 if self._request_timeout_seconds is None else max(0.05, min(float(self._request_timeout_seconds), 30.0))
        return http_json(USAGE_URL, headers, timeout=timeout)
