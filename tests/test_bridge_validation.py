import pytest
from pathlib import Path
from unittest.mock import Mock

from mindsync.bridge import (
    validate_agent,
    validate_attribute,
    validate_entity,
    validate_source,
    validate_fact_id,
    validate_fact_text,
    write_fact_remote,
)
from mindsync.config import Settings


def test_validate_accepts_safe():
    assert validate_entity("system-core") == "system-core"
    assert validate_source("agent:coder") == "agent:coder"
    assert validate_agent("my-agent") == "my-agent"
    assert validate_attribute("attr_name") == "attr_name"
    assert validate_fact_id("uuid-123") == "uuid-123"
    assert validate_fact_text("hello world") == "hello world"


def test_validate_rejects_shell_meta():
    with pytest.raises(ValueError):
        validate_entity("foo; rm -rf /")
    with pytest.raises(ValueError):
        validate_attribute("x$(whoami)")
    with pytest.raises(ValueError):
        validate_agent("bad name")
    with pytest.raises(ValueError):
        validate_source("bad source;")


def test_validate_entity_rejects_paths_and_reserved():
    with pytest.raises(ValueError):
        validate_entity("path/to/file")
    with pytest.raises(ValueError):
        validate_entity("path\\to\\file")
    with pytest.raises(ValueError):
        validate_entity("con")
    with pytest.raises(ValueError):
        validate_entity("COM1")
    with pytest.raises(ValueError):
        validate_entity("file.txt:stream")


def test_validate_fact_text_limit():
    assert validate_fact_text("a" * 100) == "a" * 100
    with pytest.raises(ValueError):
        validate_fact_text("")
    with pytest.raises(ValueError):
        validate_fact_text("a" * (50 * 1024 + 1))


def test_write_fact_remote_rejects_bad_id_without_ssh():
    # If remote is disabled, validation should still fail on bad entity/attribute
    with pytest.raises(ValueError):
        write_fact_remote(
            fact_id="123",
            agent="agent-a",
            entity="bad/entity",
            attribute="attr",
            text="hello",
            source="src",
            confidence=1.0,
        )


def test_remote_disabled_by_default(monkeypatch):
    for key in (
        "MINDSYNC_HOME",
        "MINDSYNC_SSH_HOST",
        "MINDSYNC_REMOTE_ROOT",
        "MINDSYNC_REMOTE_WRITE_SCRIPT",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.remote_enabled is False
    assert s.home.name == ".mindsync"


def test_write_batch_remote_mocked(monkeypatch):
    s = Settings()
    s.ssh_host = "test-host"
    s.remote_root = "/test/root"
    s.remote_write_script = "tools/mindsync_fact.py"
    
    import mindsync.bridge as bridge
    monkeypatch.setattr(bridge, "settings", s)
    
    called_args = []
    def mock_run(args, input=None, capture_output=True, timeout=None, check=False):
        called_args.append((args, input))
        res_mock = Mock()
        res_mock.args = args
        res_mock.returncode = 0
        res_mock.stdout = b'{"ok": true, "success_ids": ["f1"]}'
        res_mock.stderr = b''
        return res_mock
        
    monkeypatch.setattr(bridge.subprocess, "run", mock_run)
    
    facts = [{
        "fact_id": "f1",
        "agent": "agent-a",
        "entity": "ent-1",
        "attribute": "attr-1",
        "text": "hello",
        "source": "src-1",
        "confidence": 1.0
    }]
    
    res = bridge.write_batch_remote(facts)
    assert res.ok
    assert len(called_args) == 1
    cmd_args, stdin_data = called_args[0]
    assert "ssh" in cmd_args
    assert "test-host" in cmd_args
    assert b"mindsync_fact.py" in stdin_data
    assert b"base64 -d" in stdin_data


def test_pull_compiled_truth_mocked(monkeypatch, tmp_path):
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    s = Settings()
    s.ssh_host = "test-host"
    s.remote_root = "/test/root"
    s.remote_truth_subdir = "compiled-truth"
    s.ensure_dirs()
    
    import mindsync.config as config_mod
    import mindsync.storage as storage_mod
    import mindsync.bridge as bridge
    monkeypatch.setattr(config_mod, "settings", s)
    monkeypatch.setattr(storage_mod, "settings", s)
    monkeypatch.setattr(bridge, "settings", s)
    
    called_args = []
    def mock_run(args, timeout=None, check=False):
        called_args.append(args)
        dest_dir = Path(args[-1])
        staged_file = dest_dir / "valid-entity.md"
        staged_file.write_text("hello truth", encoding="utf-8")
        res_mock = Mock()
        res_mock.returncode = 0
        res_mock.stdout = "ok"
        res_mock.stderr = ""
        return res_mock
    
    monkeypatch.setattr(bridge, "_run", mock_run)
    
    staging_dir = s.home / "staging-truth"
    res = bridge.pull_compiled_truth()
    assert res.ok
    assert len(called_args) == 1
    scp_args = called_args[0]
    assert "scp" in scp_args
    assert "test-host:/test/root/compiled-truth/." in scp_args
    
    # Staging directory should be cleaned up
    assert not staging_dir.exists()
    
    # Target file should exist under compiled_truth_dir
    truth_file = s.compiled_truth_dir / "valid-entity.md"
    assert truth_file.exists()
    assert truth_file.read_text(encoding="utf-8") == "hello truth"
