from mindsync.config import Settings

import pytest


_MINDSYNC_ENV = (
    "MINDSYNC_HOME",
    "MINDSYNC_SSH_HOST",
    "MINDSYNC_REMOTE_ROOT",
    "MINDSYNC_REMOTE_ENV_FILE",
    "MINDSYNC_REMOTE_WRITE_SCRIPT",
    "MINDSYNC_REMOTE_CONSOLIDATE_SCRIPT",
    "MINDSYNC_REMOTE_TRUTH_SUBDIR",
    "MINDSYNC_MEMORY_MODEL_URL",
    "MINDSYNC_MEMORY_EMBEDDING_MODEL",
    "MINDSYNC_MEMORY_CONSOLIDATION_MODEL",
    "MINDSYNC_MEMORY_MODEL_TIMEOUT",
    "MINDSYNC_QUEUE_LOCK_TIMEOUT",
    "MINDSYNC_LOCK_CONTENTION_BACKOFF_BASE",
    "MINDSYNC_LOCK_CONTENTION_BACKOFF_MAX",
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
    assert s.memory_model_url == "http://127.0.0.1:11434"
    assert s.memory_embedding_model == ""
    assert s.memory_consolidation_model == ""
    assert s.memory_model_timeout_seconds == 60


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


def test_memory_model_timeout_is_bounded(monkeypatch):
    _clear_mindsync_env(monkeypatch)
    monkeypatch.setenv("MINDSYNC_MEMORY_MODEL_TIMEOUT", "0")
    try:
        Settings()
    except ValueError as exc:
        assert "MINDSYNC_MEMORY_MODEL_TIMEOUT" in str(exc)
    else:  # pragma: no cover - explicit failure keeps this dependency-free
        raise AssertionError("zero timeout should be rejected")


def test_memory_model_timeout_names_invalid_numeric_setting(monkeypatch):
    _clear_mindsync_env(monkeypatch)
    monkeypatch.setenv("MINDSYNC_MEMORY_MODEL_TIMEOUT", "not-a-number")
    try:
        Settings()
    except ValueError as exc:
        assert str(exc) == "MINDSYNC_MEMORY_MODEL_TIMEOUT must be a number"
    else:  # pragma: no cover
        raise AssertionError("non-numeric timeout should be rejected")


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("MINDSYNC_QUEUE_LOCK_TIMEOUT", "0"),
        ("MINDSYNC_QUEUE_LOCK_TIMEOUT", "-1"),
        ("MINDSYNC_QUEUE_LOCK_TIMEOUT", "nan"),
        ("MINDSYNC_LOCK_CONTENTION_BACKOFF_BASE", "0"),
        ("MINDSYNC_LOCK_CONTENTION_BACKOFF_BASE", "-0.01"),
        ("MINDSYNC_LOCK_CONTENTION_BACKOFF_BASE", "inf"),
        ("MINDSYNC_LOCK_CONTENTION_BACKOFF_MAX", "0"),
        ("MINDSYNC_LOCK_CONTENTION_BACKOFF_MAX", "not-a-number"),
    ],
)
def test_lock_contention_settings_reject_invalid_values(monkeypatch, env_name, value):
    _clear_mindsync_env(monkeypatch)
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ValueError, match=env_name):
        Settings()
