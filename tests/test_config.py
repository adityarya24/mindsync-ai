from mindsync.config import Settings


def test_defaults_are_generic_and_local_only(monkeypatch):
    monkeypatch.delenv("MINDSYNC_HOME", raising=False)
    monkeypatch.delenv("MINDSYNC_SSH_HOST", raising=False)
    monkeypatch.delenv("MINDSYNC_REMOTE_ROOT", raising=False)
    s = Settings()
    assert s.ssh_host == ""
    assert s.remote_root == ""
    assert s.remote_enabled is False
    # Only the leaf dir is product-owned; parent is the OS user home (machine-specific).
    assert s.home.name == ".mindsync"
    assert s.remote_write_script == "tools/mindsync_fact.py"
    assert s.remote_consolidate_script == "tools/mindsync_consolidate.py"
    assert "openclaw" not in s.remote_write_script
    assert "gbrain" not in s.remote_write_script
    assert "openclaw" not in s.remote_root
    assert "gbrain" not in s.remote_root


def test_remote_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("MINDSYNC_SSH_HOST", "my-server")
    monkeypatch.setenv("MINDSYNC_REMOTE_ROOT", "/opt/mindsync")
    monkeypatch.setenv("MINDSYNC_REMOTE_WRITE_SCRIPT", "bin/write_fact.py")
    s = Settings()
    assert s.remote_enabled is True
    assert s.ssh_host == "my-server"
    assert s.remote_root == "/opt/mindsync"
    assert s.remote_write_script == "bin/write_fact.py"
