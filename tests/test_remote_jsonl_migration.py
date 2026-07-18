"""Safety tests for the example remote writer's JSONL-to-SQLite migration
(examples/remote/tools/mindsync_fact.py::_migrate_jsonl).

This module lives under examples/ (a sample remote deployment script, not
part of the installable `mindsync` package), so it's loaded directly from
its file path rather than imported normally.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "remote"
    / "tools"
    / "mindsync_fact.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("mindsync_fact_remote", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def remote_mod():
    return _load_module()


def _fact_line(fact_id: str, text: str = "hello") -> str:
    return json.dumps(
        {
            "fact_id": fact_id,
            "timestamp": "2026-07-16T00:00:00+00:00",
            "agent": "agent-a",
            "entity": "e",
            "attribute": "attr",
            "text": text,
            "source": "agent:agent-a",
            "confidence": 1.0,
        }
    )


def _fetch_fact_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT fact_id FROM facts").fetchall()
    return {r[0] for r in rows}


def test_claim_before_read_preserves_concurrent_appends(tmp_path, monkeypatch, remote_mod):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    jsonl_path = data_dir / "facts.jsonl"
    jsonl_path.write_text(_fact_line("f1") + "\n" + _fact_line("f2") + "\n", encoding="utf-8")

    conn = remote_mod._init_db(data_dir / "facts.db")

    real_rename = remote_mod.os.rename
    appended = {"done": False}

    def rename_then_append(src, dst):
        # Perform the real atomic claim first...
        real_rename(src, dst)
        # ...then simulate a writer appending a brand-new fact to the
        # (now-recreated) live path *during* the migration window.
        with open(src, "a", encoding="utf-8") as fh:
            fh.write(_fact_line("concurrent-1", text="appended during migration") + "\n")
        appended["done"] = True

    monkeypatch.setattr(remote_mod.os, "rename", rename_then_append)

    remote_mod._migrate_jsonl(conn, jsonl_path)

    assert appended["done"] is True
    # The two pre-claim facts made it into the DB.
    assert {"f1", "f2"} <= _fetch_fact_ids(conn)
    # The concurrently-appended fact must NOT be lost: it lives on at the
    # live path, ready for the next migration pass.
    assert jsonl_path.exists()
    assert "concurrent-1" in jsonl_path.read_text(encoding="utf-8")
    assert "concurrent-1" not in _fetch_fact_ids(conn)

    # A later, uninterrupted migration pass picks up the concurrent append.
    monkeypatch.setattr(remote_mod.os, "rename", real_rename)
    remote_mod._migrate_jsonl(conn, jsonl_path)
    assert "concurrent-1" in _fetch_fact_ids(conn)
    assert not jsonl_path.exists()


def test_malformed_lines_are_quarantined_not_dropped(tmp_path, remote_mod):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    jsonl_path = data_dir / "facts.jsonl"
    jsonl_path.write_text(
        _fact_line("good-1")
        + "\n"
        + "{not valid json at all\n"
        + "42\n"  # valid JSON, but not an object
        + "\n",
        encoding="utf-8",
    )

    conn = remote_mod._init_db(data_dir / "facts.db")
    remote_mod._migrate_jsonl(conn, jsonl_path)

    assert _fetch_fact_ids(conn) == {"good-1"}

    dead_letter_path = data_dir / "dead_letter.jsonl"
    assert dead_letter_path.exists()
    lines = [
        json.loads(line) for line in dead_letter_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 2
    reasons = {rec["error"] for rec in lines}
    assert reasons == {"malformed_json", "not_an_object"}
    raw_records = {rec["raw_record"] for rec in lines}
    assert "{not valid json at all" in raw_records
    assert "42" in raw_records

    # Fully consumed (valid facts migrated, garbage quarantined) -- claimed
    # file cleaned up, nothing left dangling.
    assert not jsonl_path.exists()
    assert not list(data_dir.glob("facts.jsonl.migrating-*"))


def test_failed_transaction_preserves_claimed_file_for_retry(tmp_path, monkeypatch, remote_mod):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    jsonl_path = data_dir / "facts.jsonl"
    jsonl_path.write_text(_fact_line("f1") + "\n", encoding="utf-8")

    conn = remote_mod._init_db(data_dir / "facts.db")

    # sqlite3.Connection is a C extension type -- its instances/class don't
    # allow attribute patching. Instead, force a *genuine* sqlite3 failure
    # inside the transaction by having the parse step hand back a record
    # missing bound columns the INSERT statement requires.
    def broken_parse(_claimed_path):
        return [{"fact_id": "f1"}]

    monkeypatch.setattr(remote_mod, "_parse_claimed_facts", broken_parse)

    with pytest.raises(sqlite3.ProgrammingError):
        remote_mod._migrate_jsonl(conn, jsonl_path)

    # The live path was already claimed (renamed away) -- it must not be
    # silently resurrected/clobbered; the claimed copy is what survives.
    assert not jsonl_path.exists()
    orphans = list(data_dir.glob("facts.jsonl.migrating-*"))
    assert len(orphans) == 1
    assert "f1" in orphans[0].read_text(encoding="utf-8")
    assert _fetch_fact_ids(conn) == set()

    # A later call (healthy connection) recovers the orphaned claim and
    # completes the migration -- nothing was permanently lost.
    monkeypatch.undo()
    remote_mod._migrate_jsonl(conn, jsonl_path)
    assert _fetch_fact_ids(conn) == {"f1"}
    assert not list(data_dir.glob("facts.jsonl.migrating-*"))
