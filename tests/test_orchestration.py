"""Tests for persistent orchestration policy and dispatch enforcement."""

from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.orchestration as orchestration
from mindsync.dispatch import store
from mindsync.dispatch.adapters import user_config_path
from mindsync.dispatch.runner import (
    AutoDelegationDisabled,
    AutoDelegationSuggestion,
    _create_job_with_auto_limit,
    run_task,
)
from mindsync.server import delegate_task


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    from tests.isolation_helpers import isolate_mindsync_home

    isolate_mindsync_home(tmp_path, monkeypatch)
    monkeypatch.delenv("MINDSYNC_CALLER_CLI", raising=False)
    settings = config_mod.settings
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
    assert orchestration.normalize_client_name("Antigravity (Gemini)") == "agy"


def test_auto_admission_check_and_reservation_are_atomic(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def admit() -> None:
        barrier.wait()
        try:
            _create_job_with_auto_limit(
                max_parallel=1,
                agent="builder",
                prompt="bounded work",
                cwd=str(tmp_path),
                routing={"agent": "builder"},
            )
            outcomes.append("admitted")
        except RuntimeError as exc:
            assert "limit reached" in str(exc)
            outcomes.append("rejected")

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["admitted", "rejected"]
    assert store.count_active_auto_jobs() == 1


def test_active_auto_index_avoids_rescanning_completed_history(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    completed = store.create_job(agent="builder", prompt="old", cwd=str(tmp_path))
    store.update_job(completed["id"], {"status": "done"})
    assert store.count_active_auto_jobs() == 0  # one-time legacy-index initialization

    active = store.create_job(
        agent="builder",
        prompt="active",
        cwd=str(tmp_path),
        routing={"agent": "builder"},
    )
    monkeypatch.setattr(store, "list_jobs", lambda: (_ for _ in ()).throw(AssertionError("history scan")))

    assert store.count_active_auto_jobs() == 1
    store.update_job(active["id"], {"status": "done"})
    assert store.count_active_auto_jobs() == 0


def test_worker_process_receives_non_recursive_instructions(monkeypatch):
    monkeypatch.setenv("MINDSYNC_WORKER", "1")
    instructions = orchestration.server_instructions()
    assert "delegated worker" in instructions
    assert "Do not call delegate_task" in instructions


def test_orchestrator_instructions_require_completion_wait(monkeypatch):
    monkeypatch.delenv("MINDSYNC_WORKER", raising=False)
    instructions = orchestration.server_instructions(
        orchestration.OrchestrationPolicy(mode="auto")
    )
    # Assert the intent, not the wording: the orchestrator must be told to wait
    # for delegated work, and must not be told to wait in a way that serialises
    # jobs the very next sentence says to run concurrently.
    assert "job_wait" in instructions
    assert "Do not end the turn while delegated work is still running" in instructions
    assert "concurrently" in instructions
    assert "immediately call job_wait" not in instructions


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


def test_on_complete_is_settable_and_scoped_to_one_project(tmp_path, monkeypatch):
    """Issue #41 promised per-project on_complete, and the field had no setter.

    A global default plus one project that opts in has to be expressible, or
    turning publishing on for a single repository means turning it on for all.
    """
    _isolate(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    assert orchestration.update_policy("onComplete", "none").onComplete == "none"
    assert (
        orchestration.update_policy("orchestration.onComplete", "branch").onComplete
        == "branch"
    )

    policy = orchestration.update_policy("onComplete", "pr", project=repo)
    assert policy.onComplete == "branch"  # the global default is untouched
    assert orchestration.project_on_complete(repo) == "pr"
    assert orchestration.project_on_complete(other) == "branch"
    assert orchestration.project_on_complete(None) == "branch"


def test_a_project_override_survives_a_reload(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    orchestration.update_policy("onComplete", "pr", project=repo)

    assert orchestration.project_on_complete(repo, orchestration.load_policy()) == "pr"


def test_one_repository_cannot_become_two_entries(tmp_path, monkeypatch):
    """A symlinked or unresolved path must key to the same project."""
    _isolate(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(repo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    orchestration.update_policy("onComplete", "pr", project=link)

    assert orchestration.project_on_complete(repo) == "pr"
    assert len(orchestration.load_policy().projects) == 1


def test_settings_without_a_project_meaning_are_refused_per_project(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="cannot be set per project"):
        orchestration.update_policy("maxParallel", 4, project=tmp_path)


def test_an_unknown_setting_still_names_what_is_allowed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="onComplete"):
        orchestration.update_policy("nonsense", "pr")
