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
    memory_list,
    memory_prune,
    memory_show,
    memory_stats,
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


def test_memory_stats_reports_totals(isolated_memory: Path):
    session_start(project_key="stats-project", agent="agent-a")
    ended = session_start(project_key="stats-project", agent="agent-b")
    memory_checkpoint(ended, decisions=["done"])
    session_end(ended)
    other = session_start(project_key="stats-other", agent="agent-c")
    memory_checkpoint(other, durable_facts=["fact"])
    session_end(other)

    report = memory_stats()
    assert report["total_sessions"] == 3
    assert report["active_sessions"] == 1
    assert report["total_checkpoints"] == 2
    projects = {item["project_key"]: item for item in report["projects"]}
    assert projects["stats-project"]["sessions"] == 2
    assert projects["stats-project"]["active_sessions"] == 1
    assert projects["stats-other"]["sessions"] == 1
    assert report["db_size_bytes"] > 0
    assert report["db_file"].endswith("session_memory.db")


def test_memory_list_filters_by_project_and_limit():
    first = session_start(project_key="list-a", agent="agent")
    second = session_start(project_key="list-b", agent="agent")
    third = session_start(project_key="list-a", agent="agent")

    all_entries = memory_list()
    assert [entry["session_id"] for entry in all_entries] == [third, second, first]

    filtered = memory_list(project_key="list-a")
    assert [entry["session_id"] for entry in filtered] == [third, first]
    assert filtered[0]["checkpoint_count"] == 0

    limited = memory_list(limit=1)
    assert [entry["session_id"] for entry in limited] == [third]


def test_memory_list_rejects_bad_input():
    with pytest.raises(ValueError, match="limit"):
        memory_list(limit=0)
    with pytest.raises(ValueError, match="limit"):
        memory_list(limit=10_000)
    with pytest.raises(ValueError, match="project_key"):
        memory_list(project_key="")


def test_memory_show_returns_all_checkpoints():
    session_id = session_start(
        project_key="show-project", agent="agent", branch="main"
    )
    memory_checkpoint(session_id, decisions=["first"], blockers=["blocked"])
    memory_checkpoint(session_id, status="working", pending=["next"])
    session_end(session_id, status="completed")

    shown = memory_show(session_id)
    assert shown["session_id"] == session_id
    assert shown["project_key"] == "show-project"
    assert shown["branch"] == "main"
    assert shown["session_status"] == "completed"
    assert len(shown["checkpoints"]) == 2
    assert shown["checkpoints"][0]["decisions"] == ["first"]
    assert shown["checkpoints"][0]["blockers"] == ["blocked"]
    assert shown["checkpoints"][1]["pending"] == ["next"]
    assert "decisions" not in shown["checkpoints"][1]


def test_memory_show_unknown_session():
    with pytest.raises(ValueError, match="Unknown session"):
        memory_show("0" * 32)


def test_bootstrap_prioritizes_durable_and_unresolved_sessions():
    routine_old = session_start(project_key="prio", agent="agent")
    memory_checkpoint(routine_old, decisions=["routine"])
    session_end(routine_old)

    important = session_start(project_key="prio", agent="agent")
    memory_checkpoint(important, durable_facts=["must survive"])
    session_end(important)

    blocked = session_start(project_key="prio", agent="agent")
    memory_checkpoint(blocked, blockers=["waiting on review"])

    result = memory_bootstrap("prio", budget_chars=2000)
    order = [entry["session_id"] for entry in result["bootstraps"]]
    # Active blocked session first (active class), then the durable-fact
    # session must beat the older routine one even though it is newer.
    assert order[0] == blocked
    assert order.index(important) < order.index(routine_old)


def test_bootstrap_includes_earlier_important_checkpoints():
    session_id = session_start(project_key="earlier", agent="agent")
    failed_id = memory_checkpoint(session_id, status="failed", decisions=["try A"])
    memory_checkpoint(session_id, status="done", decisions=["try B worked"])
    session_end(session_id)

    result = memory_bootstrap("earlier")
    entry = result["bootstraps"][0]
    assert entry["decisions"] == ["try B worked"]
    earlier = entry["earlier_checkpoints"]
    assert len(earlier) == 1
    assert earlier[0]["status"] == "failed"
    assert earlier[0]["decisions"] == ["try A"]
    assert failed_id  # checkpoint was created


def test_bootstrap_session_scan_is_bounded():
    for _ in range(202):
        session_id = session_start(project_key="bounded", agent="agent")
        session_end(session_id)

    result = memory_bootstrap("bounded", budget_chars=200_000)
    assert len(result["bootstraps"]) == 200


def test_prune_dry_run_does_not_delete():
    keep = session_start(project_key="prune-dry", agent="agent")
    memory_checkpoint(keep, decisions=["keep me"])
    session_end(keep)

    result = memory_prune(project_key="prune-dry", dry_run=True)
    assert result["dry_run"] is True
    assert result["candidates"] == 1
    assert result["deleted"] is None
    assert result["session_ids"] == [keep]
    stats = memory_stats()
    assert stats["total_sessions"] == 1


def test_prune_deletes_ended_sessions_and_their_checkpoints():
    target = session_start(project_key="prune-run", agent="agent")
    memory_checkpoint(target, decisions=["old work"])
    session_end(target)
    active = session_start(project_key="prune-run", agent="agent")

    result = memory_prune(project_key="prune-run", dry_run=False)
    assert result["dry_run"] is False
    assert result["candidates"] == 1
    assert result["deleted"] == 1

    db = _get_db()
    remaining = db.execute(
        "SELECT session_id FROM sessions WHERE project_key = 'prune-run'"
    ).fetchall()
    assert [row["session_id"] for row in remaining] == [active]
    orphan_checkpoints = db.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?", (target,)
    ).fetchone()[0]
    assert orphan_checkpoints == 0


def test_prune_protects_active_and_durable_fact_sessions():
    durable = session_start(project_key="prune-safe", agent="agent")
    memory_checkpoint(durable, durable_facts=["permanent fact"])
    session_end(durable)
    plain = session_start(project_key="prune-safe", agent="agent")
    session_end(plain)
    active = session_start(project_key="prune-safe", agent="agent")

    result = memory_prune(project_key="prune-safe", dry_run=False)
    assert result["protected_durable"] == 1
    assert result["deleted"] == 1
    db = _get_db()
    remaining = {
        row["session_id"]
        for row in db.execute(
            "SELECT session_id FROM sessions WHERE project_key = 'prune-safe'"
        ).fetchall()
    }
    assert remaining == {durable, active}


def test_prune_keep_last_preserves_recent_sessions_per_project():
    old_one = session_start(project_key="prune-keep", agent="agent")
    session_end(old_one)
    old_two = session_start(project_key="prune-keep", agent="agent")
    session_end(old_two)
    recent = session_start(project_key="prune-keep", agent="agent")
    session_end(recent)

    result = memory_prune(project_key="prune-keep", keep_last=2, dry_run=False)
    assert result["kept_by_keep_last"] == 2
    assert result["deleted"] == 1
    db = _get_db()
    remaining = {
        row["session_id"]
        for row in db.execute(
            "SELECT session_id FROM sessions WHERE project_key = 'prune-keep'"
        ).fetchall()
    }
    assert remaining == {recent, old_two}


def test_prune_older_than_days_only_touches_old_sessions():
    fresh = session_start(project_key="prune-age", agent="agent")
    session_end(fresh)
    stale = session_start(project_key="prune-age", agent="agent")
    session_end(stale)
    _get_db().execute(
        "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
        ("2026-01-01T00:00:00+00:00", stale),
    )

    result = memory_prune(project_key="prune-age", older_than_days=30, dry_run=True)
    assert result["candidates"] == 1
    assert result["session_ids"] == [stale]


def test_prune_rejects_bad_input():
    with pytest.raises(ValueError, match="older_than_days"):
        memory_prune(older_than_days=0)
    with pytest.raises(ValueError, match="keep_last"):
        memory_prune(keep_last=-1)
