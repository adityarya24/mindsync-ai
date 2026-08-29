"""Completion-wait behavior for delegated background jobs."""

from __future__ import annotations

import asyncio

import pytest

import mindsync.config as config_mod
import mindsync.server as server
import mindsync.storage as storage
from mindsync.dispatch import store


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(tmp_path / "dispatch-home"))
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "mindsync-home"))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    server.settings = config_mod.settings
    config_mod.settings.ensure_dirs()


@pytest.mark.asyncio
async def test_job_wait_returns_completion_ping_for_finished_job(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    job = store.create_job(agent="builder", prompt="do work", cwd=str(tmp_path))
    store.update_job(
        job["id"],
        {"status": "done", "exitCode": 0, "endedAt": store.utc_now()},
    )

    result = await server.job_wait(job["id"], timeout_seconds=1)

    assert "Completion ping" in result
    assert job["id"] in result
    assert "'done'" in result


@pytest.mark.asyncio
async def test_job_wait_resumes_after_running_job_finishes(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    job = store.create_job(agent="builder", prompt="do work", cwd=str(tmp_path))

    waiter = asyncio.create_task(
        server.job_wait(
            job["id"],
            timeout_seconds=1,
            poll_interval_seconds=0.1,
        )
    )
    await asyncio.sleep(0.02)
    assert not waiter.done()
    store.update_job(
        job["id"],
        {"status": "failed", "exitCode": 1, "endedAt": store.utc_now()},
    )

    result = await waiter

    assert "Completion ping" in result
    assert "'failed'" in result


@pytest.mark.asyncio
async def test_job_wait_timeout_keeps_watch_instruction(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    job = store.create_job(agent="builder", prompt="do work", cwd=str(tmp_path))

    result = await server.job_wait(
        job["id"],
        timeout_seconds=0.01,
        poll_interval_seconds=0.1,
    )

    assert "still pending" in result
    assert "Call job(action='wait') again" in result

