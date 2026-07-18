import json
from pathlib import Path

import mindsync.config as config_mod
import mindsync.storage as storage


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    return config_mod.settings


def test_state_roundtrip(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    with storage.locked_state() as state:
        state["active_project"] = "mindsync-mcp"
        state["agents_focus"]["agent-b"] = {
            "project": "mindsync-mcp",
            "focus": "tests",
            "timestamp": "2026-07-16T00:00:00+00:00",
        }
    loaded = storage.load_state()
    assert loaded["active_project"] == "mindsync-mcp"
    assert "agent-b" in loaded["agents_focus"]
    assert settings.state_file.exists()


def test_queue_enqueue_and_claim(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    storage.enqueue_fact({"entity": "a", "attribute": "b", "text": "one"})
    storage.enqueue_fact({"entity": "c", "attribute": "d", "text": "two"})
    facts = storage.read_queue()
    assert len(facts) == 2
    spool_id, claimed = storage.claim_offline_queue()
    assert len(claimed) == 2
    assert len(storage.read_queue()) == 0
    storage.requeue_failed_facts(spool_id, [claimed[1]], [])
    left = storage.read_queue()
    assert len(left) == 1
    assert left[0]["entity"] == "c"


def test_audit_append(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    storage.log_audit("agent-b", "test", "hello")
    lines = settings.audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["agent"] == "agent-b"
    assert rec["action"] == "test"
