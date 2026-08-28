"""Reactive provider-quota handoff tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import mindsync.dispatch.routing as routing_mod
import mindsync.dispatch.runner as runner_mod
from mindsync.dispatch.adapters import AdapterConfig, user_config_path
from mindsync.dispatch.cli import fmt_job, parse_run_args
from mindsync.dispatch.limits import classify_quota_exhaustion, mark_cooling
from mindsync.dispatch.routing import select_agent
from mindsync.dispatch.runner import run_task
from mindsync.dispatch.runner import _reactive_handoff_prompt
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


def test_successor_prompt_uses_public_task_not_injected_agent_prompt(monkeypatch):
    monkeypatch.setattr(
        "mindsync.memory.memory_show",
        lambda session_id: {
            "checkpoints": [
                {"pending": ["finish tests"], "blockers": ["quota"]}
            ]
        },
    )
    prompt = _reactive_handoff_prompt(
        {
            "taskPrompt": "implement the feature",
            "prompt": "SECRET INJECTED MEMORY",
            "memorySessionId": "a" * 32,
        },
        "primary",
    )

    assert "implement the feature" in prompt
    assert "finish tests" in prompt
    assert "SECRET INJECTED MEMORY" not in prompt


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

    decision = select_agent(
        "implement", required_capabilities=["coding"],
        adapters={row.name: row for row in (shared_a, shared_b, other)},
    )

    assert decision["agent"] == "other"
    assert {"shared-a", "shared-b"}.issubset(decision["unavailableAgents"])


@pytest.mark.asyncio
async def test_quota_failure_transfers_same_worktree_to_successor(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_agents(tmp_path, monkeypatch)
    calls: list[tuple[str, str]] = []

    async def fake_spawn(*args, **kwargs):
        cwd = Path(kwargs["cwd"])
        prompt = kwargs["input_text"] or ""
        calls.append((str(cwd), prompt))
        if len(calls) == 1:
            (cwd / "partial.txt").write_text("preserve me", encoding="utf-8")
            return {
                "stdout": "",
                "stderr": "Usage window exhausted; resets at 17:00",
                "exitCode": 1,
                "timedOut": False,
                "processTreeDead": True,
            }
        assert (cwd / "partial.txt").read_text(encoding="utf-8") == "preserve me"
        return {
            "stdout": "continued",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
        }

    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="auto",
        prompt="implement the feature",
        required_capabilities=["coding"],
        cwd=str(fake_repo),
        worktree=True,
        on_limit="handoff",
        memory_mode="off",
    )

    job = result["job"]
    assert job["status"] == "done"
    assert [(row["agent"], row["status"]) for row in job["attempts"]] == [
        ("primary", "quota_exhausted"),
        ("backup", "done"),
    ]
    assert calls[0][0] == calls[1][0] == job["worktreePath"]
    assert "Continue the same job" in calls[1][1]
    assert job["worktreeLease"]["agent"] == "backup"
    assert job["worktreeKept"] is True
    assert "primary -> backup" in fmt_job(job)


@pytest.mark.asyncio
async def test_generic_failure_never_rotates(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_agents(tmp_path, monkeypatch)
    calls = 0

    async def fake_spawn(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "stdout": "",
            "stderr": "rate limit exceeded",
            "exitCode": 1,
            "timedOut": False,
            "processTreeDead": True,
        }

    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="auto", prompt="implement the feature",
        required_capabilities=["coding"], cwd=str(fake_repo), worktree=True,
        on_limit="handoff", memory_mode="off",
    )

    assert result["job"]["status"] == "failed"
    assert len(result["job"]["attempts"]) == calls == 1
    assert result["job"]["handoffs"] == []


@pytest.mark.asyncio
async def test_handoff_stops_when_process_tree_is_not_confirmed_dead(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_agents(tmp_path, monkeypatch)

    async def fake_spawn(*args, **kwargs):
        return {
            "stdout": "",
            "stderr": "Usage window exhausted; resets at 17:00",
            "exitCode": 1,
            "timedOut": False,
            "processTreeDead": False,
        }

    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="auto", prompt="implement the feature",
        required_capabilities=["coding"], cwd=str(fake_repo), worktree=True,
        on_limit="handoff", memory_mode="off",
    )

    assert result["job"]["status"] == "failed"
    assert len(result["job"]["attempts"]) == 1
    assert "process tree" in result["job"]["handoffBlocked"]


def test_cli_parses_on_limit_and_handoff_requires_worktree():
    parsed = parse_run_args(["auto", "do", "work", "--worktree", "--on-limit", "handoff"])
    assert parsed["on_limit"] == "handoff"
    assert parsed["worktree"] is True


@pytest.mark.asyncio
async def test_handoff_without_worktree_is_rejected():
    with pytest.raises(ValueError, match="requires worktree"):
        await run_task(agent="codex", prompt="x", on_limit="handoff")
