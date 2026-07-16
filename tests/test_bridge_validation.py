import pytest

from mindsync.bridge import validate_id, write_fact_remote


def test_validate_id_accepts_safe():
    assert validate_id("entity", "system-openclaw") == "system-openclaw"
    assert validate_id("source", "agent:Ashwatthama") == "agent:Ashwatthama"


def test_validate_id_rejects_shell_meta():
    with pytest.raises(ValueError):
        validate_id("entity", "foo; rm -rf /")
    with pytest.raises(ValueError):
        validate_id("attribute", "x$(whoami)")
    with pytest.raises(ValueError):
        validate_id("agent", "bad name")


def test_write_fact_remote_rejects_bad_id_without_ssh():
    result = write_fact_remote(
        agent="Ashwatthama",
        entity="bad;id",
        attribute="ok",
        text="hello",
        source="agent:Ashwatthama",
        confidence=1.0,
    )
    assert result.ok is False
    assert "Invalid entity" in (result.error or "")
