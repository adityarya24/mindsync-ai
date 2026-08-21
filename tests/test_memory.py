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
    # Strict priority classes: durable-fact sessions first, unresolved
    # blockers/pending second, routine history last.
    assert order == [important, blocked, routine_old]


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


def test_bootstrap_surfaces_durable_session_beyond_routine_flood():
    durable = session_start(project_key="flood", agent="agent")
    memory_checkpoint(durable, durable_facts=["anchor fact"])
    session_end(durable)
    for _ in range(205):
        routine = session_start(project_key="flood", agent="agent")
        memory_checkpoint(routine, decisions=["routine work"])
        session_end(routine)

    result = memory_bootstrap("flood", budget_chars=200_000)
    order = [entry["session_id"] for entry in result["bootstraps"]]
    # The old durable session must lead despite 205 newer routine sessions.
    assert order[0] == durable
    assert len(order) <= 2 * memory_mod._MAX_BOOTSTRAP_SESSIONS_PER_CLASS + 1


def test_bootstrap_preserves_durable_facts_from_older_checkpoints():
    session_id = session_start(project_key="facts-mid", agent="agent")
    memory_checkpoint(session_id, durable_facts=["key architecture fact"])
    memory_checkpoint(session_id, decisions=["later routine decision"])
    session_end(session_id)

    entry = memory_bootstrap("facts-mid")["bootstraps"][0]
    assert entry["durable_facts"] == ["key architecture fact"]
    assert entry["decisions"] == ["later routine decision"]


def test_prune_protects_durable_fact_in_any_checkpoint():
    durable_mid = session_start(project_key="prune-any", agent="agent")
    memory_checkpoint(durable_mid, durable_facts=["old but gold"])
    memory_checkpoint(durable_mid, decisions=["routine follow-up"])
    session_end(durable_mid)
    plain = session_start(project_key="prune-any", agent="agent")
    session_end(plain)

    result = memory_prune(project_key="prune-any", dry_run=False)
    assert result["protected_durable"] == 1
    assert result["deleted"] == 1
    db = _get_db()
    remaining = {
        row["session_id"]
        for row in db.execute(
            "SELECT session_id FROM sessions WHERE project_key = 'prune-any'"
        ).fetchall()
    }
    assert remaining == {durable_mid}


def test_prune_rechecks_protection_at_deletion_time():
    target = session_start(project_key="prune-recheck", agent="agent")
    session_end(target)

    dry = memory_prune(project_key="prune-recheck", dry_run=True)
    assert dry["candidates"] == 1

    # A durable checkpoint lands after the dry run saw the candidate.
    memory_checkpoint(target, durable_facts=["arrived late"])

    result = memory_prune(project_key="prune-recheck", dry_run=False)
    assert result["deleted"] == 0
    assert result["protected_durable"] == 1
    stats = memory_stats()
    assert stats["total_sessions"] == 1


def test_memory_prune_rejects_non_bool_dry_run():
    ended = session_start(project_key="prune-bool", agent="agent")
    session_end(ended)
    for bad_value in (None, 0, 1, "yes", "false"):
        with pytest.raises(ValueError, match="dry_run must be a boolean"):
            memory_prune(project_key="prune-bool", dry_run=bad_value)
    assert memory_stats()["total_sessions"] == 1


def test_memory_list_orders_by_latest_checkpoint_activity():
    older = session_start(project_key="list-order", agent="agent")
    memory_checkpoint(older, decisions=["older session works"])
    newer = session_start(project_key="list-order", agent="agent")

    # Force the older session's checkpoint to be the newest activity overall.
    _get_db().execute(
        "UPDATE checkpoints SET timestamp = ? WHERE session_id = ?",
        ("2027-01-01T00:00:00+00:00", older),
    )

    entries = memory_list(project_key="list-order")
    assert [entry["session_id"] for entry in entries] == [older, newer]


def test_bootstrap_normalizes_string_and_dict_durable_facts():
    session_id = session_start(project_key="fact-types", agent="agent")
    memory_checkpoint(session_id, durable_facts=["older list fact"])
    memory_checkpoint(session_id, durable_facts={"newer": "dict fact"})
    memory_checkpoint(session_id, durable_facts="newest plain string")
    session_end(session_id)

    entry = memory_bootstrap("fact-types")["bootstraps"][0]
    assert entry["durable_facts"] == [
        "newest plain string",
        {"newer": "dict fact"},
        "older list fact",
    ]


def test_prune_keep_last_applies_before_age_filter():
    fresh = session_start(project_key="prune-fresh", agent="agent")
    session_end(fresh)
    stale = session_start(project_key="prune-fresh", agent="agent")
    session_end(stale)
    _get_db().execute(
        "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
        ("2026-01-01T00:00:00+00:00", stale),
    )

    result = memory_prune(
        project_key="prune-fresh",
        older_than_days=30,
        keep_last=1,
        dry_run=False,
    )
    # The fresh session already satisfies keep-last, so the stale one must
    # not be retained a second time.
    assert result["deleted"] == 1
    assert result["kept_by_keep_last"] == 1
    db = _get_db()
    remaining = {
        row["session_id"]
        for row in db.execute(
            "SELECT session_id FROM sessions WHERE project_key = 'prune-fresh'"
        ).fetchall()
    }
    assert remaining == {fresh}


def test_memory_list_orders_by_greatest_activity_timestamp():
    ended_late = session_start(project_key="greatest", agent="agent")
    memory_checkpoint(ended_late, decisions=["work"])
    started_recently = session_start(project_key="greatest", agent="agent")

    db = _get_db()
    db.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ? WHERE session_id = ?",
        ("2026-01-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", ended_late),
    )
    db.execute(
        "UPDATE checkpoints SET timestamp = ? WHERE session_id = ?",
        ("2026-02-01T00:00:00+00:00", ended_late),
    )
    db.execute(
        "UPDATE sessions SET started_at = ? WHERE session_id = ?",
        ("2026-02-15T00:00:00+00:00", started_recently),
    )

    entries = memory_list(project_key="greatest")
    # ended_late's newest activity is its end (March), which beats the other
    # session's start (mid-Feb) even though its last checkpoint is older.
    assert [entry["session_id"] for entry in entries] == [
        ended_late,
        started_recently,
    ]


def test_bootstrap_skips_oversized_entries_within_tight_budget():
    big = session_start(project_key="budget-bound", agent="agent")
    memory_checkpoint(big, decisions=["x" * 90_000])
    session_end(big)
    small = session_start(project_key="budget-bound", agent="agent")
    memory_checkpoint(small, decisions=["tiny"])
    session_end(small)

    result = memory_bootstrap("budget-bound", budget_chars=1_000)
    ids = [entry["session_id"] for entry in result["bootstraps"]]
    assert small in ids
    assert big not in ids
    assert len(json.dumps(result, ensure_ascii=False)) <= 1_000

def test_prune_keep_last_counts_durable_sessions_per_project_in_dry_run():
    expected_targets = set()
    for project in ("prune-slots-a", "prune-slots-b"):
        older_plain = session_start(project_key=project, agent="agent")
        session_end(older_plain)
        expected_targets.add(older_plain)

        newer_durable = session_start(project_key=project, agent="agent")
        memory_checkpoint(newer_durable, durable_facts=["protected"])
        session_end(newer_durable)

    result = memory_prune(keep_last=1, dry_run=True)

    assert result["protected_durable"] == 2
    assert result["kept_by_keep_last"] == 0
    assert result["candidates"] == 2
    assert set(result["session_ids"]) == expected_targets
    assert memory_stats()["total_sessions"] == 4


def test_bootstrap_accepts_exact_budget_and_rejects_boundary_minus_one():
    target = session_start(project_key="exact-bound", agent="agent")
    memory_checkpoint(target, decisions=["x" * 90_000])
    session_end(target)

    complete = memory_bootstrap("exact-bound", budget_chars=100_000)
    exact_size = len(json.dumps(complete, ensure_ascii=False))

    assert memory_bootstrap("exact-bound", exact_size) == complete
    assert memory_bootstrap("exact-bound", exact_size - 1)["bootstraps"] == []


def test_bootstrap_orders_by_greatest_activity_with_deterministic_ties():
    checkpoint_wins = session_start(project_key="activity-order", agent="agent")
    memory_checkpoint(checkpoint_wins, decisions=["checkpoint wins"])
    session_end(checkpoint_wins)
    ended_wins = session_start(project_key="activity-order", agent="agent")
    memory_checkpoint(ended_wins, decisions=["end wins"])
    session_end(ended_wins)
    started_wins = session_start(project_key="activity-order", agent="agent")
    tied_older = session_start(project_key="activity-order", agent="agent")
    tied_newer = session_start(project_key="activity-order", agent="agent")

    db = _get_db()
    db.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ? WHERE session_id = ?",
        ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00", checkpoint_wins),
    )
    db.execute(
        "UPDATE checkpoints SET timestamp = ? WHERE session_id = ?",
        ("2026-05-01T00:00:00+00:00", checkpoint_wins),
    )
    db.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ? WHERE session_id = ?",
        ("2026-01-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00", ended_wins),
    )
    db.execute(
        "UPDATE checkpoints SET timestamp = ? WHERE session_id = ?",
        ("2026-03-01T00:00:00+00:00", ended_wins),
    )
    db.execute(
        "UPDATE sessions SET started_at = ? WHERE session_id = ?",
        ("2026-03-15T00:00:00+00:00", started_wins),
    )
    for session_id in (tied_older, tied_newer):
        db.execute(
            "UPDATE sessions SET started_at = ? WHERE session_id = ?",
            ("2026-02-15T00:00:00+00:00", session_id),
        )

    result = memory_bootstrap("activity-order")

    assert [entry["session_id"] for entry in result["bootstraps"]] == [
        checkpoint_wins,
        ended_wins,
        started_wins,
        tied_newer,
        tied_older,
    ]


def test_bootstrap_does_not_load_or_decode_oversized_base_payload(monkeypatch):
    huge = session_start(project_key="base-decode", agent="agent")
    memory_checkpoint(huge, decisions=["x\\" * 30_000])
    session_end(huge)

    db = _get_db()
    oversized_json = db.execute(
        "SELECT decisions FROM checkpoints WHERE session_id = ?", (huge,)
    ).fetchone()["decisions"]
    original_decode = memory_mod._decode_structured
    original_load = memory_mod._bootstrap_payload_row

    def guarded_decode(value):
        assert value != oversized_json, "oversized base payload was decoded"
        return original_decode(value)

    def guarded_load(db, session_id, checkpoint_id):
        assert session_id != huge, "oversized base payload was loaded"
        return original_load(db, session_id, checkpoint_id)

    monkeypatch.setattr(memory_mod, "_decode_structured", guarded_decode)
    monkeypatch.setattr(memory_mod, "_bootstrap_payload_row", guarded_load)

    assert memory_bootstrap("base-decode", budget_chars=10_000)["bootstraps"] == []


def test_bootstrap_does_not_decode_oversized_historical_enrichment(monkeypatch):
    session_id = session_start(project_key="history-decode", agent="agent")
    old_checkpoint = memory_checkpoint(
        session_id,
        status="failed",
        decisions=["x\\" * 30_000],
    )
    memory_checkpoint(session_id, status="done", decisions=["small latest payload"])
    session_end(session_id)

    oversized_json = _get_db().execute(
        "SELECT decisions FROM checkpoints WHERE checkpoint_id = ?",
        (old_checkpoint,),
    ).fetchone()["decisions"]
    original_decode = memory_mod._decode_structured

    def guarded_decode(value):
        assert value != oversized_json, "oversized historical payload was decoded"
        return original_decode(value)

    monkeypatch.setattr(memory_mod, "_decode_structured", guarded_decode)

    result = memory_bootstrap("history-decode", budget_chars=10_000)
    assert result["bootstraps"] == []


def test_bootstrap_stops_durable_merge_after_first_oversized_fact(monkeypatch):
    session_id = session_start(project_key="durable-history-bound", agent="agent")
    for index in range(9):
        memory_checkpoint(session_id, durable_facts=[f"{index}:" + "x" * 80_000])
    memory_checkpoint(session_id, durable_facts=["small latest fact"])
    session_end(session_id)

    original_decode = memory_mod._decode_structured
    durable_decode_count = 0

    def counted_decode(value):
        nonlocal durable_decode_count
        if value and ("small latest fact" in value or len(value) > 70_000):
            durable_decode_count += 1
        return original_decode(value)

    monkeypatch.setattr(memory_mod, "_decode_structured", counted_decode)

    assert memory_bootstrap("durable-history-bound", budget_chars=10_000)[
        "bootstraps"
    ] == []
    assert durable_decode_count == 2


def test_bootstrap_treats_blank_latest_durable_facts_as_missing():
    session_id = session_start(project_key="blank-latest-durable", agent="agent")
    checkpoint_id = memory_checkpoint(session_id, decisions=["still useful"])
    session_end(session_id)
    _get_db().execute(
        "UPDATE checkpoints SET durable_facts = '' WHERE checkpoint_id = ?",
        (checkpoint_id,),
    )

    result = memory_bootstrap("blank-latest-durable")

    assert result["bootstraps"][0]["session_id"] == session_id
    assert "durable_facts" not in result["bootstraps"][0]


def test_bootstrap_treats_blank_earlier_durable_facts_as_missing():
    session_id = session_start(project_key="blank-earlier-durable", agent="agent")
    earlier_id = memory_checkpoint(
        session_id,
        status="failed",
        decisions=["earlier failure"],
    )
    memory_checkpoint(session_id, status="done", decisions=["latest checkpoint"])
    session_end(session_id)
    _get_db().execute(
        "UPDATE checkpoints SET durable_facts = '' WHERE checkpoint_id = ?",
        (earlier_id,),
    )

    result = memory_bootstrap("blank-earlier-durable")

    earlier = result["bootstraps"][0]["earlier_checkpoints"][0]
    assert earlier["decisions"] == ["earlier failure"]
    assert "durable_facts" not in earlier
