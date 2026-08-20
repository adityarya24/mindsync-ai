"""Tests for local session memory."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.memory as memory_mod
from mindsync.memory import (
    _close_local_db,
    _get_db,
    memory_bootstrap,
    memory_checkpoint,
    session_end,
    session_start,
)


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _close_local_db()
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    memory_mod.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    yield home
    _close_local_db()


def test_session_lifecycle_and_structured_round_trip():
    session_id = session_start(
        project_key="project-one",
        agent="test-agent",
        workspace="C:/work/project-one",
        branch="feature/memory",
        goal="Persist the current handoff",
    )
    checkpoint_id = memory_checkpoint(
        session_id=session_id,
        status="working",
        decisions={"storage": "sqlite"},
        files_changed=["mindsync/memory.py"],
        tests=[{"name": "pytest", "passed": True}],
        pending=["Add CLI adapters"],
        blockers=[],
        durable_facts=["Memory is local-first"],
    )
    session_end(session_id, status="success")

    result = memory_bootstrap("project-one")
    assert checkpoint_id
    assert result["project_key"] == "project-one"
    assert result["bootstraps"] == [
        {
            "session_id": session_id,
            "agent": "test-agent",
            "workspace": "C:/work/project-one",
            "branch": "feature/memory",
            "goal": "Persist the current handoff",
            "session_status": "success",
            "started_at": result["bootstraps"][0]["started_at"],
            "ended_at": result["bootstraps"][0]["ended_at"],
            "checkpoint_time": result["bootstraps"][0]["checkpoint_time"],
            "decisions": {"storage": "sqlite"},
            "files_changed": ["mindsync/memory.py"],
            "tests": [{"name": "pytest", "passed": True}],
            "pending": ["Add CLI adapters"],
            "blockers": [],
            "durable_facts": ["Memory is local-first"],
        }
    ]


def test_project_isolation():
    first = session_start(project_key="project-a", agent="agent-a")
    memory_checkpoint(first, decisions=["Only A can retrieve this"])
    second = session_start(project_key="project-b", agent="agent-b")
    memory_checkpoint(second, decisions=["Only B can retrieve this"])

    result_a = memory_bootstrap("project-a")
    result_b = memory_bootstrap("project-b")
    assert [item["session_id"] for item in result_a["bootstraps"]] == [first]
    assert [item["session_id"] for item in result_b["bootstraps"]] == [second]


def test_redaction_preserves_structured_values():
    session_id = session_start(project_key="secure-project", agent="test")
    memory_checkpoint(
        session_id,
        decisions={
            "github": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "aws": "AKIAIOSFODNN7EXAMPLE",
            "key": (
                "-----BEGIN RSA PRIVATE KEY-----\nsecret material\n"
                "-----END RSA PRIVATE KEY-----"
            ),
            "nested": ["password = 'super_secret_password_12345'"],
            "password": "plain_object_password_12345",
            "token": "abcdefghijklmnopqrstuvwxyz012345",
            "api_key": "abcdefghijklmnopqrstuvwxyz012345",
            "github_fine_grained": "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
            "github_oauth": "gho_abcdefghijklmnopqrstuvwxyz0123456789",
            "openai": "sk-abcdefghijklmnopqrstuvwxyz0123456789",
            "authorization": "Bearer abcdefghijklmnopqrstuvwxyz012345",
            "client_secret": "Xk29fjQ2mZ81pLdo",
            "private_key": "private-key-material-12345",
            "session_token": "session-token-material-12345",
            "aws_secret_access_key": "aws-secret-material-12345",
        },
    )

    decisions = memory_bootstrap("secure-project")["bootstraps"][0]["decisions"]
    serialized = json.dumps(decisions)
    assert isinstance(decisions, dict)
    assert "ghp_" not in serialized
    assert "AKIA" not in serialized
    assert "super_secret_password_12345" not in serialized
    assert "plain_object_password_12345" not in serialized
    assert "abcdefghijklmnopqrstuvwxyz012345" not in serialized
    assert "github_pat_" not in serialized
    assert "gho_" not in serialized
    assert "sk-" not in serialized
    assert "Bearer abcdef" not in serialized
    assert "Xk29fjQ2mZ81pLdo" not in serialized
    assert "private-key-material-12345" not in serialized
    assert "session-token-material-12345" not in serialized
    assert "aws-secret-material-12345" not in serialized
    assert serialized.count("[REDACTED]") >= 15


def test_session_end_redacts_status_before_direct_persistence():
    session_id = session_start(project_key="secure-project", agent="test")
    raw_token = "ghp_" + "a" * 36

    session_end(session_id, status=raw_token)

    row = _get_db().execute(
        "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    assert row["status"] == "[REDACTED]"
    assert raw_token not in json.dumps(memory_bootstrap("secure-project"))


def test_checkpoint_rejects_excessive_nesting():
    session_id = session_start(project_key="secure-project", agent="test")
    nested: object = "leaf"
    for _ in range(34):
        nested = [nested]
    with pytest.raises(ValueError, match="nested at most"):
        memory_checkpoint(session_id, decisions=nested)


def test_bootstrap_returns_latest_checkpoint_and_prioritizes_open_sessions():
    ended_newer = session_start(project_key="ranked", agent="ended")
    memory_checkpoint(ended_newer, decisions="old checkpoint")
    memory_checkpoint(ended_newer, decisions="latest checkpoint")
    session_end(ended_newer, status="failed")

    active_older = session_start(project_key="ranked", agent="active")
    memory_checkpoint(active_older, decisions="active checkpoint")

    result = memory_bootstrap("ranked")
    assert [item["session_id"] for item in result["bootstraps"]] == [
        active_older,
        ended_newer,
    ]
    assert result["bootstraps"][1]["decisions"] == "latest checkpoint"


def test_bootstrap_budget_counts_complete_envelope():
    first = session_start(project_key="budgeted", agent="first")
    memory_checkpoint(first, decisions="first")
    second = session_start(project_key="budgeted", agent="second")
    memory_checkpoint(second, decisions="second")

    complete = memory_bootstrap("budgeted")
    complete_size = len(json.dumps(complete, ensure_ascii=False))
    assert len(memory_bootstrap("budgeted", complete_size)["bootstraps"]) == 2

    smaller = memory_bootstrap("budgeted", complete_size - 1)
    assert len(smaller["bootstraps"]) == 1
    assert len(json.dumps(smaller, ensure_ascii=False)) <= complete_size - 1


@pytest.mark.parametrize("budget", [0, -1, True, "1000", 200_001])
def test_bootstrap_rejects_invalid_budget(budget: object):
    with pytest.raises(ValueError, match="budget_chars"):
        memory_bootstrap("project", budget)  # type: ignore[arg-type]


def test_bootstrap_rejects_budget_smaller_than_empty_envelope():
    with pytest.raises(ValueError, match="too small"):
        memory_bootstrap("project", 1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"project_key": ""}, "project_key"),
        ({"project_key": "x" * 257}, "project_key"),
        ({"project_key": " project"}, "project_key"),
        ({"agent": ""}, "agent"),
        ({"workspace": 123}, "workspace"),
        ({"branch": "x" * 1025}, "branch"),
        ({"goal": "x" * 100_001}, "goal"),
    ],
)
def test_session_start_validates_inputs(kwargs: dict[str, object], message: str):
    arguments: dict[str, object] = {"project_key": "project", "agent": "agent"}
    arguments.update(kwargs)
    with pytest.raises(ValueError, match=message):
        session_start(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [object(), {1: "non-string key"}, {"nested": object()}],
)
def test_checkpoint_rejects_unsupported_structured_values(value: object):
    session_id = session_start(project_key="project", agent="agent")
    with pytest.raises(ValueError, match="must|keys|JSON-compatible"):
        memory_checkpoint(session_id, decisions=value)


def test_checkpoint_rejects_oversized_structured_value():
    session_id = session_start(project_key="project", agent="agent")
    with pytest.raises(ValueError, match="at most"):
        memory_checkpoint(session_id, decisions={"text": "x" * 100_001})


def test_unknown_session_is_rejected():
    unknown = "0" * 32
    with pytest.raises(ValueError, match="Unknown session"):
        memory_checkpoint(unknown, decisions="missing")
    with pytest.raises(ValueError, match="Unknown session"):
        session_end(unknown)


def test_invalid_session_identifier_is_rejected():
    with pytest.raises(ValueError, match="session_id"):
        session_end("not-a-session-id")


def test_restart_persistence_and_schema_version():
    session_id = session_start(project_key="restart", agent="agent")
    memory_checkpoint(session_id, decisions=["survive restart"])
    assert _get_db().execute("PRAGMA user_version").fetchone()[0] == 1
    _close_local_db()

    result = memory_bootstrap("restart")
    assert result["bootstraps"][0]["decisions"] == ["survive restart"]
    assert _get_db().execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_concurrent_production_writes():
    session_id = session_start(project_key="concurrent", agent="agent")
    errors: list[Exception] = []

    def write_checkpoint(index: int) -> None:
        try:
            memory_checkpoint(session_id, decisions=[f"decision-{index}"])
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            _close_local_db()

    threads = [threading.Thread(target=write_checkpoint, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    count = _get_db().execute(
        "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    assert count == 12
    assert len(memory_bootstrap("concurrent")["bootstraps"]) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_database_permissions_are_restrictive(isolated_memory: Path):
    session_start(project_key="permissions", agent="agent")
    db_path = isolated_memory / "session_memory.db"
    assert db_path.stat().st_mode & 0o777 == 0o600
