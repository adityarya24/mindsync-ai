"""Public MCP and persistence boundaries reject removed paid features."""

import pytest

from mindsync import server
from mindsync.dispatch import store


@pytest.mark.asyncio
async def test_mcp_handoff_rejection_does_not_launch(monkeypatch):
    async def unexpected_launch(**kwargs):
        pytest.fail("paid handoff must be rejected before dispatch")

    monkeypatch.setattr(server, "dispatch_run_task", unexpected_launch)
    result = await server.delegate_task(prompt="test", on_limit="handoff")
    assert result == "Error: quota handoff is part of MindSync Pro"


def test_store_rejects_handoff_before_creating_job(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="quota handoff is part of MindSync Pro"):
        store.create_job(agent="codex", prompt="test", cwd=str(tmp_path), on_limit="handoff")
    assert list(tmp_path.iterdir()) == []


def test_stored_pr_mode_falls_back_and_warns_once(tmp_path, monkeypatch, capsys):
    import json
    from mindsync import config, orchestration
    from tests.isolation_helpers import isolate_mindsync_home

    home = isolate_mindsync_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_IGNORED_PRO_KEYS", set())
    (home / "orchestration.json").write_text(
        json.dumps({"onComplete": "pr", "projects": {"repo": {"onComplete": "pr"}}}),
        encoding="utf-8",
    )
    for _ in range(2):
        policy = orchestration.load_policy()
        assert policy.onComplete == "none"
        assert policy.projects["repo"].onComplete == "none"
    assert capsys.readouterr().err == (
        "ignoring onComplete: Automated pull request workflow is part of MindSync Pro\n"
    )


def test_worker_environment_does_not_block_doctor(monkeypatch, capsys):
    from mindsync import config, manage

    monkeypatch.setattr(config, "_IGNORED_PRO_KEYS", set())
    monkeypatch.setenv("MINDSYNC_WORKER_ID", "legacy")
    monkeypatch.setattr(manage, "doctor", lambda **kwargs: {"ok": True})
    assert manage.main(["doctor", "--json", "--no-probe"]) == 0
    assert "ignoring MINDSYNC_WORKER_ID:" in capsys.readouterr().err
