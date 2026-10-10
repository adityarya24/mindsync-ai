"""Reactive provider-quota classification and cooldown tests."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mindsync.dispatch.routing as routing_mod
import mindsync.dispatch.runner as runner_mod
from mindsync.dispatch.adapters import AdapterConfig, user_config_path
from mindsync.dispatch.cli import parse_run_args
from mindsync.dispatch.limits import (
    classify_quota_exhaustion,
    clear_cooldowns,
    extract_reactive_reset_at,
    list_cooldowns,
    mark_cooling,
    reactive_reset_source,
)
from mindsync.dispatch.routing import select_agent
from mindsync.dispatch import store
from mindsync.dispatch.runner import run_task
from tests.test_dispatch import _isolate_dispatch


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repo / "file.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
    return repo


def _write_agents(tmp_path: Path, monkeypatch) -> None:
    _isolate_dispatch(tmp_path, monkeypatch)
    user_config_path().write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "primary",
                        "bin": sys.executable,
                        "input": "stdin",
                        "capabilities": ["coding"],
                        "capabilityWeights": {"coding": 100},
                        "routingPriority": 100,
                        "quotaScope": "provider:account-a",
                        "quotaErrorPatterns": ["(?i)usage window exhausted; resets at"],
                        "quotaCooldownSeconds": 300,
                    },
                    {
                        "name": "backup",
                        "bin": sys.executable,
                        "input": "stdin",
                        "capabilities": ["coding"],
                        "capabilityWeights": {"coding": 90},
                        "routingPriority": 90,
                        "quotaScope": "provider:account-b",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    def only_python(value: str) -> str | None:
        return sys.executable if value == sys.executable else None
    monkeypatch.setattr(runner_mod, "resolve_bin", only_python)
    monkeypatch.setattr(routing_mod, "resolve_bin", only_python)


def test_classifier_is_provider_specific_and_rejects_generic_rate_limit():
    adapter = AdapterConfig(
        name="narrow",
        bin="narrow",
        quotaErrorPatterns=[r"(?i)usage window exhausted; resets at"],
    )

    assert classify_quota_exhaustion(
        adapter, stderr="Usage window exhausted; resets at 17:00"
    )
    assert classify_quota_exhaustion(adapter, stderr="rate limit exceeded") is None
    assert classify_quota_exhaustion(adapter, stderr="authentication failed") is None
    assert classify_quota_exhaustion(
        adapter,
        stdout="x" * 20_000,
        stderr="Usage window exhausted; resets at 17:00",
    )
    assert classify_quota_exhaustion(
        adapter,
        stdout="Usage window exhausted; resets at 17:00",
        stderr="ordinary failure",
    ) is None


def test_claude_captured_usage_message_matches_narrow_signature():
    adapter = AdapterConfig(
        name="claude",
        bin="claude",
        quotaErrorPatterns=[r"(?im)^Claude AI usage limit reached\|[0-9]{10}\s*$"],
    )
    assert classify_quota_exhaustion(
        adapter, stderr="Claude AI usage limit reached|1787949999"
    )
    assert classify_quota_exhaustion(
        adapter, stderr="Claude AI rate limit reached|1787949999"
    ) is None


def test_claude_reactive_reset_parses_future_epoch_from_stderr():
    adapter = AdapterConfig(
        name="claude",
        bin="claude",
        quotaErrorPatterns=[r"(?im)^Claude AI usage limit reached\|[0-9]{10}\s*$"],
    )
    future = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    reset_at = extract_reactive_reset_at(
        adapter, stderr=f"Claude AI usage limit reached|{future}"
    )

    assert reset_at is not None
    assert reset_at == datetime.fromtimestamp(future, tz=timezone.utc)


def test_reactive_reset_source_is_parser_only_for_allowlisted_claude_pattern():
    claude = AdapterConfig(
        name="claude",
        bin="claude",
        quotaErrorPatterns=[r"(?im)^Claude AI usage limit reached\|[0-9]{10}\s*$"],
    )
    grok = AdapterConfig(name="grok", bin="grok", quotaErrorPatterns=[r"quota"])
    assert reactive_reset_source(claude) == "claude-stderr-epoch"
    assert reactive_reset_source(grok) == "quotaCooldownSeconds"


def test_reactive_reset_rejects_past_malformed_and_stdout_only_values():
    adapter = AdapterConfig(
        name="claude",
        bin="claude",
        quotaErrorPatterns=[r"(?im)^Claude AI usage limit reached\|[0-9]{10}\s*$"],
    )
    past = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
    assert extract_reactive_reset_at(adapter, stderr=f"Claude AI usage limit reached|{past}") is None
    assert extract_reactive_reset_at(adapter, stderr="Claude AI usage limit reached|not-a-ts") is None
    assert extract_reactive_reset_at(
        adapter,
        stderr="",
    ) is None
    far_future = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    assert extract_reactive_reset_at(
        adapter, stderr=f"Claude AI usage limit reached|{far_future}"
    ) is None


def test_reactive_reset_rejects_millisecond_shaped_values_without_crashing():
    adapter = AdapterConfig(
        name="claude",
        bin="claude",
        quotaErrorPatterns=[r"(?im)^Claude AI usage limit reached\|[0-9]{10}\s*$"],
    )
    milliseconds = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp() * 1000)
    stderr = f"Claude AI usage limit reached|{milliseconds}"

    assert classify_quota_exhaustion(adapter, stderr=stderr) is None
    assert extract_reactive_reset_at(adapter, stderr=stderr) is None


def test_cooldown_applies_to_every_entry_for_one_provider_account(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    shared_a = AdapterConfig(
        name="shared-a", bin=sys.executable, capabilities=["coding"],
        quotaScope="provider:shared", routingPriority=100,
    )
    shared_b = AdapterConfig(
        name="shared-b", bin=sys.executable, capabilities=["coding"],
        quotaScope="provider:shared", routingPriority=90,
    )
    other = AdapterConfig(
        name="other", bin=sys.executable, capabilities=["coding"],
        quotaScope="provider:other", routingPriority=10,
    )
    mark_cooling(shared_a)
    assert list_cooldowns()[0]["scope"] == "provider:shared"

    decision = select_agent(
        "implement", required_capabilities=["coding"],
        adapters={row.name: row for row in (shared_a, shared_b, other)},
    )

    assert decision["agent"] == "other"
    assert {"shared-a", "shared-b"}.issubset(decision["unavailableAgents"])
    with pytest.raises(RuntimeError, match="provider account cooling until"):
        select_agent(
            "implement",
            required_capabilities=["coding"],
            adapters={row.name: row for row in (shared_a, shared_b)},
        )
    assert clear_cooldowns("provider:shared") == 1
    assert list_cooldowns() == []


def test_claim_bootstraps_missing_lease_on_first_worktree_attempt(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    meta = store.create_job(agent="primary", prompt="x", cwd=str(tmp_path))
    store.update_job(
        meta["id"],
        {
            "status": "running",
            "worktreePath": str(tmp_path / "wt"),
        },
    )

    claimed = store.claim_worktree_lease(
        meta["id"],
        agent="primary",
        attempt=1,
        attempts=[{"number": 1, "agent": "primary", "status": "running"}],
    )

    assert claimed["worktreeLease"] == {
        "attempt": 1,
        "agent": "primary",
        "state": "running",
    }


def test_claim_rejects_conflicting_worktree_owner(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    meta = store.create_job(agent="primary", prompt="x", cwd=str(tmp_path))
    store.update_job(
        meta["id"],
        {
            "status": "running",
            "worktreePath": str(tmp_path / "wt"),
            "worktreeLease": {"attempt": 1, "agent": "primary", "state": "owned"},
        },
    )

    with pytest.raises(RuntimeError, match="worktree lease is not available"):
        store.claim_worktree_lease(
            meta["id"],
            agent="backup",
            attempt=1,
            attempts=[{"number": 1, "agent": "backup", "status": "running"}],
        )


@pytest.mark.asyncio
async def test_supervise_claims_worktree_lease_before_first_spawn(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_agents(tmp_path, monkeypatch)

    async def fake_spawn(*args, **kwargs):
        job = store.list_jobs()[0]
        assert job["worktreeLease"]["state"] == "running"
        return {
            "stdout": "ok",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
        }

    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="primary",
        prompt="implement",
        cwd=str(fake_repo),
        worktree=True,
        memory_mode="off",
    )

    assert result["job"]["status"] == "done"
    assert result["job"]["worktreeLease"]["state"] == "released"


@pytest.mark.asyncio
async def test_stop_marks_provider_cooldown(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_agents(tmp_path, monkeypatch)

    async def fake_spawn(*args, **kwargs):
        return {
            "stdout": "",
            "stderr": "Usage window exhausted; resets at 17:00",
            "exitCode": 1,
            "timedOut": False,
            "processTreeDead": True,
        }

    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="primary", prompt="implement", cwd=str(fake_repo),
        worktree=True, on_limit="stop", memory_mode="off",
    )

    assert result["job"]["status"] == "failed"
    assert result["job"]["quotaFailure"]["cooldownUntil"]
    assert list_cooldowns()[0]["scope"] == "provider:account-a"


def test_cli_rejects_paid_quota_handoff():
    with pytest.raises(SystemExit, match="quota handoff is part of MindSync Pro"):
        parse_run_args(["auto", "do", "work", "--on-limit", "handoff"])


def test_cli_keeps_stop_as_the_only_on_limit_mode():
    parsed = parse_run_args(["auto", "do", "work", "--on-limit", "stop"])
    assert parsed["on_limit"] == "stop"
    with pytest.raises(SystemExit):
        parse_run_args(["auto", "do", "work", "--on-limit", "typo"])


@pytest.mark.asyncio
async def test_api_rejects_paid_quota_handoff():
    with pytest.raises(ValueError, match="quota handoff is part of MindSync Pro"):
        await run_task(agent="codex", prompt="x", on_limit="handoff")
