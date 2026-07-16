from mindsync.config import Settings


_MINDSYNC_ENV = (
    "MINDSYNC_HOME",
    "MINDSYNC_SSH_HOST",
    "MINDSYNC_REMOTE_ROOT",
    "MINDSYNC_REMOTE_ENV_FILE",
    "MINDSYNC_REMOTE_WRITE_SCRIPT",
    "MINDSYNC_REMOTE_CONSOLIDATE_SCRIPT",
    "MINDSYNC_REMOTE_TRUTH_SUBDIR",
)


def _clear_mindsync_env(monkeypatch) -> None:
    for key in _MINDSYNC_ENV:
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_generic_and_local_only(monkeypatch):
    _clear_mindsync_env(monkeypatch)
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
