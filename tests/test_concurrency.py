import threading
from pathlib import Path


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


def test_concurrent_claim(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    for i in range(100):
        storage.enqueue_fact({"entity": "a", "attribute": "b", "text": f"fact-{i}"})

    claimed_spools = []
    def worker():
        spool_id, claimed = storage.claim_offline_queue()
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
