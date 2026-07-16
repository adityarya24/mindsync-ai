import json
from pathlib import Path

import mindsync.config as config_mod
import mindsync.storage as storage


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "gbrain"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    # Reset singleton paths
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    return config_mod.settings


def test_state_roundtrip(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    with storage.locked_state() as state:
        state["active_project"] = "mindsync-mcp"
        state["agents_focus"]["Ashwatthama"] = {
            "project": "mindsync-mcp",
            "focus": "tests",
            "timestamp": "2026-07-16T00:00:00+00:00",
        }
    loaded = storage.load_state()
    assert loaded["active_project"] == "mindsync-mcp"
    assert "Ashwatthama" in loaded["agents_focus"]
    assert settings.state_file.exists()


def test_queue_enqueue_and_rewrite(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    storage.enqueue_fact({"entity": "a", "attribute": "b", "text": "one"})
    storage.enqueue_fact({"entity": "c", "attribute": "d", "text": "two"})
    facts = storage.read_queue()
    assert len(facts) == 2
    storage.rewrite_queue([facts[1]])
    left = storage.read_queue()
    assert len(left) == 1
    assert left[0]["entity"] == "c"


def test_audit_append(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    storage.log_audit("Ashwatthama", "test", "hello")
    lines = settings.audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["agent"] == "Ashwatthama"
    assert rec["action"] == "test"
