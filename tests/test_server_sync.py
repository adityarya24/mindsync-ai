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
    # Dead-lettered records mean this was NOT a clean sync: partial, not ok.
    assert result["ok"] is False
    assert result["status"] == "partial"


def test_only_dead_letter_facts_report_error_not_ok(tmp_path, monkeypatch):
    """When nothing at all synced successfully (everything was malformed /
    dead-lettered), the sync must report status="error" and ok=False, never
    a clean ok=True."""
    settings = _isolate(tmp_path, monkeypatch)
    settings.ssh_host = "example-host"
    settings.remote_root = "/opt/mindsync"

    storage.enqueue_fact({"attribute": "missing-entity", "text": "bad"})

    monkeypatch.setattr(server, "check_remote_online", lambda force=False: True)
    monkeypatch.setattr(server, "write_batch_remote", lambda batch: WriteResult(ok=True))

    result = server.sync_offline_facts("agent-t")

    assert result["synced_count"] == 0
    assert result["dead_letter_count"] == 1
    assert result["ok"] is False
    assert result["status"] == "error"


def test_all_malformed_queue_reports_quarantine_not_empty(tmp_path, monkeypatch):
    """A queue file containing only unparseable lines must NOT be reported
    as "No offline facts" -- it must surface the quarantined count."""
    settings = _isolate(tmp_path, monkeypatch)
    settings.ssh_host = "example-host"
    settings.remote_root = "/opt/mindsync"

    settings.offline_queue_file.write_text(
        "{not valid json\nalso not json\n", encoding="utf-8"
    )

    monkeypatch.setattr(server, "check_remote_online", lambda force=False: True)
    monkeypatch.setattr(server, "write_batch_remote", lambda batch: WriteResult(ok=True))

    result = server.sync_offline_facts("agent-t")

    assert result["message"] != "No offline facts in the queue."
    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["dead_letter_count"] == 2
    assert result["synced_count"] == 0
    assert len(storage.read_queue()) == 0


def test_oversized_fact_is_dead_lettered_not_dropped(tmp_path, monkeypatch):
    """A single fact bigger than max payload must be explicitly rejected/
    quarantined, never silently dropped and never sent to the remote."""
    settings = _isolate(tmp_path, monkeypatch)
    settings.ssh_host = "example-host"
    settings.remote_root = "/opt/mindsync"

    huge_text = "x" * (25 * 1024)  # bigger than the 20KB default max_bytes
    storage.enqueue_fact({"entity": "a", "attribute": "b", "text": huge_text})
    storage.enqueue_fact({"entity": "a", "attribute": "c", "text": "small"})

    written_batches = []

    def mock_write(batch):
        written_batches.append(batch)
        success_ids = [f["fact_id"] for f in batch]
        return WriteResult(ok=True, results={"success_ids": success_ids, "failed": []})

    monkeypatch.setattr(server, "check_remote_online", lambda force=False: True)
    monkeypatch.setattr(server, "write_batch_remote", mock_write)
    monkeypatch.setattr(server, "consolidate_remote", lambda: WriteResult(ok=True))
    monkeypatch.setattr(server, "pull_compiled_truth", lambda: WriteResult(ok=True))

    result = server.sync_offline_facts("agent-t")

    assert result["synced_count"] == 1
    assert result["dead_letter_count"] == 1
    assert result["ok"] is False
    assert result["status"] == "partial"
    assert any("exceeds max payload" in e for e in result["errors"])
    # The oversized fact must never make it into a remote payload.
    for batch in written_batches:
        assert all(len(f.get("text", "")) < 25 * 1024 for f in batch)
    assert len(storage.read_queue()) == 0


def test_bounded_batches_count_complete_serialized_json():
    """The byte bound must account for the array brackets/commas of the
    COMPLETE serialized payload, not just the sum of item lengths."""
    import json

    from mindsync.server import _make_bounded_batches

    # Craft facts whose *individual* lengths sum well under max_bytes, but
    # whose complete serialized array (with brackets + separators) would
    # exceed it if array overhead were ignored.
    max_bytes = 200
    fact_text = "y" * 18
    facts = [
        {"fact_id": f"f{i}", "entity": "e", "attribute": "a", "text": fact_text}
        for i in range(10)
    ]
    batches, oversized = _make_bounded_batches(facts, max_count=50, max_bytes=max_bytes)

    assert oversized == []
    for batch in batches:
        assert len(json.dumps(batch).encode("utf-8")) <= max_bytes


def test_lock_file_is_persistent_and_reusable(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    lock_path = settings.lock_dir / "t.lock"
    with storage.file_lock("t"):
        pass
    assert lock_path.exists()
    first_token = lock_path.read_text(encoding="ascii").strip()

    with storage.file_lock("t"):
        pass
    second_token = lock_path.read_text(encoding="ascii").strip()

    assert second_token != first_token


def test_abandoned_lock_file_does_not_block_os_lock(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    lock_path = settings.lock_dir / "t.lock"
    lock_path.write_text("dead-process\n", encoding="ascii")
    old = time.time() - 120
    os.utime(lock_path, (old, old))
    with storage.file_lock("t", timeout=0.2):
        pass
    assert lock_path.exists()
    assert lock_path.read_text(encoding="ascii").strip() != "dead-process"
