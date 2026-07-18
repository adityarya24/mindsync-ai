import pytest

from mindsync.bridge import validate_id, write_fact_remote
from mindsync.config import Settings


def test_validate_id_accepts_safe():
    assert validate_id("entity", "system-core") == "system-core"
    assert validate_id("source", "agent:coder") == "agent:coder"


def test_validate_id_rejects_shell_meta():
    with pytest.raises(ValueError):
        validate_id("entity", "foo; rm -rf /")
    with pytest.raises(ValueError):
        validate_id("attribute", "x$(whoami)")
    with pytest.raises(ValueError):
        validate_id("agent", "bad name")


def test_write_fact_remote_rejects_bad_id_without_ssh():
    result = write_fact_remote(
        fact_id="test-123",
        agent="agent-b",
        entity="bad;id",
        attribute="ok",
        text="hello",
        source="agent:agent-b",
        confidence=1.0,
    )
    assert result.ok is False
    assert result.error
    assert "Invalid entity" in result.error or "not configured" in result.error


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
        from unittest.mock import Mock
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


def test_write_batch_remote_falls_back_to_legacy_single_writes(monkeypatch):
    """An already-deployed remote writer that only understands single
    `write` calls (no `batch` subcommand yet) must not break a new client:
    write_batch_remote should detect the "unknown subcommand" failure and
    transparently fall back to one legacy write per fact."""
    s = Settings()
    s.ssh_host = "test-host"
    s.remote_root = "/test/root"
    s.remote_write_script = "tools/mindsync_fact.py"

    import mindsync.bridge as bridge
    monkeypatch.setattr(bridge, "settings", s)

    calls = []

    def mock_run(args, input=None, capture_output=True, timeout=None, check=False):
        from unittest.mock import Mock

        stdin_data = input or b""
        calls.append(stdin_data)
        res_mock = Mock()
        res_mock.args = args
        if b" batch " in stdin_data or stdin_data.strip().endswith(b"batch"):
            # Simulate the old remote: argparse rejects the unknown subcommand.
            res_mock.returncode = 2
            res_mock.stdout = b""
            res_mock.stderr = (
                b"usage: mindsync_fact.py [-h] {write} ...\n"
                b"mindsync_fact.py: error: argument cmd: invalid choice: 'batch' "
                b"(choose from 'write')"
            )
        else:
            # Legacy single-write call always succeeds.
            res_mock.returncode = 0
            res_mock.stdout = b'{"ok": true, "stored": "facts.db", "count": 1}'
            res_mock.stderr = b""
        return res_mock

    monkeypatch.setattr(bridge.subprocess, "run", mock_run)

    facts = [
        {
            "fact_id": "f1",
            "agent": "agent-a",
            "entity": "ent-1",
            "attribute": "attr-1",
            "text": "hello",
            "source": "src-1",
            "confidence": 1.0,
        },
        {
            "fact_id": "f2",
            "agent": "agent-a",
            "entity": "ent-2",
            "attribute": "attr-2",
            "text": "world",
            "source": "src-1",
            "confidence": 0.5,
        },
    ]

    res = bridge.write_batch_remote(facts)

    assert res.ok
    # One failed batch attempt + one legacy `write` call per fact.
    assert len(calls) == 1 + len(facts)
    assert res.results is not None
    assert set(res.results["success_ids"]) == {"f1", "f2"}
    assert res.results.get("failed") == []
    # The fallback calls must use the legacy single-write subcommand, not batch.
    for stdin_data in calls[1:]:
        assert b" write " in stdin_data
        assert b"batch" not in stdin_data
