"""Tier 2 semantic recall and reversible consolidation tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.memory as memory_mod
from mindsync.memory import (
    _close_local_db,
    _get_db,
    memory_bootstrap,
    memory_checkpoint,
    memory_consolidate_preview,
    memory_consolidation_apply,
    memory_consolidation_list,
    memory_consolidation_undo,
    memory_recall,
    session_start,
)


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _close_local_db()
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "mindsync-home"))
    monkeypatch.setenv("MINDSYNC_MEMORY_EMBEDDING_MODEL", "embed-test")
    monkeypatch.setenv("MINDSYNC_MEMORY_CONSOLIDATION_MODEL", "chat-test")
    config_mod.settings = config_mod.Settings()
    memory_mod.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    yield
    _close_local_db()


def _record(project: str, *facts: str) -> list[str]:
    session_ids = []
    for fact in facts:
        session_id = session_start(project_key=project, agent="tester")
        memory_checkpoint(session_id, durable_facts=[fact])
        session_ids.append(session_id)
    return session_ids


def _embed(texts: list[str], model: str) -> list[list[float]]:
    assert model == "embed-test"
    vectors = []
    for text in texts:
        lowered = text.lower()
        if "sqlite" in lowered or "database" in lowered:
            vectors.append([1.0, 0.0, 0.0])
        elif "privacy" in lowered or "redact" in lowered:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return vectors


def test_semantic_recall_is_project_scoped_cached_and_does_not_store_cue():
    _record(
        "alpha",
        "SQLite stores durable project memory",
        "Redaction protects memory privacy",
    )
    _record("beta", "SQLite belongs to another project")
    raw_cue = "database token ghp_abcdefghijklmnopqrstuvwxyz0123456789"

    first = memory_recall("alpha", raw_cue, _embedder=_embed)
    second = memory_recall("alpha", "database", _embedder=_embed)

    assert first["indexed"] == 2
    assert first["matches"][0]["text"] == "SQLite stores durable project memory"
    assert all("another project" not in item["text"] for item in first["matches"])
    assert second["indexed"] == 0
    dump = "\n".join(_get_db().iterdump())
    assert raw_cue not in dump
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in dump


def test_empty_recall_has_stable_indexed_field():
    result = memory_recall("empty", "cue", _embedder=_embed)
    assert result == {
        "project_key": "empty",
        "model": "embed-test",
        "indexed": 0,
        "matches": [],
    }


def test_recall_indexes_in_bounded_batches_with_forward_progress():
    _record("alpha", *(f"durable fact {index}" for index in range(70)))
    calls: list[int] = []

    def flaky_embed(texts: list[str], model: str) -> list[list[float]]:
        calls.append(len(texts))
        if len(calls) == 3:
            raise RuntimeError("simulated third-batch failure")
        return [[1.0, 0.0, 0.0] for _ in texts]

    with pytest.raises(RuntimeError, match="third-batch"):
        memory_recall("alpha", "cue", _embedder=flaky_embed)

    # One query probe plus one committed batch made durable forward progress.
    assert calls[:3] == [1, 32, 32]
    assert _get_db().execute(
        "SELECT COUNT(*) FROM fact_embeddings"
    ).fetchone()[0] == 32

    retry_calls: list[int] = []

    def healthy_embed(texts: list[str], model: str) -> list[list[float]]:
        retry_calls.append(len(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = memory_recall("alpha", "cue", _embedder=healthy_embed)
    assert result["indexed"] == 38
    assert max(retry_calls) <= 32
    assert _get_db().execute(
        "SELECT COUNT(*) FROM fact_embeddings"
    ).fetchone()[0] == 70


def test_same_model_dimension_change_reindexes_cached_facts():
    _record("alpha", "first fact", "second fact")

    def embed_three(texts: list[str], model: str) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    memory_recall("alpha", "cue", _embedder=embed_three)

    def embed_four(texts: list[str], model: str) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    result = memory_recall("alpha", "cue", _embedder=embed_four)
    assert result["indexed"] == 2
    assert len(result["matches"]) == 2
    assert {
        row["dimensions"]
        for row in _get_db().execute(
            "SELECT dimensions FROM fact_embeddings"
        ).fetchall()
    } == {4}


def test_recall_index_population_is_capped(
    monkeypatch: pytest.MonkeyPatch,
):
    _record("alpha", *(f"fact {index}" for index in range(5)))
    monkeypatch.setattr(memory_mod, "_MAX_RECALL_INDEX_FACTS", 3)

    result = memory_recall("alpha", "cue", _embedder=_embed)

    assert result["indexed"] == 3
    assert _get_db().execute(
        "SELECT COUNT(*) FROM fact_embeddings"
    ).fetchone()[0] == 3


def test_recall_normalizes_multiline_whitespace_without_persisting_cue():
    _record("alpha", "SQLite database fact")
    seen: list[list[str]] = []

    def capture_embed(texts: list[str], model: str) -> list[list[float]]:
        seen.append(texts)
        return _embed(texts, model)

    memory_recall("alpha", "  database\nstack\ttrace  ", _embedder=capture_embed)

    assert seen[0] == ["database stack trace"]
    assert "database\nstack\ttrace" not in "\n".join(_get_db().iterdump())


def test_recall_rejects_missing_model_before_provider_call(monkeypatch: pytest.MonkeyPatch):
    _record("alpha", "SQLite fact")
    monkeypatch.setattr(memory_mod.settings, "memory_embedding_model", "")

    with pytest.raises(ValueError, match="embedding_model"):
        memory_recall("alpha", "database", _embedder=_embed)


def test_consolidation_preview_apply_and_undo_preserve_provenance():
    session_ids = _record(
        "alpha",
        "SQLite stores session memory locally",
        "The database keeps project facts locally",
        "Redaction protects secrets",
    )
    seen: list[list[dict[str, str]]] = []

    def consolidate(facts: list[dict[str, str]], model: str) -> dict[str, object]:
        assert model == "chat-test"
        seen.append(facts)
        return {
            "text": "SQLite is the local durable store for project memory",
            "supporting_fact_ids": [item["fact_id"] for item in facts],
        }

    preview = memory_consolidate_preview(
        "alpha",
        limit=3,
        min_similarity=0.9,
        _embedder=_embed,
        _consolidator=consolidate,
    )

    assert preview["status"] == "pending"
    assert len(preview["sources"]) == 2
    assert len(seen[0]) == 2
    before_apply = memory_bootstrap("alpha")["project_facts"]
    assert "SQLite stores session memory locally" in before_apply
    assert "The database keeps project facts locally" in before_apply

    applied = memory_consolidation_apply(preview["proposal_id"])
    generated_id = applied["fact_id"]
    after_apply = memory_bootstrap("alpha")["project_facts"]
    assert preview["proposed_text"] in after_apply
    assert "SQLite stores session memory locally" not in after_apply
    assert "The database keeps project facts locally" not in after_apply
    assert "Redaction protects secrets" in after_apply

    provenance = _get_db().execute(
        "SELECT COUNT(*) FROM fact_sources WHERE fact_id = ?", (generated_id,)
    ).fetchone()[0]
    assert provenance == 2

    # Reasserting a superseded source strengthens its generated replacement too.
    memory_checkpoint(
        session_ids[0], durable_facts=["SQLite stores session memory locally"]
    )
    generated_count = _get_db().execute(
        "SELECT source_count FROM facts WHERE fact_id = ?", (generated_id,)
    ).fetchone()[0]
    assert generated_count == 3

    undone = memory_consolidation_undo(generated_id)
    assert undone["status"] == "undone"
    restored = memory_bootstrap("alpha")["project_facts"]
    assert preview["proposed_text"] not in restored
    assert "SQLite stores session memory locally" in restored
    assert "The database keeps project facts locally" in restored
    assert _get_db().execute(
        "SELECT 1 FROM facts WHERE fact_id = ?", (generated_id,)
    ).fetchone() is None
    audit = memory_consolidation_list(project_key="alpha", status="undone")
    assert audit[0]["applied_fact_id"] == generated_id
    assert audit[0]["source_fact_ids"] == applied["source_fact_ids"]


def test_consolidation_preview_rejects_verbatim_source_copy():
    _record("alpha", "SQLite stores session memory", "Database facts stay local")

    def copy_source(facts: list[dict[str, str]], model: str) -> dict[str, object]:
        return {
            "text": facts[0]["text"],
            "supporting_fact_ids": [item["fact_id"] for item in facts],
        }

    with pytest.raises(ValueError, match="generalize rather than copy"):
        memory_consolidate_preview(
            "alpha",
            min_similarity=0.9,
            _embedder=_embed,
            _consolidator=copy_source,
        )

    assert memory_consolidation_list(project_key="alpha") == []


def test_apply_rejects_stale_proposal_without_partial_changes():
    _record("alpha", "SQLite stores session memory", "Database facts stay local")

    def consolidate(facts: list[dict[str, str]], model: str) -> dict[str, object]:
        return {
            "text": "SQLite stores local memory facts",
            "supporting_fact_ids": [item["fact_id"] for item in facts],
        }

    preview = memory_consolidate_preview(
        "alpha", min_similarity=0.9, _embedder=_embed, _consolidator=consolidate
    )
    source_id = preview["sources"][0]["fact_id"]
    _get_db().execute("DELETE FROM facts WHERE fact_id = ?", (source_id,))

    with pytest.raises(ValueError, match="sources changed"):
        memory_consolidation_apply(preview["proposal_id"])

    proposal = _get_db().execute(
        "SELECT status, applied_fact_id FROM consolidation_proposals "
        "WHERE proposal_id = ?",
        (preview["proposal_id"],),
    ).fetchone()
    assert dict(proposal) == {"status": "pending", "applied_fact_id": None}
    assert _get_db().execute(
        "SELECT COUNT(*) FROM facts WHERE is_generated = 1"
    ).fetchone()[0] == 0


def test_proposal_persists_redacted_text_only():
    _record("alpha", "SQLite stores session memory", "Database facts stay local")
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"

    def consolidate(facts: list[dict[str, str]], model: str) -> dict[str, object]:
        return {
            "text": f"SQLite facts use secret {secret}",
            "supporting_fact_ids": [item["fact_id"] for item in facts],
        }

    preview = memory_consolidate_preview(
        "alpha", min_similarity=0.9, _embedder=_embed, _consolidator=consolidate
    )

    assert "[REDACTED]" in preview["proposed_text"]
    assert secret not in json.dumps(preview)
    assert secret not in "\n".join(_get_db().iterdump())


def test_consolidation_model_input_and_preview_sources_are_bounded():
    long_prefix = "SQLite database "
    _record(
        "alpha",
        long_prefix + "a" * 50_000,
        long_prefix + "b" * 50_000,
        long_prefix + "c" * 50_000,
    )

    def consolidate(facts: list[dict[str, str]], model: str) -> dict[str, object]:
        assert sum(len(item["fact_id"]) + len(item["text"]) for item in facts) <= 40_000
        assert all(len(item["text"]) <= 16_000 for item in facts)
        return {
            "text": "SQLite stores bounded local database facts",
            "supporting_fact_ids": [item["fact_id"] for item in facts],
        }

    preview = memory_consolidate_preview(
        "alpha", min_similarity=0.9, _embedder=_embed, _consolidator=consolidate
    )

    assert len(preview["sources"]) == 2
    assert all(item["truncated"] is True for item in preview["sources"])


def test_consolidation_similarity_limit_is_applied_within_candidates(
    monkeypatch: pytest.MonkeyPatch,
):
    _record(
        "alpha",
        "pair-a",
        "pair-b",
        *(f"decoy-{index}" for index in range(8)),
    )

    def vectors(texts: list[str], model: str) -> list[list[float]]:
        result = []
        for text in texts:
            if "pair-a" in text:
                result.append([1.0, 0.1])
            elif "pair-b" in text:
                result.append([1.0, -0.1])
            else:
                result.append([1.0, 0.05])
        return result

    memory_recall("alpha", "decoy", limit=50, _embedder=vectors)
    _get_db().execute(
        "UPDATE facts SET source_count = 100 WHERE text IN ('pair-a', 'pair-b')"
    )
    monkeypatch.setattr(memory_mod, "_MAX_CONSOLIDATION_FACTS", 2)

    def consolidate(facts: list[dict[str, str]], model: str) -> dict[str, object]:
        return {
            "text": "the pair stays related",
            "supporting_fact_ids": [item["fact_id"] for item in facts],
        }

    preview = memory_consolidate_preview(
        "alpha",
        limit=2,
        min_similarity=0.9,
        _embedder=vectors,
        _consolidator=consolidate,
    )
    assert {item["text"] for item in preview["sources"]} == {"pair-a", "pair-b"}


def test_pending_consolidation_proposals_are_capped(
    monkeypatch: pytest.MonkeyPatch,
):
    _record("alpha", "SQLite one", "SQLite two")
    monkeypatch.setattr(memory_mod, "_MAX_PENDING_CONSOLIDATIONS_PER_PROJECT", 1)

    def consolidate(facts: list[dict[str, str]], model: str) -> dict[str, object]:
        return {
            "text": "SQLite combined",
            "supporting_fact_ids": [item["fact_id"] for item in facts],
        }

    memory_consolidate_preview(
        "alpha", min_similarity=0.9, _embedder=_embed, _consolidator=consolidate
    )
    with pytest.raises(ValueError, match="too many pending"):
        memory_consolidate_preview(
            "alpha",
            min_similarity=0.9,
            _embedder=_embed,
            _consolidator=consolidate,
        )


def test_schema_v2_upgrade_is_additive_and_preserves_existing_fact():
    _close_local_db()
    db_path = config_mod.settings.memory_db_file
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, project_key TEXT NOT NULL,
            agent TEXT NOT NULL, workspace TEXT, branch TEXT, goal TEXT,
            status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT
        );
        CREATE TABLE checkpoints (
            checkpoint_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL, status TEXT, decisions TEXT,
            files_changed TEXT, tests TEXT, pending TEXT, blockers TEXT,
            durable_facts TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY, project_key TEXT NOT NULL, text TEXT NOT NULL,
            first_seen TEXT NOT NULL, last_recalled TEXT,
            recall_count INTEGER NOT NULL DEFAULT 0,
            source_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE fact_sources (
            fact_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
            checkpoint_id TEXT NOT NULL
                REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
            PRIMARY KEY (fact_id, checkpoint_id)
        );
        CREATE UNIQUE INDEX idx_facts_project_text ON facts(project_key, text);
        INSERT INTO facts (
            fact_id, project_key, text, first_seen, source_count
        ) VALUES (
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'legacy', 'preserved fact',
            '2026-08-20T00:00:00+00:00', 2
        );
        PRAGMA user_version = 2;
        """
    )
    db.close()

    upgraded = _get_db()

    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 3
    fact = upgraded.execute(
        "SELECT text, source_count, is_generated, superseded_by FROM facts"
    ).fetchone()
    assert dict(fact) == {
        "text": "preserved fact",
        "source_count": 2,
        "is_generated": 0,
        "superseded_by": None,
    }
    tables = {
        row["name"]
        for row in upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"fact_embeddings", "consolidation_proposals"} <= tables


def test_generated_facts_do_not_crowd_out_the_consolidation_window():
    """Generated facts outrank their own sources, so filtering them after the
    LIMIT silently empties the candidate window and wedges consolidation shut."""
    from mindsync.memory import _MAX_CONSOLIDATION_FACTS, _active_fact_rows, _get_db, _utc_now

    db = _get_db()
    now = _utc_now()
    for i in range(30):
        db.execute(
            "INSERT INTO facts (fact_id, project_key, text, first_seen,"
            " recall_count, source_count, is_generated) VALUES (?,?,?,?,1,1,0)",
            (f"plain{i:03d}", "wedge", f"plain fact {i}", now),
        )
    # apply() seeds a generated fact with the summed source_count of everything
    # it replaced, so these sort above every plain fact.
    for i in range(_MAX_CONSOLIDATION_FACTS):
        db.execute(
            "INSERT INTO facts (fact_id, project_key, text, first_seen,"
            " recall_count, source_count, is_generated) VALUES (?,?,?,?,5,40,1)",
            (f"gen{i:03d}", "wedge", f"generated fact {i}", now),
        )

    candidates = _active_fact_rows(
        db, "wedge", _MAX_CONSOLIDATION_FACTS, exclude_generated=True
    )
    assert len(candidates) == _MAX_CONSOLIDATION_FACTS
    assert all(row["is_generated"] == 0 for row in candidates)

    # Recall must still see generated facts — they are the better answer.
    everything = _active_fact_rows(db, "wedge", _MAX_CONSOLIDATION_FACTS)
    assert any(row["is_generated"] for row in everything)


def test_applying_a_proposal_retires_the_ones_it_invalidates():
    """A proposal citing a now-superseded fact can never apply. Left pending it
    counts against the per-project cap forever, with no way to clear it."""
    from mindsync.memory import _get_db, _utc_now, memory_consolidation_apply

    db = _get_db()
    now = _utc_now()
    for fid, text in (("f1", "alpha"), ("f2", "beta"), ("f3", "gamma")):
        db.execute(
            "INSERT INTO facts (fact_id, project_key, text, first_seen,"
            " recall_count, source_count, is_generated) VALUES (?,?,?,?,1,1,0)",
            (fid, "retire", text, now),
        )
    for pid, sources, text in (
        ("aa" * 16, ["f1", "f2"], "alpha and beta"),
        ("bb" * 16, ["f2", "f3"], "beta and gamma"),
    ):
        db.execute(
            "INSERT INTO consolidation_proposals (proposal_id, project_key, model,"
            " source_fact_ids, proposed_text, status, created_at)"
            " VALUES (?,?,?,?,?,'pending',?)",
            (pid, "retire", "test-model", json.dumps(sources), text, now),
        )

    result = memory_consolidation_apply("aa" * 16)
    assert "bb" * 16 in result["superseded_proposals"]

    status = db.execute(
        "SELECT status FROM consolidation_proposals WHERE proposal_id = ?",
        ("bb" * 16,),
    ).fetchone()["status"]
    assert status == "superseded"
    audit = memory_consolidation_list(project_key="retire", status="superseded")
    assert [proposal["proposal_id"] for proposal in audit] == ["bb" * 16]
    assert db.execute(
        "SELECT COUNT(*) FROM consolidation_proposals WHERE status = 'pending'"
    ).fetchone()[0] == 0


def test_undo_restores_the_proposals_the_application_retired():
    """Undoing an application makes its retired proposals applicable again.

    Applying a proposal supersedes its sources and retires every proposal
    citing them. Undo restores those source facts, so leaving the retired
    proposals at 'superseded' strands work that is valid once more.
    """
    from mindsync.memory import (
        _get_db,
        _utc_now,
        memory_consolidation_apply,
        memory_consolidation_undo,
    )

    db = _get_db()
    now = _utc_now()
    for fid, text in (("u1", "one"), ("u2", "two"), ("u3", "three")):
        db.execute(
            "INSERT INTO facts (fact_id, project_key, text, first_seen,"
            " recall_count, source_count, is_generated) VALUES (?,?,?,?,1,1,0)",
            (fid, "undo-proj", text, now),
        )
    applied_id, retired_id = "cc" * 16, "dd" * 16
    for pid, sources, text in (
        (applied_id, ["u1", "u2"], "one and two"),
        (retired_id, ["u2", "u3"], "two and three"),
    ):
        db.execute(
            "INSERT INTO consolidation_proposals (proposal_id, project_key, model,"
            " source_fact_ids, proposed_text, status, created_at)"
            " VALUES (?,?,?,?,?,'pending',?)",
            (pid, "undo-proj", "test-model", json.dumps(sources), text, now),
        )

    result = memory_consolidation_apply(applied_id)
    assert retired_id in result["superseded_proposals"]

    memory_consolidation_undo(result["fact_id"])

    statuses = {
        row["proposal_id"]: row["status"]
        for row in db.execute(
            "SELECT proposal_id, status FROM consolidation_proposals"
            " WHERE project_key = 'undo-proj'"
        ).fetchall()
    }
    assert statuses[retired_id] == "pending", statuses
    assert statuses[applied_id] == "undone", statuses
