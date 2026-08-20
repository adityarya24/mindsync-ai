import subprocess

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


def test_run_does_not_inherit_long_lived_mcp_stdin(monkeypatch):
    import mindsync.bridge as bridge

    seen = {}

    def mock_run(args, **kwargs):
        seen.update(kwargs)
        return Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bridge.subprocess, "run", mock_run)

    bridge._run(["ssh", "example-host", "echo", "1"], check=False)

    assert seen["stdin"] is subprocess.DEVNULL


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
    assert cmd_args[0] == bridge.resolve_openssh_tool("ssh")
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
    def mock_run(args, stdin=None, timeout=None, check=False):
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
    
    res = bridge.pull_compiled_truth()
    assert res.ok
    assert len(called_args) == 1
    scp_args = called_args[0]
    assert scp_args[0] == bridge.resolve_openssh_tool("scp")
    assert "test-host:/test/root/compiled-truth/." in scp_args
    
    # Every pull gets a unique staging directory and cleans it up.
    staging_dir = Path(scp_args[-1])
    assert staging_dir.name.startswith("staging-truth-")
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
        "identity file /home/testuser/.ssh/id_ed25519 rejected; "
        "also tried C:\\Users\\testuser\\.ssh\\id_rsa "
        "and ~/.ssh/id_ecdsa, "
        "remote root /srv/mindsync/compiled-truth unreachable"
    )
    sanitized = bridge._sanitize_error(msg)

    for leaked in ("id_ed25519", "id_rsa", "id_ecdsa", ".ssh", "testuser", "prod-host", "/srv/mindsync"):
        assert leaked not in sanitized, f"{leaked!r} leaked in: {sanitized!r}"

    assert "[SSH_HOST]" in sanitized
    assert "[REMOTE_ROOT]" in sanitized
    assert "[PATH]" in sanitized or "[KEY_FILE]" in sanitized


def test_sanitize_error_empty_and_none_like():
    import mindsync.bridge as bridge

    assert bridge._sanitize_error("") == "unknown error"


def test_legacy_write_sanitizes_remote_errors(monkeypatch):
    import subprocess
    import mindsync.bridge as bridge

    s = Settings()
    s.ssh_host = "prod-host"
    s.remote_root = "/srv/mindsync"
    monkeypatch.setattr(bridge, "settings", s)
    monkeypatch.setattr(
        bridge,
        "_ssh_script",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="identity /home/testuser/.ssh/id_ed25519 rejected by prod-host",
        ),
    )

    result = bridge._legacy_write_call(
        {
            "fact_id": "fact-1",
            "agent": "agent-a",
            "entity": "project:test",
            "attribute": "status",
            "text": "hello",
            "source": "agent:agent-a",
            "confidence": 1.0,
        },
        with_fact_id=True,
    )

    assert result.ok is False
    assert "testuser" not in (result.error or "")
    assert ".ssh" not in (result.error or "")
    assert "id_ed25519" not in (result.error or "")


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
        raise OSError("[Errno 2] No such file or directory: '/home/testuser/.ssh/id_ed25519'")

    monkeypatch.setattr(bridge, "_run", raise_oserror)

    res = bridge.pull_compiled_truth()
    assert res.ok is False
    assert res.error is not None
    assert "id_ed25519" not in res.error
    assert ".ssh" not in res.error
    assert "testuser" not in res.error


def test_resolve_openssh_tool_honours_explicit_config(tmp_path, monkeypatch):
    import mindsync.bridge as bridge

    ssh = tmp_path / "ssh.exe"
    scp = tmp_path / "scp.exe"
    ssh.write_text("", encoding="utf-8")
    scp.write_text("", encoding="utf-8")
    monkeypatch.setattr(bridge.settings, "ssh_bin", str(ssh), raising=False)

    assert bridge.resolve_openssh_tool("ssh") == str(ssh)
    # scp is taken from the same install, not from PATH.
    assert bridge.resolve_openssh_tool("scp") == str(scp)


def test_resolve_openssh_tool_falls_back_when_sibling_missing(tmp_path, monkeypatch):
    import mindsync.bridge as bridge

    ssh = tmp_path / "ssh.exe"
    ssh.write_text("", encoding="utf-8")
    monkeypatch.setattr(bridge.settings, "ssh_bin", str(ssh), raising=False)

    assert bridge.resolve_openssh_tool("scp") == "scp"


def test_resolve_openssh_tool_prefers_system_openssh_on_windows(tmp_path, monkeypatch):
    """Git for Windows' MSYS ssh cannot use the Windows agent; don't let it win."""
    import mindsync.bridge as bridge

    system_root = tmp_path / "Windows"
    openssh = system_root / "System32" / "OpenSSH"
    openssh.mkdir(parents=True)
    (openssh / "ssh.exe").write_text("", encoding="utf-8")

    monkeypatch.setattr(bridge.settings, "ssh_bin", "", raising=False)
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(system_root))

    assert bridge.resolve_openssh_tool("ssh") == str(openssh / "ssh.exe")
    # scp.exe is absent here, so it must not be invented.
    assert bridge.resolve_openssh_tool("scp") == "scp"


def test_resolve_openssh_tool_plain_name_on_posix(monkeypatch):
    import mindsync.bridge as bridge

    monkeypatch.setattr(bridge.settings, "ssh_bin", "", raising=False)
    monkeypatch.setattr(bridge.sys, "platform", "linux")
    assert bridge.resolve_openssh_tool("ssh") == "ssh"
    assert bridge.resolve_openssh_tool("scp") == "scp"


def test_validate_entity_accepts_namespaced_keys():
    """Entity keys are namespaced with a colon (person:alice)."""
    assert validate_entity("person:alice") == "person:alice"
    assert validate_entity("project:web-api") == "project:web-api"
    assert validate_entity("system:mindsync_ai") == "system:mindsync_ai"


def test_validate_entity_still_rejects_dangerous_input():
    for bad in (
        "a b",            # whitespace
        "a;rm -rf /",     # shell metacharacter
        "../etc/passwd",  # traversal
        "a$(id)",         # substitution
        "a|b",
        "a&b",
        "file.txt:stream",  # Windows alternate data stream shape
        "project:NUL",      # reserved device name behind a namespace
        "a:b:c",            # only one namespace prefix
        ":leading-colon",   # must still start alphanumeric
        "",
        "NUL",            # reserved Windows device name
        "x" * 200,        # over length
    ):
        with pytest.raises(ValueError):
            validate_entity(bad)


def _fact(fid: str, entity: str = "ent-1"):
    return {
        "fact_id": fid,
        "agent": "agent-a",
        "entity": entity,
        "attribute": "attr-1",
        "text": "hello",
        "source": "src-1",
        "confidence": 1.0,
    }


def _old_remote_mock(calls, *, rejects_fact_id: bool):
    """Simulate a remote with no `batch` and (optionally) no `--fact_id`."""
    def mock_run(args, input=None, capture_output=True, timeout=None, check=False):
        stdin_data = input or b""
        calls.append(stdin_data)
        res = Mock()
        res.args = args
        if b" batch " in stdin_data or stdin_data.strip().endswith(b"batch"):
            res.returncode = 2
            res.stdout = b""
            res.stderr = (
                b"usage: mindsync_fact.py [-h] {write,read} ...\n"
                b"mindsync_fact.py: error: argument cmd: invalid choice: 'batch'"
            )
        elif rejects_fact_id and b"--fact_id" in stdin_data:
            res.returncode = 2
            res.stdout = b""
            res.stderr = (
                b"usage: mindsync_fact.py write [-h] --agent AGENT --entity ENTITY\n"
                b"mindsync_fact.py: error: unrecognized arguments: --fact_id abc123"
            )
        else:
            res.returncode = 0
            res.stdout = b'{"ok": true, "count": 1}'
            res.stderr = b""
        return res
    return mock_run


def _configure_remote(monkeypatch):
    s = Settings()
    s.ssh_host = "test-host"
    s.remote_root = "/test/root"
    s.remote_write_script = "tools/mindsync_fact.py"
    import mindsync.bridge as bridge
    monkeypatch.setattr(bridge, "settings", s)
    monkeypatch.setattr(bridge, "_legacy_supports_fact_id", None, raising=False)
    return bridge


def test_legacy_write_degrades_when_remote_rejects_fact_id(monkeypatch):
    """A remote too old for --fact_id must still receive the fact, not lose it."""
    bridge = _configure_remote(monkeypatch)
    calls = []
    monkeypatch.setattr(bridge.subprocess, "run", _old_remote_mock(calls, rejects_fact_id=True))

    res = bridge.write_batch_remote([_fact("f1")])

    assert res.ok
    assert res.results["success_ids"] == ["f1"]
    assert res.results.get("failed") == []
    # batch attempt, --fact_id attempt, then the retry without it.
    assert len(calls) == 3
    assert b"--fact_id" in calls[1]
    assert b"--fact_id" not in calls[2]
    assert b"--entity" in calls[2]


def test_legacy_write_learns_fact_id_is_unsupported(monkeypatch):
    """The rejection is probed once, not re-paid on every remaining fact."""
    bridge = _configure_remote(monkeypatch)
    calls = []
    monkeypatch.setattr(bridge.subprocess, "run", _old_remote_mock(calls, rejects_fact_id=True))

    res = bridge.write_batch_remote([_fact("f1"), _fact("f2"), _fact("f3")])

    assert res.ok
    assert set(res.results["success_ids"]) == {"f1", "f2", "f3"}
    # 1 batch + (f1: reject + retry) + f2 + f3 = 5, not 7.
    assert len(calls) == 5
    assert sum(1 for c in calls if b"--fact_id" in c) == 1


def test_legacy_write_keeps_fact_id_when_remote_accepts_it(monkeypatch):
    """Don't drop fact_id against a remote that understands it."""
    bridge = _configure_remote(monkeypatch)
    calls = []
    monkeypatch.setattr(bridge.subprocess, "run", _old_remote_mock(calls, rejects_fact_id=False))

    res = bridge.write_batch_remote([_fact("f1"), _fact("f2")])

    assert res.ok
    assert set(res.results["success_ids"]) == {"f1", "f2"}
    assert len(calls) == 3  # batch probe + one write per fact, no retries
    for c in calls[1:]:
        assert b"--fact_id" in c
