import json
import os
import threading
import time
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.storage as storage
from mindsync.bridge import WriteResult


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    import mindsync.server as server
    server.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    return config_mod.settings


def test_concurrent_enqueue(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    def worker(i: int):
        storage.enqueue_fact({"entity": "a", "attribute": "b", "text": f"fact-{i}"})

    threads = []
    for i in range(50):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    facts = storage.read_queue()
    assert len(facts) == 50


def test_concurrent_enqueue_stress_high_contention(tmp_path, monkeypatch):
    """50 same-process writers must all persist under the queue lock deadline."""
    settings = _isolate(tmp_path, monkeypatch)
    errors: list[BaseException] = []

    def worker(i: int):
        try:
            storage.enqueue_fact({"entity": "a", "attribute": "b", "text": f"stress-{i}"})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    facts = storage.read_queue()
    assert not errors, f"unexpected enqueue failures: {errors[:3]}"
    assert len(facts) == 50
    raw = settings.offline_queue_file.read_text(encoding="utf-8")
    for line in raw.strip().splitlines():
        json.loads(line)


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt thread contention path")
def test_concurrent_enqueue_bounded_timeout_on_insufficient_deadline(tmp_path, monkeypatch):
    """When the queue deadline is too short, failures surface as TimeoutError."""
    settings = _isolate(tmp_path, monkeypatch)
    settings.queue_lock_timeout_seconds = 0.3
    successes = 0
    errors: list[TimeoutError] = []
    lock = threading.Lock()

    def worker(i: int):
        nonlocal successes
        try:
            storage.enqueue_fact({"entity": "a", "attribute": "b", "text": f"bound-{i}"})
        except TimeoutError as exc:
            with lock:
                errors.append(exc)
            return
        with lock:
            successes += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors, "expected some writers to hit the bounded queue lock deadline"
    assert successes + len(errors) == 50
    facts = storage.read_queue()
    assert len(facts) == successes
    raw = settings.offline_queue_file.read_text(encoding="utf-8")
    for line in raw.strip().splitlines():
        json.loads(line)


def test_concurrent_claim(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    for i in range(100):
        storage.enqueue_fact({"entity": "a", "attribute": "b", "text": f"fact-{i}"})

    claimed_spools = []
    def worker():
        spool_id, claimed, _malformed_count = storage.claim_offline_queue()
        if claimed:
            claimed_spools.append((spool_id, claimed))

    threads = []
    for _ in range(5):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Exactly one thread should have claimed the 100 facts, the others should get empty
    assert len(claimed_spools) == 1
    assert len(claimed_spools[0][1]) == 100
    assert len(storage.read_queue()) == 0


def test_two_simultaneous_sync_calls(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    settings.ssh_host = "example-host"
    settings.remote_root = "/opt/mindsync"
    settings.lock_timeout_seconds = 0.1
    
    storage.enqueue_fact({"entity": "a", "attribute": "b", "text": "fact-1"})
    
    import mindsync.server as server
    import time
    
    def mock_write(batch):
        time.sleep(0.5)
        from mindsync.bridge import WriteResult
        import json
        success_ids = [f["fact_id"] for f in batch]
        return WriteResult(ok=True, stdout=json.dumps({"ok": True, "success_ids": success_ids}))
        
    monkeypatch.setattr(server, "check_remote_online", lambda force=False: True)
    monkeypatch.setattr(server, "write_batch_remote", mock_write)
    monkeypatch.setattr(server, "consolidate_remote", lambda: WriteResult(ok=True))
    monkeypatch.setattr(server, "pull_compiled_truth", lambda: WriteResult(ok=True))
    
    results = []
    def run_sync():
        res = server.sync_offline_facts("agent-t")
        results.append(res)
            
    t1 = threading.Thread(target=run_sync)
    t2 = threading.Thread(target=run_sync)
    
    t1.start()
    time.sleep(0.1)
    t2.start()
    
    t1.join()
    t2.join()
    
    success = [r for r in results if isinstance(r, dict) and r.get("synced_count") == 1]
    locked_out = [r for r in results if isinstance(r, dict) and "Another sync is currently in progress" in r.get("message", "")]

    assert len(success) == 1
    assert len(locked_out) == 1


def test_lock_beyond_legacy_stale_timeout_is_not_stolen(tmp_path, monkeypatch):
    """A live OS-lock holder must not lose its lock merely because the
    legacy stale interval elapsed."""
    settings = _isolate(tmp_path, monkeypatch)
    # Tiny windows so the test runs fast, but with the same ratios as prod
    # (thief's own acquire timeout << stale_after << holder's hold time).
    settings.lock_stale_seconds = 0.3
    settings.lock_timeout_seconds = 0.1

    storage.enqueue_fact({"entity": "a", "attribute": "b", "text": "fact-1"})
    spool_id, claimed, _malformed_count = storage.claim_offline_queue()
    assert len(claimed) == 1
    spool_path = settings.spool_dir / f"spool-{spool_id}.jsonl"
    assert spool_path.exists()

    holder_state = {"acquired": False, "held_full_duration": False}

    def long_holder():
        with storage.file_lock("sync"):
            holder_state["acquired"] = True
            # Hold well beyond the stale timeout -- a real slow remote write
            # (e.g. 90s x retries) that a naive mtime-only scheme would
            # mistake for a dead holder and steal from.
            time.sleep(settings.lock_stale_seconds * 4)
            holder_state["held_full_duration"] = True
            # Only the still-legitimate holder may recover/requeue its spool.
            storage.requeue_failed_facts(spool_id, [], [])

    holder = threading.Thread(target=long_holder)
    holder.start()

    # Give the holder time to acquire, then wait past the legacy stale window.
    time.sleep(settings.lock_stale_seconds * 1.5)
    assert holder_state["acquired"] is True

    thief_stole_lock = False
    try:
        with storage.file_lock("sync"):
            thief_stole_lock = True
            # If we got here, the lock was wrongly stolen; a real thief
            # would now wrongly believe it's safe to recover orphan spools.
            storage.recover_orphan_spools()
    except TimeoutError:
        pass

    holder.join(timeout=10)

    assert thief_stole_lock is False, "lock was stolen from a live OS-lock holder"
    assert holder_state["held_full_duration"] is True
    # The spool must still exist right up until the legitimate holder
    # finishes and requeues it -- never deleted/recovered by a thief.
    assert not spool_path.exists()
    assert len(storage.read_queue()) == 0
