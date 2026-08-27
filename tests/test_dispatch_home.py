"""Dispatch roster/jobs live under the MindSync home, not Claude's folder."""

from __future__ import annotations

from mindsync.config import dispatch_home, migrate_legacy_dispatch_home
from mindsync.dispatch.adapters import user_config_path
from mindsync.dispatch.store import jobs_root


def test_dispatch_home_uses_mindsync_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "ms"))
    monkeypatch.delenv("AGENT_DISPATCH_HOME", raising=False)
    assert dispatch_home() == tmp_path / "ms" / "dispatch"
    assert user_config_path() == tmp_path / "ms" / "dispatch" / "agents.json"
    assert jobs_root() == tmp_path / "ms" / "dispatch" / "jobs"


def test_agent_dispatch_home_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "ms"))
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(tmp_path / "legacy-override"))
    assert dispatch_home() == tmp_path / "legacy-override"
    assert jobs_root() == tmp_path / "legacy-override" / "jobs"


def test_migrate_copies_missing_files_and_keeps_new_ones(tmp_path):
    legacy = tmp_path / "claude-dispatch"
    target = tmp_path / "mindsync-dispatch"
    (legacy / "jobs").mkdir(parents=True)
    (legacy / "agents.json").write_text('{"agents":[{"name":"vidur"}]}', encoding="utf-8")
    (legacy / "jobs" / "old.json").write_text('{"id":"old"}', encoding="utf-8")

    (target / "jobs").mkdir(parents=True)
    (target / "agents.json").write_text('{"agents":[{"name":"keep"}]}', encoding="utf-8")

    assert migrate_legacy_dispatch_home(target, legacy=legacy) is True
    assert (target / "agents.json").read_text(encoding="utf-8") == '{"agents":[{"name":"keep"}]}'
    assert (target / "jobs" / "old.json").read_text(encoding="utf-8") == '{"id":"old"}'


def test_isolated_mindsync_home_does_not_copy_real_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "ms"))
    monkeypatch.delenv("AGENT_DISPATCH_HOME", raising=False)
    dispatch_home()
    assert not (tmp_path / "ms" / "dispatch").exists()
