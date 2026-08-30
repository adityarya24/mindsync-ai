"""Focused tests for pluggable usage readers."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.error import HTTPError

import pytest

import mindsync.dispatch.usage.registry as usage_registry
from mindsync.dispatch.adapters import AdapterConfig, load_adapters
from mindsync.dispatch.usage import (
    evaluate_adapter_threshold,
    evaluate_threshold,
    load_usage_config,
    read_usage_for_adapter,
    safe_read,
)
from mindsync.dispatch.usage.config import UsageConfig
from mindsync.dispatch.usage.readers.claude import ClaudeOAuthUsageReader
from mindsync.dispatch.usage.readers.codex import CodexOAuthUsageReader
from mindsync.dispatch.usage.readers.cursor import CursorOAuthUsageReader
from mindsync.dispatch.usage.types import UsageReadResult, UsageWindow


@pytest.fixture(autouse=True)
def _clear_usage_snapshot_cache():
    usage_registry._clear_usage_cache()
    yield
    usage_registry._clear_usage_cache()


def _usage_payload(
  primary_percent: float,
  weekly_percent: float,
  *,
  primary_reset: int = 1_700_000_000,
  weekly_reset: int = 1_700_086_400,
) -> dict:
    return {
        "rate_limit": {
            "primary_window": {
                "used_percent": primary_percent,
                "reset_at": primary_reset,
            },
            "secondary_window": {
                "used_percent": weekly_percent,
                "reset_at": weekly_reset,
            },
        }
    }


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    return home


def test_codex_preset_declares_native_reader_without_runtime_wiring():
    adapter = load_adapters()["codex"]
    assert adapter.usageReader == "codex-oauth"
    assert adapter.usageThresholdPercent is None


def test_missing_auth_source_returns_unavailable(codex_home: Path):
    reader = CodexOAuthUsageReader(codex_home=codex_home, account_scope="openai:test")
    result = reader.read()
    assert result.status == "unavailable"
    assert result.reason == "codex auth source missing"
    assert result.account_scope == "openai:test"


def test_malformed_auth_json_returns_unavailable(codex_home: Path):
    (codex_home / "auth.json").write_text("{not-json", encoding="utf-8")
    result = CodexOAuthUsageReader(codex_home=codex_home).read()
    assert result.status == "unavailable"
    assert result.reason == "codex usage data malformed"


def test_unauthenticated_provider_failure_returns_unavailable(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token", "account_id": "acct-1"}}),
        encoding="utf-8",
    )

    def fetch(_url: str, _headers: dict[str, str]) -> dict:
        raise HTTPError("https://chatgpt.com", 401, "unauthorized", hdrs=None, fp=None)

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "unavailable"
    assert result.reason == "codex credentials unauthenticated"


def test_network_provider_failure_returns_unavailable(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token"}}),
        encoding="utf-8",
    )

    def fetch(_url: str, _headers: dict[str, str]) -> dict:
        raise OSError("network down")

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "unavailable"
    assert result.reason == "codex provider request failed"


def test_malformed_usage_payload_returns_unavailable(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token"}}),
        encoding="utf-8",
    )

    def fetch(_url: str, _headers: dict[str, str]) -> dict:
        return {"rate_limit": {"primary_window": {"reset_at": 1}}}

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "unavailable"
    assert result.reason == "codex usage data malformed"


def test_reads_primary_and_weekly_windows_with_reset_metadata(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token", "account_id": "acct-42"}}),
        encoding="utf-8",
    )

    def fetch(_url: str, headers: dict[str, str]) -> dict:
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer test-token"
        assert headers["ChatGPT-Account-Id"] == "acct-42"
        assert "test-token" not in json.dumps(_usage_payload(12.5, 48.0))
        return _usage_payload(12.5, 48.0)

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "available"
    assert result.account_scope == "openai:acct-42"
    assert [window.id for window in result.windows] == ["primary", "weekly"]
    assert result.windows[0].used_percent == 12.5
    assert result.windows[1].used_percent == 48.0
    assert result.windows[0].reset_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_weekly_only_payload_promotes_secondary_to_primary(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token"}}),
        encoding="utf-8",
    )

    def fetch(_url: str, _headers: dict[str, str]) -> dict:
        return {
            "rate_limit": {
                "secondary_window": {
                    "used_percent": 33.0,
                    "reset_at": 1_700_000_100,
                }
            }
        }

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "available"
    assert len(result.windows) == 1
    assert result.windows[0].id == "primary"
    assert result.windows[0].used_percent == 33.0


@pytest.mark.parametrize(
    ("primary", "weekly", "expected"),
    [
        (89.9, 10.0, "below_threshold"),
        (90.0, 10.0, "at_threshold"),
        (10.0, 95.0, "at_threshold"),
        (100.0, 100.0, "at_threshold"),
    ],
)
def test_threshold_evaluation_across_both_windows(primary, weekly, expected):
    result = UsageReadResult.available(
        provider="codex",
        account_scope="openai:default",
        reader="codex-oauth",
        source="test",
        windows=[
            UsageWindow(
                id="primary",
                label="Primary",
                used_percent=primary,
                reset_at=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
            ),
            UsageWindow(
                id="weekly",
                label="Weekly",
                used_percent=weekly,
                reset_at=datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
            ),
        ],
    )
    evaluation = evaluate_threshold(result, threshold_percent=90.0)
    assert evaluation.status == expected
    if expected == "at_threshold":
        assert evaluation.triggering_window is not None
        assert evaluation.triggering_window.used_percent >= 90.0


def test_threshold_uses_earliest_reset_among_triggering_windows():
    earlier = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    later = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    result = UsageReadResult.available(
        provider="codex",
        account_scope="openai:default",
        reader="codex-oauth",
        source="test",
        windows=[
            UsageWindow(id="primary", label="Primary", used_percent=92.0, reset_at=later),
            UsageWindow(id="weekly", label="Weekly", used_percent=91.0, reset_at=earlier),
        ],
    )
    evaluation = evaluate_threshold(result, threshold_percent=90.0)
    assert evaluation.status == "at_threshold"
    assert evaluation.triggering_window is not None
    assert evaluation.triggering_window.id == "primary"
    assert evaluation.earliest_reset_at == earlier


def test_percentage_boundaries_reject_out_of_range(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "test-token"}}),
        encoding="utf-8",
    )

    def fetch(_url: str, _headers: dict[str, str]) -> dict:
        return {
            "rate_limit": {
                "primary_window": {"used_percent": 150.0, "reset_at": 1},
                "secondary_window": {"used_percent": 10.0, "reset_at": 2},
            }
        }

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "unavailable"
    assert result.reason == "codex usage data malformed"


def test_default_usage_config_keeps_cursor_opt_in_off():
    config = UsageConfig()
    assert config.enabled is False
    assert config.readers.cursor is False
    empty = load_usage_config()
    assert empty.readers.cursor is False


def test_cursor_reader_stays_unavailable_until_opted_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"usage": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setattr("mindsync.dispatch.usage.config.user_config_path", lambda: agents)
    monkeypatch.setattr("mindsync.dispatch.usage.readers.cursor.load_usage_config", load_usage_config)

    def boom() -> str:
        raise AssertionError("cursor db opened without opt-in")

    adapter = AdapterConfig(
        name="cursor",
        bin="cursor-agent",
        usageReader="cursor-oauth",
        quotaScope="cursor:default",
    )
    result = read_usage_for_adapter(adapter, usage_config=load_usage_config(agents))
    assert result.status == "unavailable"
    assert result.reason == "cursor reader not enabled (set usage.readers.cursor)"
    assert CursorOAuthUsageReader(token_loader=boom).read().reason == (
        "cursor reader not enabled (set usage.readers.cursor)"
    )

    opted = UsageConfig(enabled=True, readers={"cursor": True})
    live = CursorOAuthUsageReader(
        enabled=True,
        token_loader=lambda: "cursor-jwt",
        fetch_fn=lambda *_a, **_k: {
            "planUsage": {"autoPercentUsed": 3.0, "totalPercentUsed": 3.0},
            "billingCycleEnd": "1788606107000",
        },
    ).read()
    assert live.status == "available"
    assert opted.readers.cursor is True


def test_non_cursor_readers_stay_on_when_usage_enabled(tmp_path: Path):
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / ".credentials.json").write_text(
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
    result = ClaudeOAuthUsageReader(
        claude_home=claude_home,
        fetch_fn=lambda *_a, **_k: {
            "limits": [{"kind": "session", "percent": 8, "resets_at": "2026-08-30T12:00:00Z"}]
        },
    ).read()
    assert result.status == "available"
    assert result.windows[0].used_percent == 8.0


def test_adapter_without_reader_stays_unavailable():
    adapter = AdapterConfig(name="codex", bin="codex")
    result = read_usage_for_adapter(adapter)
    assert result.status == "unavailable"
    assert result.reason == "usage reader not configured"


def test_adapter_usage_snapshot_is_shared_by_scope_and_refreshes_on_interval(monkeypatch):
    clock = [100.0]
    calls: list[str] = []

    def fake_safe_read(_reader_name: str, **kwargs) -> UsageReadResult:
        calls.append(kwargs["account_scope"])
        return UsageReadResult.available(
            provider="codex",
            account_scope=kwargs["account_scope"],
            reader="codex-oauth",
            source="test",
            windows=[
                UsageWindow(
                    id="primary",
                    label="Primary",
                    used_percent=float(len(calls)),
                )
            ],
        )

    monkeypatch.setattr(usage_registry, "safe_read", fake_safe_read)
    monkeypatch.setattr(usage_registry, "monotonic", lambda: clock[0])
    config = UsageConfig(enabled=True, pollingIntervalSeconds=5)
    primary = AdapterConfig(
        name="codex",
        bin="codex",
        usageReader="codex-oauth",
        quotaScope="openai:shared",
    )
    alias = AdapterConfig(
        name="codex-backup",
        bin="codex",
        usageReader="codex-oauth",
        quotaScope="openai:shared",
    )

    first = read_usage_for_adapter(primary, usage_config=config)
    clock[0] = 104.999
    cached = read_usage_for_adapter(alias, usage_config=config)
    clock[0] = 105.0
    refreshed = read_usage_for_adapter(primary, usage_config=config)

    assert calls == ["openai:shared", "openai:shared"]
    assert first.windows[0].used_percent == cached.windows[0].used_percent == 1.0
    assert refreshed.windows[0].used_percent == 2.0


def test_adapter_usage_snapshot_deduplicates_concurrent_reads(monkeypatch):
    entered = Event()
    release = Event()
    calls = 0

    def fake_safe_read(_reader_name: str, **kwargs) -> UsageReadResult:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return UsageReadResult.available(
            provider="claude",
            account_scope=kwargs["account_scope"],
            reader="claude-oauth",
            source="test",
            windows=[UsageWindow(id="session", label="Session", used_percent=12.0)],
        )

    monkeypatch.setattr(usage_registry, "safe_read", fake_safe_read)
    adapter = AdapterConfig(
        name="claude",
        bin="claude",
        usageReader="claude-oauth",
        quotaScope="anthropic:shared",
    )
    config = UsageConfig(enabled=True, pollingIntervalSeconds=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(read_usage_for_adapter, adapter, usage_config=config)
        assert entered.wait(timeout=2)
        second = pool.submit(read_usage_for_adapter, adapter, usage_config=config)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert calls == 1
    assert [result.windows[0].used_percent for result in results] == [12.0, 12.0]


def test_unavailable_usage_snapshot_avoids_immediate_retry_storm(monkeypatch):
    calls = 0

    def fake_safe_read(_reader_name: str, **kwargs) -> UsageReadResult:
        nonlocal calls
        calls += 1
        return UsageReadResult.unavailable(
            provider="claude",
            account_scope=kwargs["account_scope"],
            reader="claude-oauth",
            reason="claude provider request failed",
        )

    monkeypatch.setattr(usage_registry, "safe_read", fake_safe_read)
    adapter = AdapterConfig(
        name="claude",
        bin="claude",
        usageReader="claude-oauth",
        quotaScope="anthropic:shared",
    )
    config = UsageConfig(enabled=True, pollingIntervalSeconds=60)

    first = read_usage_for_adapter(adapter, usage_config=config)
    second = read_usage_for_adapter(adapter, usage_config=config)

    assert calls == 1
    assert first.status == second.status == "unavailable"
    assert first.reason == second.reason == "claude provider request failed"


def test_adapter_scope_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    agents = tmp_path / "agents.json"
    agents.write_text(
        json.dumps(
            {
                "usage": {"enabled": False, "defaultThresholdPercent": 85},
                "agents": [
                    {
                        "name": "codex",
                        "usageReader": "codex-oauth",
                        "usageThresholdPercent": 80,
                        "quotaScope": "openai:team-a",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "mindsync.dispatch.usage.config.user_config_path",
        lambda: agents,
    )
    config = load_usage_config(agents)
    assert config.enabled is False
    assert config.defaultThresholdPercent == 85

    adapter = AdapterConfig(
        name="codex",
        bin="codex",
        usageReader="codex-oauth",
        usageThresholdPercent=80,
        quotaScope="openai:team-a",
    )
    result = UsageReadResult.available(
        provider="codex",
        account_scope="openai:team-a",
        reader="codex-oauth",
        source="test",
        windows=[UsageWindow(id="primary", label="Primary", used_percent=81.0)],
    )
    evaluation = evaluate_adapter_threshold(adapter, usage_config=config, result=result)
    assert evaluation.status == "at_threshold"
    assert evaluation.account_scope == "openai:team-a"


def test_safe_read_never_raises_for_unknown_reader():
    result = safe_read("missing-reader", provider="codex", account_scope="openai:default")
    assert result.status == "unavailable"
    assert result.reason == "usage reader configuration invalid"


def test_public_serialization_contains_no_secrets():
    result = UsageReadResult.available(
        provider="codex",
        account_scope="openai:default",
        reader="codex-oauth",
        source="test",
        windows=[UsageWindow(id="primary", label="Primary", used_percent=10.0)],
    )
    payload = json.dumps(result.model_dump(mode="json"))
    assert "access_token" not in payload
    assert "Bearer" not in payload
    assert "refresh_token" not in payload

    evaluation = evaluate_threshold(result, threshold_percent=90.0)
    public = json.dumps(evaluation.to_public_dict())
    assert "access_token" not in public
    assert "Bearer" not in public


def test_invalid_usage_config_threshold_rejected(tmp_path: Path):
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"usage": {"defaultThresholdPercent": 0}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_usage_config(agents)


def test_invalid_adapter_reader_name_rejected():
    with pytest.raises(ValueError, match="Unknown usage reader"):
        AdapterConfig(name="codex", bin="codex", usageReader="codexbar")


def test_api_key_credentials_return_unavailable_without_fetch(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-secret-key"}),
        encoding="utf-8",
    )
    called: list[tuple[str, dict[str, str]]] = []

    def fetch(url: str, headers: dict[str, str]) -> dict:
        called.append((url, headers))
        return _usage_payload(10.0, 5.0)

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "unavailable"
    assert result.reason == "codex credentials unavailable"
    assert not called


def test_nested_provider_chatgpt_base_url_is_ignored(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "oauth-token"}}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        '[model_providers.other]\nchatgpt_base_url = "https://evil.com"\n',
        encoding="utf-8",
    )
    captured: list[str] = []

    def fetch(url: str, _headers: dict[str, str]) -> dict:
        captured.append(url)
        return _usage_payload(10.0, 5.0)

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "available"
    assert captured == ["https://chatgpt.com/backend-api/wham/usage"]


def test_unsafe_top_level_chatgpt_base_url_defaults_to_chatgpt(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "oauth-token"}}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'chatgpt_base_url = "https://evil.com/exfil"\n',
        encoding="utf-8",
    )
    captured: list[str] = []

    def fetch(url: str, _headers: dict[str, str]) -> dict:
        captured.append(url)
        return _usage_payload(10.0, 5.0)

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "available"
    assert captured == ["https://chatgpt.com/backend-api/wham/usage"]


def test_top_level_chatgpt_base_url_honored_via_toml(codex_home: Path):
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "oauth-token"}}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'chatgpt_base_url = "http://127.0.0.1:8080/backend-api"\n',
        encoding="utf-8",
    )
    captured: list[str] = []

    def fetch(url: str, _headers: dict[str, str]) -> dict:
        captured.append(url)
        return _usage_payload(10.0, 5.0)

    result = CodexOAuthUsageReader(codex_home=codex_home, fetch_fn=fetch).read()
    assert result.status == "available"
    assert captured == ["http://127.0.0.1:8080/backend-api/wham/usage"]
