"""Tests for persistent orchestration policy and dispatch enforcement."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.orchestration as orchestration
import mindsync.storage as storage
from mindsync.dispatch import store
from mindsync.dispatch.adapters import user_config_path
from mindsync.dispatch.runner import (
    AutoDelegationDisabled,
    AutoDelegationSuggestion,
    run_task,
)
from mindsync.server import delegate_task


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "mindsync-home"))
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(tmp_path / "dispatch-home"))
    monkeypatch.delenv("MINDSYNC_CALLER_CLI", raising=False)
    settings = config_mod.Settings()
    config_mod.settings = settings
    storage.settings = settings
    orchestration.settings = settings
    settings.ensure_dirs()
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "builder",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; print(sys.stdin.read())"],
                        "capabilities": ["general", "coding"],
                        "capabilityWeights": {"coding": 100},
                        "routingPriority": 100,
                    },
                    {
                        "name": "reviewer",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; print(sys.stdin.read())"],
                        "capabilities": ["general", "review"],
                        "capabilityWeights": {"review": 100},
                        "routingPriority": 100,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return settings.orchestration_file


def test_policy_round_trip_and_update(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    assert orchestration.load_policy().mode == "auto"

    saved = orchestration.save_policy(
        orchestration.OrchestrationPolicy(mode="suggest", maxParallel=4)
    )
    assert saved == path
    assert orchestration.load_policy().mode == "suggest"
    assert orchestration.update_policy("orchestration.mode", "off").mode == "off"
    assert orchestration.update_policy("orchestration.announce", False).announce is False


def test_client_info_identifies_human_facing_cli_without_registration_env():
    ctx = SimpleNamespace(
        session=SimpleNamespace(
            client_params=SimpleNamespace(
                clientInfo=SimpleNamespace(name="Claude Code"),
            )
        )
    )
    assert orchestration.caller_cli_from_context(ctx) == "claude"
    assert orchestration.effective_exclusions([], caller_cli="claude") == ["claude"]


def test_gemini_and_antigravity_are_excluded_as_one_human_facing_family():
    assert orchestration.effective_exclusions([], caller_cli="gemini") == ["gemini", "agy"]
    assert orchestration.effective_exclusions([], caller_cli="agy") == ["agy", "gemini"]


def test_worker_process_receives_non_recursive_instructions(monkeypatch):
    monkeypatch.setenv("MINDSYNC_WORKER", "1")
    instructions = orchestration.server_instructions()
    assert "delegated worker" in instructions
    assert "Do not call delegate_task" in instructions


def test_invalid_policy_snapshot_fails_closed(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    path.write_text('{"mode":"broken"}', encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid orchestration policy"):
        orchestration.load_policy()
    snapshot = orchestration.policy_snapshot()
    assert snapshot["mode"] == "off"
    assert "error" in snapshot
    assert "invalid" in orchestration.server_instructions().lower()


@pytest.mark.asyncio
async def test_suggest_mode_returns_route_without_launching(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    orchestration.save_policy(orchestration.OrchestrationPolicy(mode="suggest"))

    with pytest.raises(AutoDelegationSuggestion) as exc_info:
        await run_task(agent="auto", prompt="implement it", required_capabilities=["coding"])
    assert exc_info.value.decision["agent"] == "builder"
    assert store.list_jobs() == []

    response = await delegate_task(prompt="implement it", required_capabilities=["coding"])
    assert response.startswith("Suggestion only; no job was launched.")
    assert store.list_jobs() == []


@pytest.mark.asyncio
async def test_off_mode_blocks_auto_but_keeps_explicit_agent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    orchestration.save_policy(orchestration.OrchestrationPolicy(mode="off"))

    with pytest.raises(AutoDelegationDisabled):
        await run_task(agent="auto", prompt="implement it")

    result = await run_task(agent="builder", prompt="explicit work")
    assert result["job"]["agent"] == "builder"
    assert "explicit work" in result["result"]


@pytest.mark.asyncio
async def test_mcp_blocks_recursive_delegation_from_worker(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("MINDSYNC_WORKER", "1")

    response = await delegate_task(agent="builder", prompt="delegate again")

    assert "recursive delegation is disabled" in response
    assert store.list_jobs() == []


@pytest.mark.asyncio
async def test_auto_mode_excludes_caller_and_enforces_parallel_limit(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    orchestration.save_policy(
        orchestration.OrchestrationPolicy(mode="auto", maxParallel=1)
    )
    monkeypatch.setenv("MINDSYNC_CALLER_CLI", "builder")

    decision = await run_task(
        agent="auto",
        prompt="review the code",
        required_capabilities=["review"],
    )
    assert decision["job"]["agent"] != "builder"
    assert "builder" in decision["job"]["routing"]["excludedAgents"]

    store.create_job(
        agent="builder",
        prompt="active",
        cwd=str(tmp_path),
        routing={"agent": "builder"},
    )
    with pytest.raises(RuntimeError, match="limit reached"):
        await run_task(
            agent="auto",
            prompt="review another",
            required_capabilities=["review"],
        )
