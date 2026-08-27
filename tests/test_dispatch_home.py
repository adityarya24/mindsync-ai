"""Dispatch roster/jobs live under the MindSync home, not Claude's folder."""

from __future__ import annotations

import threading
import time

import pytest

import mindsync.config as config_mod
import mindsync.storage as storage_mod
from mindsync.config import dispatch_home, migrate_legacy_dispatch_home
from mindsync.dispatch.adapters import user_config_path
from mindsync.dispatch.store import jobs_root


def _isolate_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "ms"))
    isolated = config_mod.Settings()
    monkeypatch.setattr(config_mod, "settings", isolated)
    monkeypatch.setattr(storage_mod, "settings", isolated)
    isolated.ensure_dirs()


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


def test_migrate_copies_missing_files_and_keeps_new_ones(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
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


def test_migrate_legacy_home_is_one_time(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    legacy = tmp_path / "claude-dispatch"
    target = tmp_path / "mindsync-dispatch"
    legacy.mkdir()
    (legacy / "initial.json").write_text("initial", encoding="utf-8")

    assert migrate_legacy_dispatch_home(target, legacy=legacy) is True
    (legacy / "late.json").write_text("late", encoding="utf-8")

    assert migrate_legacy_dispatch_home(target, legacy=legacy) is False
    assert (target / "initial.json").read_text(encoding="utf-8") == "initial"
    assert not (target / "late.json").exists()
    assert (target / config_mod._LEGACY_MIGRATION_MARKER).is_file()


def test_migrate_legacy_home_does_not_mark_failed_copy(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    legacy = tmp_path / "claude-dispatch"
    target = tmp_path / "mindsync-dispatch"
    legacy.mkdir()
    (legacy / "initial.json").write_text("initial", encoding="utf-8")

    def fail_copy(_source, _target):
        raise OSError("synthetic copy failure")

    monkeypatch.setattr(config_mod, "_copy_missing", fail_copy)
    with pytest.raises(OSError, match="synthetic copy failure"):
        migrate_legacy_dispatch_home(target, legacy=legacy)

    assert not (target / config_mod._LEGACY_MIGRATION_MARKER).exists()


def test_concurrent_legacy_migrations_copy_once(tmp_path, monkeypatch):
    _isolate_storage(tmp_path, monkeypatch)
    legacy = tmp_path / "claude-dispatch"
    target = tmp_path / "mindsync-dispatch"
    legacy.mkdir()
    (legacy / "initial.json").write_text("initial", encoding="utf-8")

    original_copy = config_mod._copy_missing
    copy_calls = 0
    copy_calls_lock = threading.Lock()

    def slow_copy(source, destination):
        nonlocal copy_calls
        if source == legacy:
            with copy_calls_lock:
                copy_calls += 1
        time.sleep(0.05)
        original_copy(source, destination)

    monkeypatch.setattr(config_mod, "_copy_missing", slow_copy)
    results = []

    def migrate():
        results.append(migrate_legacy_dispatch_home(target, legacy=legacy))

    workers = [threading.Thread(target=migrate) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert sorted(results) == [False, True]
    assert copy_calls == 1
    assert (target / config_mod._LEGACY_MIGRATION_MARKER).is_file()


def test_isolated_mindsync_home_does_not_copy_real_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "ms"))
    monkeypatch.delenv("AGENT_DISPATCH_HOME", raising=False)
    dispatch_home()
    assert not (tmp_path / "ms" / "dispatch").exists()
