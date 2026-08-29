"""Focused tests for pluggable usage readers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest

from mindsync.dispatch.adapters import AdapterConfig, load_adapters
from mindsync.dispatch.usage import (
    evaluate_adapter_threshold,
    evaluate_threshold,
    load_usage_config,
    read_usage_for_adapter,
    safe_read,
)
from mindsync.dispatch.usage.readers.codex import CodexOAuthUsageReader
from mindsync.dispatch.usage.types import UsageReadResult, UsageWindow


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


def test_adapter_without_reader_stays_unavailable():
    adapter = AdapterConfig(name="codex", bin="codex")
    result = read_usage_for_adapter(adapter)
    assert result.status == "unavailable"
    assert result.reason == "usage reader not configured"


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
