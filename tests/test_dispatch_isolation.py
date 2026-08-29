"""Regression tests for hermetic dispatch/orchestration policy isolation."""

from __future__ import annotations

import json
import sys

import pytest

import mindsync.orchestration as orchestration
from mindsync.dispatch.adapters import user_config_path
from mindsync.dispatch.runner import run_task
from tests.isolation_helpers import isolate_mindsync_home


def _configure_agents(tmp_path, monkeypatch) -> None:
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
                        "capabilities": ["general", "coding"],
                        "capabilityWeights": {"coding": 100},
                        "routingPriority": 100,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_isolated_auto_routing_ignores_live_suggest_policy(tmp_path, monkeypatch):
    """Temp home must not inherit the operator's live orchestration mode."""
    _configure_agents(tmp_path, monkeypatch)
    assert orchestration.load_policy().mode == "auto"

    res = await run_task(
        agent="auto",
        prompt="implement the feature",
        required_capabilities=["coding"],
        cwd=str(tmp_path),
    )

    assert res["job"]["agent"] == "builder"
    assert "built:implement the feature" in res["result"]
