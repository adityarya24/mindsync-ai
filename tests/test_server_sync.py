import os
import time
from pathlib import Path

import mindsync.config as config_mod
import mindsync.server as server
import mindsync.storage as storage
from mindsync.bridge import WriteResult


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    server.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    return config_mod.settings


def test_sync_skips_malformed_facts_without_crashing(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    settings.ssh_host = "example-host"
    settings.remote_root = "/opt/mindsync"

    storage.enqueue_fact({"entity": "a", "attribute": "b", "text": "good"})
    storage.enqueue_fact({"attribute": "missing-entity", "text": "bad"})
    storage.enqueue_fact({"entity": "a", "attribute": "b", "text": "x", "confidence": "not-a-number"})

    written = []
    monkeypatch.setattr(server, "check_remote_online", lambda force=False: True)
    monkeypatch.setattr(
        server,
        "write_batch_remote",
        lambda batch: (written.extend(batch), WriteResult(ok=True, stdout="ok"))[1],
    )
    monkeypatch.setattr(server, "consolidate_remote", lambda: WriteResult(ok=True))
    monkeypatch.setattr(server, "pull_compiled_truth", lambda: WriteResult(ok=True))

    result = server.sync_offline_facts("agent-t")

    assert result["synced_count"] == 1
    assert result["remaining_queue"] == 0
    assert result["dead_letter_count"] == 2
    assert len(written) == 1
    assert any("Malformed" in e for e in result["errors"])
    assert len(storage.read_queue()) == 0


def test_release_does_not_delete_foreign_lock(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    lock_path = settings.lock_dir / "t.lock"
    with storage.file_lock("t"):
        # Simulate a steal + re-acquire by another process while we hold it.
        lock_path.write_text("someone-else\n", encoding="ascii")
    assert lock_path.exists()
    lock_path.unlink()


def test_stale_lock_is_stolen(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    lock_path = settings.lock_dir / "t.lock"
    lock_path.write_text("dead-process\n", encoding="ascii")
    old = time.time() - 120
    os.utime(lock_path, (old, old))
    with storage.file_lock("t", timeout=0.2):
        assert lock_path.read_text(encoding="ascii").strip() != "dead-process"
    assert not lock_path.exists()
