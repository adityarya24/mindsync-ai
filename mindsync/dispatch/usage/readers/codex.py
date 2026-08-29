"""Native Codex OAuth usage reader."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

from mindsync.dispatch.usage.types import UsageReadResult, UsageWindow

READER_NAME = "codex-oauth"
PROVIDER = "codex"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
USAGE_PATH = "/wham/usage"
REQUEST_TIMEOUT_SECONDS = 30
_MAX_RESPONSE_BYTES = 1_000_000

FetchFn = Callable[[str, dict[str, str]], dict[str, Any]]


class CodexOAuthUsageReader:
    """Read Codex primary and weekly usage from the local OAuth source."""

    def __init__(
        self,
        *,
        codex_home: Path | None = None,
        account_scope: str | None = None,
        fetch_fn: FetchFn | None = None,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self._codex_home = codex_home
        self._account_scope = account_scope
        self._fetch_fn = fetch_fn
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def reader(self) -> str:
        return READER_NAME

    def read(self) -> UsageReadResult:
        try:
            codex_dir = self._resolve_codex_dir()
            auth_path = codex_dir / "auth.json"
            if not auth_path.is_file():
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._fallback_scope(),
                    reason="codex auth source missing",
                    reader=READER_NAME,
                )

            credentials = self._load_credentials(auth_path)
            if credentials is None:
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._fallback_scope(),
                    reason="codex credentials unavailable",
                    reader=READER_NAME,
                )

            account_scope = self._resolve_account_scope(credentials)
            base_url = self._resolve_base_url(codex_dir)
            payload = self._fetch_usage(
                base_url=base_url,
                access_token=credentials["access_token"],
                account_id=credentials.get("account_id"),
            )
            windows = self._parse_windows(payload)
            if not windows:
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=account_scope,
                    reason="codex usage data malformed",
                    reader=READER_NAME,
                )
            return UsageReadResult.available(
                provider=PROVIDER,
                account_scope=account_scope,
                reader=READER_NAME,
                source="codex-oauth-wham-usage",
                windows=windows,
            )
        except _AuthRequired:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._fallback_scope(),
                reason="codex credentials unauthenticated",
                reader=READER_NAME,
            )
        except _ProviderFailure as exc:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._fallback_scope(),
                reason=exc.reason,
                reader=READER_NAME,
            )
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._fallback_scope(),
                reason="codex usage data malformed",
                reader=READER_NAME,
            )

    def _resolve_codex_dir(self) -> Path:
        if self._codex_home is not None:
            return self._codex_home.expanduser()
        override = os.environ.get("CODEX_HOME", "").strip()
        if override:
            return Path(override).expanduser()
        return Path.home() / ".codex"

    def _fallback_scope(self) -> str:
        return self._account_scope or "openai:default"

    def _resolve_account_scope(self, credentials: dict[str, str | None]) -> str:
        if self._account_scope:
            return self._account_scope
        account_id = credentials.get("account_id")
        if account_id:
            return f"openai:{account_id}"
        return "openai:default"

    def _load_credentials(self, auth_path: Path) -> dict[str, str | None] | None:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None

        tokens = raw.get("tokens")
        if isinstance(tokens, dict):
            access_token = tokens.get("access_token")
            if isinstance(access_token, str) and access_token.strip():
                account_id = tokens.get("account_id")
                if isinstance(account_id, str) and not account_id.strip():
                    account_id = None
                return {
                    "access_token": access_token.strip(),
                    "account_id": account_id if isinstance(account_id, str) else None,
                }

        api_key = raw.get("OPENAI_API_KEY")
        if isinstance(api_key, str) and api_key.strip():
            return None

        return None

    def _resolve_base_url(self, codex_dir: Path) -> str:
        config_path = codex_dir / "config.toml"
        if not config_path.is_file():
            return DEFAULT_BASE_URL
        try:
            with config_path.open("rb") as handle:
                config = tomllib.load(handle)
        except (OSError, ValueError):
            return DEFAULT_BASE_URL
        if not isinstance(config, dict):
            return DEFAULT_BASE_URL
        raw = config.get("chatgpt_base_url")
        if not isinstance(raw, str) or not raw.strip():
            return DEFAULT_BASE_URL
        normalized = self._normalize_base_url(raw.strip())
        return normalized or DEFAULT_BASE_URL

    def _normalize_base_url(self, value: str) -> str | None:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"https", "http"}:
            return None
        host = parsed.hostname
        if host is None:
            return None
        host_lower = host.lower()
        if parsed.scheme == "https" and host_lower in {"chatgpt.com", "www.chatgpt.com"}:
            return normalized
        if parsed.scheme == "http" and host_lower in {"127.0.0.1", "localhost"}:
            return normalized
        return None

    def _fetch_usage(
        self,
        *,
        base_url: str,
        access_token: str,
        account_id: str | None,
    ) -> dict[str, Any]:
        url = f"{base_url.rstrip('/')}{USAGE_PATH}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "MindSync-usage-reader/1.0",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        if self._fetch_fn is not None:
            try:
                payload = self._fetch_fn(url, headers)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise _AuthRequired from exc
                raise _ProviderFailure("codex provider request failed") from exc
            except (TimeoutError, URLError, OSError) as exc:
                raise _ProviderFailure("codex provider request failed") from exc
            if not isinstance(payload, dict):
                raise _ProviderFailure("codex provider response malformed")
            return payload
        return self._http_get_json(url, headers)

    def _request_timeout(self) -> float:
        if self._request_timeout_seconds is None:
            return REQUEST_TIMEOUT_SECONDS
        return max(0.05, min(float(self._request_timeout_seconds), REQUEST_TIMEOUT_SECONDS))

    def _http_get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self._request_timeout()) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise _AuthRequired from exc
            raise _ProviderFailure("codex provider request failed") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise _ProviderFailure("codex provider request failed") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise _ProviderFailure("codex provider response too large")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise _ProviderFailure("codex provider response malformed")
        return payload

    def _parse_windows(self, payload: dict[str, Any]) -> list[UsageWindow]:
        rate_limit = payload.get("rate_limit")
        if not isinstance(rate_limit, dict):
            return []

        primary_raw = rate_limit.get("primary_window")
        secondary_raw = rate_limit.get("secondary_window")
        has_primary_key = "primary_window" in rate_limit

        primary = self._parse_window(primary_raw, window_id="primary", label="Primary")
        if has_primary_key and primary is None:
            return []

        secondary = self._parse_window(secondary_raw, window_id="weekly", label="Weekly")

        if primary is None and secondary is not None:
            primary = secondary.model_copy(update={"id": "primary", "label": "Primary"})
            secondary = None
        if primary is None:
            return []
        windows = [primary]
        if secondary is not None:
            windows.append(secondary)
        return windows

    def _parse_window(
        self,
        window: Any,
        *,
        window_id: str,
        label: str,
    ) -> UsageWindow | None:
        if not isinstance(window, dict):
            return None
        used = window.get("used_percent")
        if used is None:
            used = window.get("usage_percent")
        used_percent = self._coerce_percent(used)
        if used_percent is None:
            return None
        reset_at = self._parse_reset_at(window.get("reset_at"))
        return UsageWindow(
            id=window_id,
            label=label,
            used_percent=used_percent,
            reset_at=reset_at,
        )

    def _coerce_percent(self, value: Any) -> float | None:
        if isinstance(value, bool):
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

    def _parse_reset_at(self, value: Any) -> datetime | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            timestamp = int(value)
        elif isinstance(value, str) and value.strip().isdigit():
            timestamp = int(value.strip())
        else:
            return None
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)


class _AuthRequired(Exception):
    pass


class _ProviderFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
