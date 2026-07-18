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
