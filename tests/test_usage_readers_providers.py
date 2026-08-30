"""Focused tests for Claude, Grok, Antigravity, Cursor, and OpenCode Go readers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from mindsync.dispatch.adapters import load_adapters
from mindsync.dispatch.usage.readers import antigravity as antigravity_mod
from mindsync.dispatch.usage.readers.antigravity import AntigravityOAuthUsageReader
from mindsync.dispatch.usage.readers.claude import ClaudeOAuthUsageReader
from mindsync.dispatch.usage.readers.cursor import CursorOAuthUsageReader
from mindsync.dispatch.usage.readers.grok import GrokOAuthUsageReader
from mindsync.dispatch.usage.readers.opencode_go import OpenCodeGoUsageReader


def test_presets_declare_preemptive_readers():
    adapters = load_adapters()
    assert adapters["claude"].usageReader == "claude-oauth"
    assert adapters["grok"].usageReader == "grok-oauth"
    assert adapters["gemini"].usageReader == "antigravity-oauth"
    assert adapters["agy"].usageReader == "antigravity-oauth"
    assert adapters["cursor"].usageReader == "cursor-oauth"
    assert adapters["opencode"].usageReader == "opencode-go"
    assert adapters["gemini"].quotaScope == adapters["agy"].quotaScope == "google:default"


def test_claude_missing_credentials(tmp_path: Path):
    result = ClaudeOAuthUsageReader(claude_home=tmp_path).read()
    assert result.status == "unavailable"
    assert result.reason == "claude auth source missing"


def test_claude_reads_session_and_weekly(tmp_path: Path):
    (tmp_path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-test",
                    "refreshToken": "sk-ant-ort01-test",
                    "expiresAt": 4102444800000,
                }
            }
        ),
        encoding="utf-8",
    )

    def fetch(url: str, headers: dict[str, str], **_kwargs: object) -> dict:
        assert "oauth/usage" in url
        assert headers["Authorization"] == "Bearer sk-ant-oat01-test"
        return {
            "limits": [
                {"kind": "session", "percent": 12, "resets_at": "2026-08-30T12:00:00Z"},
                {"kind": "weekly_all", "percent": 73, "resets_at": "2026-09-02T15:59:59Z"},
            ]
        }

    result = ClaudeOAuthUsageReader(claude_home=tmp_path, fetch_fn=fetch).read()
    assert result.status == "available"
    assert [window.id for window in result.windows] == ["primary", "weekly"]
    assert result.windows[0].used_percent == 12.0
    assert result.windows[1].used_percent == 73.0
    assert result.windows[1].reset_at == datetime(2026, 9, 2, 15, 59, 59, tzinfo=timezone.utc)


def test_claude_refresh_persists_rotated_token(tmp_path: Path):
    cred_path = tmp_path / ".credentials.json"
    cred_path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def fetch(url: str, headers: dict[str, str], **kwargs: object) -> dict:
        calls.append(url)
        if "oauth/token" in url:
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 28800,
            }
        assert headers["Authorization"] == "Bearer new-access"
        return {"five_hour": {"utilization": 4, "resets_at": None}, "seven_day": {"utilization": 20}}

    result = ClaudeOAuthUsageReader(claude_home=tmp_path, fetch_fn=fetch).read()
    assert result.status == "available"
    packed = json.loads(cred_path.read_text(encoding="utf-8"))
    assert packed["claudeAiOauth"]["accessToken"] == "new-access"
    assert packed["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert any("oauth/token" in url for url in calls)


def test_grok_reads_weekly_credits(tmp_path: Path):
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "https://auth.x.ai::abc": {
                    "auth_mode": "oidc",
                    "key": "jwt-token",
                    "user_id": "user-1",
                }
            }
        ),
        encoding="utf-8",
    )

    def fetch(url: str, headers: dict[str, str], **_kwargs: object) -> dict:
        assert "billing" in url
        assert headers["Authorization"] == "Bearer jwt-token"
        assert headers["x-userid"] == "user-1"
        return {
            "config": {
                "creditUsagePercent": 13,
                "currentPeriod": {"end": "2026-08-31T11:57:47Z"},
                "productUsage": [{"usagePercent": 13}],
            }
        }

    result = GrokOAuthUsageReader(grok_home=tmp_path, fetch_fn=fetch).read()
    assert result.status == "available"
    assert result.windows[0].used_percent == 13.0
    assert result.windows[-1].used_percent == 13.0


def test_antigravity_hottest_weekly_bucket():
    def loader() -> dict:
        return {"token": {"access_token": "ya29.test", "expiry": "2099-01-01T00:00:00Z"}}

    def fetch(url: str, headers: dict[str, str], **_kwargs: object) -> dict:
        assert "Antigravity/" in headers["User-Agent"]
        if url.endswith("loadCodeAssist"):
            return {"cloudaicompanionProject": "clean-hook"}
        return {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "window": "weekly",
                            "remainingFraction": 0.806224,
                            "resetTime": "2026-09-02T21:15:13Z",
                        },
                        {
                            "bucketId": "gemini-5h",
                            "window": "5h",
                            "remainingFraction": 1,
                            "resetTime": "2026-08-30T13:47:41Z",
                        },
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {
                            "bucketId": "3p-weekly",
                            "window": "weekly",
                            "remainingFraction": 0.98754096,
                            "resetTime": "2026-09-02T10:27:56Z",
                        }
                    ],
                },
            ]
        }

    result = AntigravityOAuthUsageReader(credential_loader=loader, fetch_fn=fetch).read()
    assert result.status == "available"
    by_id = {window.id: window for window in result.windows}
    assert round(by_id["weekly"].used_percent, 1) == 19.4
    assert by_id["primary"].used_percent == 0.0


def test_cursor_reads_plan_usage():
    def fetch(url: str, headers: dict[str, str], **_kwargs: object) -> dict:
        assert "GetCurrentPeriodUsage" in url
        assert headers["Connect-Protocol-Version"] == "1"
        return {
            "planUsage": {"autoPercentUsed": 51.66, "totalPercentUsed": 51.66},
            "billingCycleEnd": "1788606107000",
        }

    result = CursorOAuthUsageReader(token_loader=lambda: "cursor-jwt", fetch_fn=fetch).read()
    assert result.status == "available"
    assert result.windows[0].used_percent == 51.66
    assert result.windows[-1].used_percent == 51.66


def test_cursor_missing_token_without_db(tmp_path: Path):
    result = CursorOAuthUsageReader(cursor_db=tmp_path / "missing.vscdb").read()
    assert result.status == "unavailable"
    assert result.reason == "cursor auth source missing"


def test_opencode_go_prefers_hotter_monthly_window(tmp_path: Path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"opencode-go": {"type": "api", "key": "sk-test"}}), encoding="utf-8")

    def fetch(url: str, headers: dict[str, str], **_kwargs: object) -> dict:
        assert headers["Authorization"] == "Bearer sk-test"
        return {
            "usage": {
                "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-08-30T11:30:18Z"},
                "weekly": {"status": "ok", "percent": 41, "resetsAt": "2026-08-31T00:00:00Z"},
                "monthly": {"status": "ok", "percent": 75, "resetsAt": "2026-09-07T12:03:54Z"},
            }
        }

    result = OpenCodeGoUsageReader(opencode_home=tmp_path, fetch_fn=fetch).read()
    assert result.status == "available"
    assert result.windows[0].used_percent == 0.0
    assert result.windows[-1].used_percent == 75.0


def test_unauthenticated_claude(tmp_path: Path):
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": 4102444800000}}),
        encoding="utf-8",
    )

    def fetch(_url: str, _headers: dict[str, str], **_kwargs: object) -> dict:
        raise HTTPError("https://api.anthropic.com", 401, "unauthorized", hdrs=None, fp=None)

    result = ClaudeOAuthUsageReader(claude_home=tmp_path, fetch_fn=fetch).read()
    assert result.status == "unavailable"
    assert result.reason == "claude credentials unauthenticated"


def test_antigravity_source_has_no_embedded_oauth_client():
    text = Path(antigravity_mod.__file__).read_text(encoding="utf-8")
    assert "apps.googleusercontent.com" not in text
    assert "GOCSPX-" not in text


def test_antigravity_refresh_without_oauth_client(monkeypatch):
    monkeypatch.delenv("MINDSYNC_ANTIGRAVITY_CLIENT_ID", raising=False)
    monkeypatch.delenv("MINDSYNC_ANTIGRAVITY_CLIENT_SECRET", raising=False)

    def loader() -> dict:
        return {
            "token": {
                "access_token": "old",
                "refresh_token": "refresh",
                "expiry": "2000-01-01T00:00:00Z",
            }
        }

    result = AntigravityOAuthUsageReader(credential_loader=loader, fetch_fn=lambda *_a, **_k: {}).read()
    assert result.status == "unavailable"
    assert result.reason == "antigravity credentials unauthenticated"


def test_antigravity_refresh_uses_injected_oauth_client():
    seen: dict[str, bytes] = {}

    def loader() -> dict:
        return {
            "token": {
                "access_token": "old",
                "refresh_token": "refresh-token",
                "expiry": "2000-01-01T00:00:00Z",
            }
        }

    def fetch(url: str, _headers: dict[str, str], **kwargs: object) -> dict:
        if "oauth2.googleapis.com" in url:
            body = kwargs.get("body")
            assert isinstance(body, (bytes, bytearray))
            seen["body"] = bytes(body)
            return {"access_token": "ya29.refreshed"}
        if url.endswith("loadCodeAssist"):
            return {}
        return {
            "groups": [
                {
                    "buckets": [
                        {"window": "weekly", "remainingFraction": 0.5, "resetTime": "2026-09-02T00:00:00Z"}
                    ]
                }
            ]
        }

    result = AntigravityOAuthUsageReader(
        credential_loader=loader,
        fetch_fn=fetch,
        client_id="test-client-id",
        client_secret="test-client-secret",
    ).read()
    assert result.status == "available"
    assert b"client_id=test-client-id" in seen["body"]
    assert b"client_secret=test-client-secret" in seen["body"]
