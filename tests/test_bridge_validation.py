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


def test_sanitize_error_redacts_ssh_key_and_paths(monkeypatch):
    """Blocker #3: OSError/SCP/publish messages must have absolute paths,
    home dirs, and key-file references redacted -- not just the first two
    path segments (the old regex left `.ssh\\id_ed25519`-style suffixes)."""
    import mindsync.bridge as bridge

    s = Settings()
    s.ssh_host = "prod-host"
    s.remote_root = "/srv/mindsync"
    monkeypatch.setattr(bridge, "settings", s)

    msg = (
        "Permission denied (publickey) for prod-host, "
        "identity file /home/aditya/.ssh/id_ed25519 rejected; "
        "also tried C:\\Users\\aditya\\.ssh\\id_rsa "
        "and ~/.ssh/id_ecdsa, "
        "remote root /srv/mindsync/compiled-truth unreachable"
    )
    sanitized = bridge._sanitize_error(msg)

    for leaked in ("id_ed25519", "id_rsa", "id_ecdsa", ".ssh", "aditya", "prod-host", "/srv/mindsync"):
        assert leaked not in sanitized, f"{leaked!r} leaked in: {sanitized!r}"

    assert "[SSH_HOST]" in sanitized
    assert "[REMOTE_ROOT]" in sanitized
    assert "[PATH]" in sanitized or "[KEY_FILE]" in sanitized


def test_sanitize_error_empty_and_none_like():
    import mindsync.bridge as bridge

    assert bridge._sanitize_error("") == "unknown error"


def test_pull_compiled_truth_sanitizes_scp_oserror(monkeypatch, tmp_path):
    """End-to-end proof that the SCP OSError path (previously unsanitized)
    now redacts sensitive fragments before they reach the caller."""
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    s = Settings()
    s.ssh_host = "test-host"
    s.remote_root = "/test/root"
    s.ensure_dirs()

    import mindsync.config as config_mod
    import mindsync.storage as storage_mod
    import mindsync.bridge as bridge
    monkeypatch.setattr(config_mod, "settings", s)
    monkeypatch.setattr(storage_mod, "settings", s)
    monkeypatch.setattr(bridge, "settings", s)

    def raise_oserror(args, timeout=None, check=False):
        raise OSError("[Errno 2] No such file or directory: '/home/aditya/.ssh/id_ed25519'")

    monkeypatch.setattr(bridge, "_run", raise_oserror)

    res = bridge.pull_compiled_truth()
    assert res.ok is False
    assert res.error is not None
    assert "id_ed25519" not in res.error
    assert ".ssh" not in res.error
    assert "aditya" not in res.error
