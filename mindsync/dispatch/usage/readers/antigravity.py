"""Antigravity / Gemini CLI OAuth quota reader."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode

from mindsync.dispatch.usage.common import (
    AuthRequired,
    ProviderFailure,
    coerce_percent,
    http_json,
    parse_reset_at,
    remaining_to_used,
    session_weekly_windows,
)
from mindsync.dispatch.usage.types import UsageReadResult

READER_NAME = "antigravity-oauth"
PROVIDER = "gemini"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CLIENT_ID_ENV = "MINDSYNC_ANTIGRAVITY_CLIENT_ID"
CLIENT_SECRET_ENV = "MINDSYNC_ANTIGRAVITY_CLIENT_SECRET"
LOAD_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
SUMMARY_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
VAULT_TARGET = "gemini:antigravity"
FetchFn = Callable[..., dict[str, Any]]
CredentialLoader = Callable[[], dict[str, Any] | None]


class AntigravityOAuthUsageReader:
    """Read Antigravity 5h + weekly pools from the official CLI OAuth store."""

    def __init__(
        self,
        *,
        account_scope: str | None = None,
        fetch_fn: FetchFn | None = None,
        credential_loader: CredentialLoader | None = None,
        request_timeout_seconds: float | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self._account_scope = account_scope
        self._fetch_fn = fetch_fn
        self._credential_loader = credential_loader
        self._request_timeout_seconds = request_timeout_seconds
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def reader(self) -> str:
        return READER_NAME

    def read(self) -> UsageReadResult:
        try:
            token = self._access_token()
            if token is None:
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="antigravity auth source missing",
                    reader=READER_NAME,
                )
            loaded = self._fetch(
                LOAD_URL,
                self._headers(token),
                method="POST",
                body=b"{}",
            )
            project = loaded.get("cloudaicompanionProject")
            payload: dict[str, Any] = {}
            if isinstance(project, str) and project.strip():
                name = project if project.startswith("projects/") else f"projects/{project}"
                payload = {"project": name}
            summary = self._fetch(
                SUMMARY_URL,
                self._headers(token),
                method="POST",
                body=json.dumps(payload).encode("utf-8"),
            )
            windows = self._parse_windows(summary)
            if not windows:
                return UsageReadResult.unavailable(
                    provider=PROVIDER,
                    account_scope=self._scope(),
                    reason="antigravity usage data malformed",
                    reader=READER_NAME,
                )
            return UsageReadResult.available(
                provider=PROVIDER,
                account_scope=self._scope(),
                reader=READER_NAME,
                source="antigravity-oauth-quota-summary",
                windows=windows,
            )
        except AuthRequired:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="antigravity credentials unauthenticated",
                reader=READER_NAME,
            )
        except ProviderFailure:
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="antigravity provider request failed",
                reader=READER_NAME,
            )
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError):
            return UsageReadResult.unavailable(
                provider=PROVIDER,
                account_scope=self._scope(),
                reason="antigravity usage data malformed",
                reader=READER_NAME,
            )

    def _scope(self) -> str:
        return self._account_scope or "google:default"

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Antigravity/4.3.0",
        }

    def _access_token(self) -> str | None:
        packed = self._load_credentials()
        if packed is None:
            return None
        token = packed.get("token") if isinstance(packed.get("token"), dict) else packed
        if not isinstance(token, dict):
            return None
        access = token.get("access_token")
        refresh = token.get("refresh_token")
        expiry = token.get("expiry")
        if isinstance(access, str) and access.strip() and not self._expired(expiry):
            return access.strip()
        if not isinstance(refresh, str) or not refresh.strip():
            raise AuthRequired
        client_id, client_secret = self._oauth_client()
        if not client_id or not client_secret:
            raise AuthRequired
        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode()
        payload = self._fetch(
            TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
            body=body,
        )
        new_access = payload.get("access_token")
        if not isinstance(new_access, str) or not new_access.strip():
            raise AuthRequired
        return new_access.strip()

    def _oauth_client(self) -> tuple[str | None, str | None]:
        client_id = (self._client_id or os.environ.get(CLIENT_ID_ENV) or "").strip() or None
        client_secret = (self._client_secret or os.environ.get(CLIENT_SECRET_ENV) or "").strip() or None
        return client_id, client_secret

    def _expired(self, expiry: Any) -> bool:
        if not isinstance(expiry, str) or not expiry.strip():
            return True
        try:
            parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() <= datetime.now(timezone.utc).timestamp() + 60

    def _load_credentials(self) -> dict[str, Any] | None:
        if self._credential_loader is not None:
            return self._credential_loader()
        return _read_windows_generic_credential(VAULT_TARGET)

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
                return self._fetch_fn(url, headers, method=method, body=body)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AuthRequired from exc
                raise ProviderFailure("antigravity provider request failed") from exc
        timeout = 30.0 if self._request_timeout_seconds is None else max(0.05, min(float(self._request_timeout_seconds), 30.0))
        return http_json(url, headers, method=method, body=body, timeout=timeout)

    def _parse_windows(self, payload: dict[str, Any]) -> list:
        groups = payload.get("groups")
        session_best: tuple[float, Any] | None = None
        weekly_best: tuple[float, Any] | None = None
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                buckets = group.get("buckets")
                if not isinstance(buckets, list):
                    continue
                for bucket in buckets:
                    if not isinstance(bucket, dict):
                        continue
                    used = remaining_to_used(bucket.get("remainingFraction"))
                    if used is None:
                        used = coerce_percent(bucket.get("used_percent"))
                    if used is None:
                        continue
                    window = str(bucket.get("window") or bucket.get("bucketId") or "").lower()
                    reset = parse_reset_at(bucket.get("resetTime") or bucket.get("resets_at"))
                    if "5h" in window:
                        if session_best is None or used > session_best[0]:
                            session_best = (used, reset)
                    if "weekly" in window:
                        if weekly_best is None or used > weekly_best[0]:
                            weekly_best = (used, reset)
        return session_weekly_windows(
            session_percent=None if session_best is None else session_best[0],
            session_reset=None if session_best is None else session_best[1],
            weekly_percent=None if weekly_best is None else weekly_best[0],
            weekly_reset=None if weekly_best is None else weekly_best[1],
        )


def _read_windows_generic_credential(target: str) -> dict[str, Any] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr = ctypes.c_void_p()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ctypes.cast(ptr, ctypes.POINTER(CREDENTIAL)).contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        text = blob.decode("utf-8", errors="replace").rstrip("\x00")
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    finally:
        advapi32.CredFree(ptr)
