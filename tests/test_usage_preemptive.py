"""Pre-emptive usage polling and checkpoint-gated handoff tests."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mindsync.dispatch.routing as routing_mod
import mindsync.dispatch.runner as runner_mod
from mindsync.dispatch.adapters import AdapterConfig, user_config_path
from mindsync.dispatch.cli import fmt_job
from mindsync.dispatch.limits import clear_cooldowns, list_cooldowns, mark_cooling_until
from mindsync.dispatch.routing import select_agent
from mindsync.dispatch.usage.preemptive import preflight_skip_reason
from mindsync.dispatch.usage.registry import evaluate_adapter_threshold
from mindsync.dispatch.usage.types import UsageReadResult, UsageWindow
from mindsync.dispatch import store
from mindsync.dispatch.runner import cancel_job, run_task
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


def _usage_agents_config(*, usage_enabled: bool = True) -> dict:
    return {
        "usage": {
            "enabled": usage_enabled,
            "defaultThresholdPercent": 90,
            "pollingIntervalSeconds": 5,
        },
        "agents": [
            {
                "name": "primary",
                "bin": sys.executable,
                "input": "stdin",
                "capabilities": ["coding"],
                "capabilityWeights": {"coding": 100},
                "routingPriority": 100,
                "quotaScope": "provider:account-a",
                "usageReader": "codex-oauth",
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
                "usageReader": "codex-oauth",
            },
        ],
    }


def _write_usage_agents(
    tmp_path: Path,
    monkeypatch,
    *,
    usage_enabled: bool = True,
    extra: dict | None = None,
) -> None:
    _isolate_dispatch(tmp_path, monkeypatch)
    config = _usage_agents_config(usage_enabled=usage_enabled)
    if extra:
        config.update(extra)
    user_config_path().write_text(json.dumps(config), encoding="utf-8")

    def only_python(value: str) -> str | None:
        return sys.executable if value == sys.executable else None

    monkeypatch.setattr(runner_mod, "resolve_bin", only_python)
    monkeypatch.setattr(routing_mod, "resolve_bin", only_python)


def _at_threshold_result(adapter_name: str, scope: str) -> UsageReadResult:
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    return UsageReadResult.available(
        provider=adapter_name,
        account_scope=scope,
        reader="codex-oauth",
        source="test",
        windows=[
            UsageWindow(
                id="primary",
                label="Primary",
                used_percent=95.0,
                reset_at=reset,
            )
        ],
    )


def _below_threshold_result(adapter_name: str, scope: str) -> UsageReadResult:
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    return UsageReadResult.available(
        provider=adapter_name,
        account_scope=scope,
        reader="codex-oauth",
        source="test",
        windows=[
            UsageWindow(
                id="primary",
                label="Primary",
                used_percent=10.0,
                reset_at=reset,
            )
        ],
    )


@pytest.mark.asyncio
async def test_usage_disabled_never_calls_reader(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch, usage_enabled=False)
    calls = 0

    def fake_evaluate(adapter, **kwargs):
        nonlocal calls
        calls += 1
        return evaluate_adapter_threshold(adapter, **kwargs)

    monkeypatch.setattr(runner_mod, "evaluate_adapter_threshold", fake_evaluate)

    async def fake_spawn(*args, **kwargs):
        assert kwargs.get("poll_interval_seconds") is None
        assert kwargs.get("poll_callback") is None
        return {
            "stdout": "ok",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
            "preemptiveThreshold": False,
        }

    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="primary",
        prompt="implement",
        cwd=str(fake_repo),
        worktree=True,
        on_limit="handoff",
        memory_mode="off",
    )

    assert result["job"]["status"] == "done"
    assert calls == 0


@pytest.mark.asyncio
async def test_on_limit_stop_never_polls_or_skips(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch, usage_enabled=True)
    monkeypatch.setattr(
        runner_mod,
        "evaluate_adapter_threshold",
        lambda adapter, **kwargs: _at_threshold_result(
            adapter.name, adapter.quotaScope or f"agent:{adapter.name}"
        ),
    )

    async def fake_spawn(*args, **kwargs):
        assert kwargs.get("poll_interval_seconds") is None
        return {
            "stdout": "ok",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
            "preemptiveThreshold": False,
        }

    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="primary",
        prompt="implement",
        cwd=str(fake_repo),
        worktree=True,
        on_limit="stop",
        memory_mode="off",
    )

    assert result["job"]["status"] == "done"
    assert result["job"].get("usageSkips") in (None, [])


@pytest.mark.asyncio
async def test_preflight_skips_over_threshold_provider(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch)

    def fake_evaluate(adapter, **kwargs):
        scope = adapter.quotaScope or f"agent:{adapter.name}"
        if adapter.name == "primary":
            return evaluate_adapter_threshold(
                adapter,
                result=_at_threshold_result(adapter.name, scope),
            )
        return evaluate_adapter_threshold(
            adapter, result=_below_threshold_result(adapter.name, scope)
        )

    monkeypatch.setattr(runner_mod, "evaluate_adapter_threshold", fake_evaluate)
    calls: list[str] = []

    async def fake_spawn(*args, **kwargs):
        calls.append(kwargs.get("input_text") or "")
        return {
            "stdout": "ok",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
            "preemptiveThreshold": False,
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
    assert job["agent"] == "backup"
    assert any(row["agent"] == "primary" for row in job.get("usageSkips") or [])
    assert "usage skip: primary" in fmt_job(job)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_running_threshold_without_checkpoint_does_not_kill(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch)
    poll_count = 0
    threshold_seen = False

    async def fake_spawn(*args, **kwargs):
        nonlocal poll_count, threshold_seen
        callback = kwargs.get("poll_callback")
        assert kwargs.get("poll_interval_seconds") == 5.0
        assert callback is not None
        for _ in range(3):
            poll_count += 1
            action = await callback()
            if action == "blocked":
                threshold_seen = True
        return {
            "stdout": "ok",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
            "preemptiveThreshold": False,
        }

    def fake_evaluate(adapter, **kwargs):
        scope = adapter.quotaScope or f"agent:{adapter.name}"
        if poll_count >= 2:
            return evaluate_adapter_threshold(
                adapter,
                result=_at_threshold_result(adapter.name, scope),
            )
        return evaluate_adapter_threshold(
            adapter, result=_below_threshold_result(adapter.name, scope)
        )

    monkeypatch.setattr(runner_mod, "evaluate_adapter_threshold", fake_evaluate)
    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="primary",
        prompt="implement",
        cwd=str(fake_repo),
        worktree=True,
        on_limit="handoff",
        memory_mode="off",
    )

    job = result["job"]
    assert job["status"] == "done"
    assert threshold_seen
    assert job.get("preemptiveBlocked") == "no memory session checkpoint available"
    assert len(job.get("attempts") or []) == 1


@pytest.mark.asyncio
async def test_running_threshold_with_checkpoint_transfers_once(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch)
    poll_count = 0
    handoff_triggered = False

    monkeypatch.setattr(
        runner_mod,
        "has_usable_checkpoint",
        lambda meta: (True, None),
    )

    async def fake_spawn(*args, **kwargs):
        nonlocal handoff_triggered
        callback = kwargs.get("poll_callback")
        assert callback is not None
        action = await callback()
        if action == "handoff":
            handoff_triggered = True
            return {
                "stdout": "partial",
                "stderr": "",
                "exitCode": -1,
                "timedOut": False,
                "processTreeDead": True,
                "preemptiveThreshold": True,
            }
        return {
            "stdout": "done",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
            "preemptiveThreshold": False,
        }

    def fake_evaluate(adapter, **kwargs):
        nonlocal poll_count
        poll_count += 1
        scope = adapter.quotaScope or f"agent:{adapter.name}"
        if adapter.name == "primary" and poll_count >= 1:
            return evaluate_adapter_threshold(
                adapter,
                result=_at_threshold_result(adapter.name, scope),
            )
        return evaluate_adapter_threshold(
            adapter, result=_below_threshold_result(adapter.name, scope)
        )

    monkeypatch.setattr(runner_mod, "evaluate_adapter_threshold", fake_evaluate)
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
    assert handoff_triggered
    assert job["status"] == "done"
    assert [(row["agent"], row["status"]) for row in job["attempts"]] == [
        ("primary", "usage_threshold"),
        ("backup", "done"),
    ]
    assert any(row["reason"] == "usage_threshold" for row in job.get("handoffs") or [])
    assert list_cooldowns()[0]["scope"] == "provider:account-a"
    clear_cooldowns()


@pytest.mark.asyncio
async def test_reader_failure_degrades_to_reactive_handoff(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch)
    calls = 0

    def fake_evaluate(adapter, **kwargs):
        nonlocal calls
        calls += 1
        scope = adapter.quotaScope or f"agent:{adapter.name}"
        return evaluate_adapter_threshold(
            adapter,
            result=UsageReadResult.unavailable(
                provider=adapter.name,
                account_scope=scope,
                reason="usage reader failed",
            ),
        )

    monkeypatch.setattr(runner_mod, "evaluate_adapter_threshold", fake_evaluate)

    async def fake_spawn(*args, **kwargs):
        if calls <= 1:
            return {
                "stdout": "",
                "stderr": "Usage window exhausted; resets at 17:00",
                "exitCode": 1,
                "timedOut": False,
                "processTreeDead": True,
                "preemptiveThreshold": False,
            }
        return {
            "stdout": "continued",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
            "preemptiveThreshold": False,
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


def test_two_accounts_same_scope_share_usage_cooldown(tmp_path, monkeypatch):
    _write_usage_agents(tmp_path, monkeypatch)
    shared_a = AdapterConfig(
        name="shared-a",
        bin=sys.executable,
        capabilities=["coding"],
        quotaScope="provider:shared",
        usageReader="codex-oauth",
        routingPriority=100,
    )
    shared_b = AdapterConfig(
        name="shared-b",
        bin=sys.executable,
        capabilities=["coding"],
        quotaScope="provider:shared",
        usageReader="codex-oauth",
        routingPriority=90,
    )
    other = AdapterConfig(
        name="other",
        bin=sys.executable,
        capabilities=["coding"],
        quotaScope="provider:other",
        usageReader="codex-oauth",
        routingPriority=10,
    )
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    mark_cooling_until(shared_a, until, reason="usage threshold reached")

    decision = select_agent(
        "implement",
        required_capabilities=["coding"],
        adapters={row.name: row for row in (shared_a, shared_b, other)},
        usage_aware=True,
        on_limit="handoff",
    )

    assert decision["agent"] == "other"
    assert preflight_skip_reason(shared_a) is not None
    assert preflight_skip_reason(shared_b) is not None
    clear_cooldowns()


@pytest.mark.asyncio
async def test_cancel_during_poll_does_not_transfer(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch)
    job_id_holder: list[str] = []

    async def fake_spawn(*args, **kwargs):
        callback = kwargs.get("poll_callback")
        job_id_holder.append(store.list_jobs()[0]["id"])
        await asyncio.sleep(0.05)
        if callback is not None:
            await callback()
        return {
            "stdout": "",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
            "processTreeDead": True,
            "preemptiveThreshold": False,
        }

    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)

    async def run_and_cancel():
        task = asyncio.create_task(
            run_task(
                agent="primary",
                prompt="implement",
                cwd=str(fake_repo),
                worktree=True,
                on_limit="handoff",
                memory_mode="off",
                background=True,
            )
        )
        await asyncio.sleep(0.01)
        while not job_id_holder:
            await asyncio.sleep(0.01)
        cancel_job(job_id_holder[0])
        return await task

    result = await run_and_cancel()
    job = result["job"]
    assert job["status"] == "cancelled"
    assert job.get("handoffs") in (None, [])


@pytest.mark.asyncio
async def test_no_successor_retains_work_on_preflight(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch)
    config = json.loads(user_config_path().read_text(encoding="utf-8"))
    config["agents"] = [row for row in config["agents"] if row["name"] == "primary"]
    user_config_path().write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(
        runner_mod,
        "evaluate_adapter_threshold",
        lambda adapter, **kwargs: evaluate_adapter_threshold(
            adapter,
            result=_at_threshold_result(
                adapter.name, adapter.quotaScope or f"agent:{adapter.name}"
            ),
        ),
    )

    result = await run_task(
        agent="primary",
        prompt="implement",
        cwd=str(fake_repo),
        worktree=True,
        on_limit="handoff",
        memory_mode="off",
    )

    job = result["job"]
    assert job["status"] == "failed"
    assert "no successor available" in job["handoffBlocked"]
    assert job.get("worktreeKept") is True


@pytest.mark.asyncio
async def test_process_tree_must_be_dead_before_preemptive_transfer(
    fake_repo: Path, tmp_path: Path, monkeypatch
):
    _write_usage_agents(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runner_mod,
        "has_usable_checkpoint",
        lambda meta: (True, None),
    )

    async def fake_spawn(*args, **kwargs):
        callback = kwargs.get("poll_callback")
        if callback is not None:
            await callback()
        return {
            "stdout": "",
            "stderr": "",
            "exitCode": -1,
            "timedOut": False,
            "processTreeDead": False,
            "preemptiveThreshold": True,
        }

    monkeypatch.setattr(
        runner_mod,
        "evaluate_adapter_threshold",
        lambda adapter, **kwargs: evaluate_adapter_threshold(
            adapter,
            result=_at_threshold_result(
                adapter.name, adapter.quotaScope or f"agent:{adapter.name}"
            ),
        ),
    )
    monkeypatch.setattr(runner_mod, "spawn_foreground", fake_spawn)
    result = await run_task(
        agent="primary",
        prompt="implement",
        cwd=str(fake_repo),
        worktree=True,
        on_limit="handoff",
        memory_mode="off",
    )

    job = result["job"]
    assert job["status"] == "failed"
    assert "process tree" in job["handoffBlocked"]
    assert len(job.get("handoffs") or []) == 0
