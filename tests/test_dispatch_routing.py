"""Tests for capability-based automatic dispatch routing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mindsync.dispatch import store
from mindsync.dispatch.adapters import AdapterConfig, load_adapters, user_config_path
from mindsync.dispatch.cli import parse_run_args
from mindsync.dispatch.routing import infer_capabilities, select_agent
from mindsync.dispatch.runner import run_task
from mindsync.dispatch.usage.config import UsageConfig
from mindsync.dispatch.usage.types import ThresholdEvaluation, UsageWindow
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


def _quota_pair():
    codex = AdapterConfig(
        name="codex",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=100,
        usageReader="codex-oauth",
        quotaScope="openai:default",
    )
    grok = AdapterConfig(
        name="grok",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=70,
        usageReader="grok-oauth",
        quotaScope="xai:default",
    )
    return {"codex": codex, "grok": grok}


def _eval(name: str, percents: list[float], *, status: str = "below_threshold") -> ThresholdEvaluation:
    windows = [
        UsageWindow(id=f"w{index}", label=f"w{index}", used_percent=percent)
        for index, percent in enumerate(percents)
    ]
    return ThresholdEvaluation(
        status=status,
        threshold_percent=90,
        provider=name,
        account_scope=f"{name}:default",
        windows=windows,
    )


def _evaluator(mapping: dict[str, ThresholdEvaluation]):
    def evaluate(adapter, **_kwargs):
        return mapping[adapter.name]

    return evaluate


def test_headroom_picks_fresh_grok_over_hot_codex(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    decision = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters=_quota_pair(),
        usage_config=UsageConfig(enabled=True),
        evaluator=_evaluator({"codex": _eval("codex", [89]), "grok": _eval("grok", [4])}),
    )
    assert decision["agent"] == "grok"
    assert decision["usedPercent"] == 4
    assert decision["headroomBonus"] == 46
    by_name = {row["agent"]: row for row in decision["candidates"]}
    assert by_name["codex"]["usedPercent"] == 89
    assert by_name["codex"]["headroomBonus"] == -39
    assert "4% used vs codex 89%" in decision["reason"]


def test_small_headroom_gap_does_not_overturn_routing_priority(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    decision = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters=_quota_pair(),
        usage_config=UsageConfig(enabled=True),
        evaluator=_evaluator({"codex": _eval("codex", [2]), "grok": _eval("grok", [4])}),
    )
    assert decision["agent"] == "codex"
    assert decision["usedPercent"] == 2


def test_capability_match_still_beats_a_fresher_unqualified_agent(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    capable = AdapterConfig(
        name="codex",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=70,
        usageReader="codex-oauth",
    )
    fresher = AdapterConfig(
        name="fresh-coder",
        bin=sys.executable,
        capabilities=["coding"],
        capabilityWeights={"coding": 100},
        routingPriority=100,
        usageReader="codex-oauth",
    )
    decision = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters={"codex": capable, "fresh-coder": fresher},
        usage_config=UsageConfig(enabled=True),
        evaluator=_evaluator(
            {
                "codex": _eval("codex", [80]),
                "fresh-coder": _eval("fresh-coder", [1]),
            }
        ),
    )
    assert decision["agent"] == "codex"
    assert all(row["agent"] != "fresh-coder" for row in decision["candidates"])


def test_unavailable_readers_keep_priority_order_when_all_readers_unavailable(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    decision = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters=_quota_pair(),
        usage_config=UsageConfig(enabled=True),
        evaluator=_evaluator(
            {
                "codex": _eval("codex", [], status="unavailable"),
                "grok": _eval("grok", [], status="unavailable"),
            }
        ),
    )
    assert decision["agent"] == "codex"
    assert decision["usedPercent"] is None
    assert decision["headroomBonus"] == 0
    assert decision["candidates"][1]["agent"] == "grok"
    assert "used vs" not in decision["reason"]


def test_usage_disabled_ranking_matches_today(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    adapters = _quota_pair()
    noisy = _evaluator({"codex": _eval("codex", [89]), "grok": _eval("grok", [4])})
    baseline = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters=adapters,
        usage_config=UsageConfig(enabled=False),
    )
    disabled = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters=adapters,
        usage_config=UsageConfig(enabled=False),
        evaluator=noisy,
    )
    assert disabled["agent"] == baseline["agent"] == "codex"
    assert disabled["score"] == baseline["score"]
    assert [row["score"] for row in disabled["candidates"]] == [
        row["score"] for row in baseline["candidates"]
    ]


def test_hottest_window_not_average_decides_headroom(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    hot_weekly = AdapterConfig(
        name="hot-weekly",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=80,
        usageReader="codex-oauth",
    )
    steady = AdapterConfig(
        name="steady",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=80,
        usageReader="grok-oauth",
    )
    decision = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters={"hot-weekly": hot_weekly, "steady": steady},
        usage_config=UsageConfig(enabled=True),
        evaluator=_evaluator(
            {
                "hot-weekly": _eval("hot-weekly", [0, 95]),
                "steady": _eval("steady", [40]),
            }
        ),
    )
    assert decision["agent"] == "steady"
    assert decision["usedPercent"] == 40
    by_name = {row["agent"]: row for row in decision["candidates"]}
    assert by_name["hot-weekly"]["usedPercent"] == 95


def test_handoff_successor_uses_the_same_headroom_ranking(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    decision = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters=_quota_pair(),
        exclude_agents=["codex"],
        usage_config=UsageConfig(enabled=True),
        usage_aware=True,
        on_limit="handoff",
        evaluator=_evaluator({"codex": _eval("codex", [89]), "grok": _eval("grok", [4])}),
    )
    assert decision["agent"] == "grok"
    assert decision["excludedAgents"] == ["codex"]
    assert decision["usedPercent"] == 4


def test_unread_adapter_is_not_penalized_against_a_readable_burned_one(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    claude = AdapterConfig(
        name="claude",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=80,
        usageReader="claude-oauth",
        quotaScope="anthropic:default",
    )
    aider = AdapterConfig(
        name="aider",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=70,
    )
    decision = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters={"claude": claude, "aider": aider},
        usage_config=UsageConfig(enabled=True),
        evaluator=_evaluator(
            {
                "claude": _eval("claude", [85]),
                "aider": _eval("aider", [], status="unavailable"),
            }
        ),
    )
    assert decision["agent"] == "aider"
    by_name = {row["agent"]: row for row in decision["candidates"]}
    assert by_name["aider"]["usedPercent"] is None
    assert by_name["aider"]["headroomBonus"] == 0
    assert by_name["claude"]["usedPercent"] == 85
    assert by_name["claude"]["headroomBonus"] == -35


def test_cursor_opt_out_does_not_demote_when_usage_enabled(tmp_path, monkeypatch):
    _configure_agents(tmp_path, monkeypatch)
    cursor = AdapterConfig(
        name="cursor",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=80,
        usageReader="cursor-oauth",
        quotaScope="cursor:default",
    )
    claude = AdapterConfig(
        name="claude",
        bin=sys.executable,
        capabilities=["general"],
        capabilityWeights={"general": 50},
        routingPriority=70,
        usageReader="claude-oauth",
        quotaScope="anthropic:default",
    )
    adapters = {"cursor": cursor, "claude": claude}
    evaluator = _evaluator(
        {
            "cursor": _eval("cursor", [], status="unavailable"),
            "claude": _eval("claude", [85]),
        }
    )
    off = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters=adapters,
        usage_config=UsageConfig(enabled=False),
        evaluator=evaluator,
    )
    on = select_agent(
        "do the work",
        required_capabilities=["general"],
        adapters=adapters,
        usage_config=UsageConfig(enabled=True, readers={"cursor": False}),
        evaluator=evaluator,
    )
    assert off["agent"] == "cursor"
    assert on["agent"] == "cursor"
    assert [row["agent"] for row in on["candidates"]] == [row["agent"] for row in off["candidates"]]
    cursor_row = next(row for row in on["candidates"] if row["agent"] == "cursor")
    assert cursor_row["headroomBonus"] == 0
    assert cursor_row["usedPercent"] is None
