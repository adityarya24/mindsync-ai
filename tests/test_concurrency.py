import threading
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.storage as storage


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
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
