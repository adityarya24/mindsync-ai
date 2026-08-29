"""Tests for capability-based automatic dispatch routing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mindsync.dispatch import store
from mindsync.dispatch.adapters import load_adapters, user_config_path
from mindsync.dispatch.cli import parse_run_args
from mindsync.dispatch.routing import infer_capabilities, select_agent
from mindsync.dispatch.runner import run_task
from mindsync.server import delegate_task, list_agents, route_task


def _configure_agents(tmp_path: Path, monkeypatch) -> None:
    from tests.isolation_helpers import isolate_mindsync_home

    isolate_mindsync_home(tmp_path, monkeypatch, codex_home=True)
    monkeypatch.delenv("MINDSYNC_WORKER", raising=False)

    user_config_path().write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "builder",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; print('built:' + sys.stdin.read())"],
                        "capabilities": ["general", "coding", "testing"],
                        "capabilityWeights": {"coding": 100, "testing": 95},
                        "routingPriority": 80,
                    },
                    {
                        "name": "auditor",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; print('audited:' + sys.stdin.read())"],
                        "capabilities": ["general", "review", "security"],
                        "capabilityWeights": {"review": 100, "security": 100},
                        "routingPriority": 70,
                    },
                    {
                        "name": "backup-builder",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; print('backup:' + sys.stdin.read())"],
                        "capabilities": ["general", "coding"],
                        "capabilityWeights": {"coding": 60},
                        "routingPriority": 10,
                    },
                    {
                        "name": "missing-specialist",
                        "bin": "definitely-not-a-real-mindsync-agent",
                        "capabilities": ["security"],
                        "capabilityWeights": {"security": 100},
                        "routingPriority": 100,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def test_infer_capabilities_uses_task_language_without_substring_false_positive():
    assert infer_capabilities("Review the authentication security design") == [
        "architecture",
        "review",
        "security",
    ]
    assert infer_capabilities("make a decision") == ["general"]


def test_select_agent_ranks_installed_workers_by_capability(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)

    decision = select_agent("audit auth", required_capabilities=["security", "review"])

    assert decision["agent"] == "auditor"
    assert decision["matchedCapabilities"] == ["security", "review"]
    assert decision["missingCapabilities"] == []
    assert decision["coverage"] == 1.0
    assert "missing-specialist" in decision["unavailableAgents"]
    assert decision["reason"].startswith("Selected auditor")


def test_select_agent_can_exclude_the_human_facing_orchestrator(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    loaded = load_adapters()
    fixture_adapters = {
        name: loaded[name]
        for name in ("builder", "backup-builder", "auditor", "missing-specialist")
    }

    decision = select_agent(
        "implement and test the fix",
        required_capabilities=["coding"],
        exclude_agents=["builder"],
        adapters=fixture_adapters,
    )

    # The deterministic fallback remains eligible, but the caller is never selected.
    assert decision["agent"] != "builder"
    assert decision["agent"] == "backup-builder"
    assert decision["excludedAgents"] == ["builder"]


def test_select_agent_reports_unmatched_requirements(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="No installed agent matches"):
        select_agent("translate", required_capabilities=["translation"])


@pytest.mark.parametrize(
    "agent, message",
    [
        (
            {
                "name": "bad-weight-name",
                "bin": sys.executable,
                "capabilities": ["coding"],
                "capabilityWeights": {"security": 50},
            },
            "undeclared capabilities",
        ),
        (
            {
                "name": "bad-weight-value",
                "bin": sys.executable,
                "capabilities": ["coding"],
                "capabilityWeights": {"coding": 101},
            },
            "between 0 and 100",
        ),
    ],
)
def test_invalid_capability_weights_fail_at_config_load(tmp_path, monkeypatch, agent, message):
    dispatch_home = tmp_path / "dispatch-home"
    dispatch_home.mkdir()
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(dispatch_home))
    user_config_path().write_text(json.dumps({"agents": [agent]}), encoding="utf-8")

    from mindsync.dispatch.adapters import load_adapters

    with pytest.raises(ValueError, match=message):
        load_adapters()


@pytest.mark.asyncio
async def test_run_task_auto_records_explainable_route(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)

    res = await run_task(
        agent="auto",
        prompt="implement the feature",
        required_capabilities=["coding"],
        cwd=str(tmp_path),
    )

    assert res["job"]["agent"] == "builder"
    assert res["job"]["routing"]["requiredCapabilities"] == ["coding"]
    assert store.get_job(res["job"]["id"])["routing"]["agent"] == "builder"
    assert "built:implement the feature" in res["result"]


@pytest.mark.asyncio
async def test_mcp_defaults_to_auto_and_exposes_preview_and_inventory(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)

    preview = route_task("security review", required_capabilities=["security"])
    assert preview["agent"] == "auditor"

    inventory = list_agents()
    auditor = next(agent for agent in inventory if agent["name"] == "auditor")
    assert auditor["available"] is True
    assert "security" in auditor["capabilities"]

    result = await delegate_task(
        prompt="security review",
        required_capabilities=["security"],
        cwd=str(tmp_path),
    )
    assert "Auto route: Selected auditor" in result
    assert "audited:security review" in result


def test_cli_parses_auto_routing_constraints():
    opts = parse_run_args(
        [
            "auto",
            "implement",
            "and",
            "test",
            "--capability",
            "coding",
            "--capability",
            "testing",
            "--exclude-agent",
            "codex",
        ]
    )

    assert opts["agent"] == "auto"
    assert opts["prompt"] == "implement and test"
    assert opts["required_capabilities"] == ["coding", "testing"]
    assert opts["exclude_agents"] == ["codex"]
