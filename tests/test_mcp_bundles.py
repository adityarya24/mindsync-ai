"""Issue #53 MCP subject-bundle discovery and discriminated validation."""

from __future__ import annotations

import pytest

from mindsync.dispatch import store
from mindsync.server import events, job, list_catalog, session
from tests.isolation_helpers import isolate_mindsync_home


@pytest.mark.asyncio
async def test_job_bundle_rejects_wait_fields_on_status():
    result = await job(action="status", job_id="20260101-abc", timeout_seconds=1.0)
    assert result.startswith("Error:")
    assert "timeout_seconds" in result
    assert "status" in result


@pytest.mark.asyncio
async def test_job_bundle_requires_job_id():
    result = await job(action="cancel")
    assert result.startswith("Error:")
    assert "job_id" in result


def test_events_bundle_rejects_poll_fields_on_publish():
    result = events(
        action="publish",
        agent_name="planner",
        event_type="task.created",
        payload={"ok": True},
        since_seq=1,
    )
    assert result["ok"] is False
    assert "since_seq" in result["error"]


def test_session_bundle_rejects_session_id_on_start():
    result = session(
        action="start",
        agent_name="planner",
        project_key="proj",
        session_id="should-not-be-here",
    )
    assert result["ok"] is False
    assert "session_id" in result["error"]


def test_list_bundle_rejects_agent_filter_on_agents():
    result = list_catalog(kind="agents", agent="codex")
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "agent" in result


@pytest.mark.asyncio
async def test_job_bundle_wait_dispatches_to_existing_helper(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    created = store.create_job(agent="builder", prompt="do work", cwd=str(tmp_path))
    store.update_job(created["id"], {"status": "done"})
    result = await job(action="wait", job_id=created["id"], timeout_seconds=1.0)
    assert "Completion ping" in result
    assert created["id"] in result
