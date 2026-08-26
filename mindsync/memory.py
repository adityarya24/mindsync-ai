"""Local, structured session memory backed by SQLite."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mindsync.config import settings

_SCHEMA_VERSION = 3
_PROJECT_KEY_MAX = 256
_AGENT_MAX = 128
_WORKSPACE_MAX = 4096
_BRANCH_MAX = 1024
_TEXT_MAX = 100_000
_STATUS_MAX = 128
_MAX_BOOTSTRAP_BUDGET = 200_000
_MAX_BOOTSTRAP_SESSIONS_PER_CLASS = 200
# A JSON escape can shrink when decoded and re-serialized (for example ``\/``
# or ``\u0061``). The size probe accounts for that potential shrink instead of
# treating stored JSON length as an unconditional lower bound.
_MAX_UNICODE_ESCAPE_EXTRA_SHRINK = 5
_MAX_FACT_CHECKPOINTS_PER_SESSION = 10
_MAX_MERGED_DURABLE_FACTS = 20
_MAX_BOOTSTRAP_PROJECT_FACTS = 50
# Project facts are the highest-value payload, but they must not starve
# the session history that gives them context, so they may claim at most
# this fraction of the caller's budget.
_BOOTSTRAP_FACTS_BUDGET_DIVISOR = 4
_MAX_IMPORTANT_CHECKPOINTS = 3
_MAX_LIST_LIMIT = 500
_MAX_PRUNE_SAMPLE = 100
_MAX_RECALL_LIMIT = 50
_MAX_RECALL_INDEX_FACTS = 2_000
_MAX_CONSOLIDATION_FACTS = 20
_MAX_EMBEDDING_BATCH_SIZE = 32
_MAX_EMBEDDING_TEXT_CHARS = 16_000
_MAX_EMBEDDING_BATCH_CHARS = 64_000
_MAX_EMBEDDING_DIMENSIONS = 8_192
_MAX_RECALL_QUERY_CHARS = 16_000
_MAX_CONSOLIDATION_INPUT_CHARS = 40_000
_MAX_PENDING_CONSOLIDATIONS_PER_PROJECT = 100
_FLOAT32_MAX = 3.4028235e38
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_FACT_ID = re.compile(r"^[0-9a-f]{32}$")
_PROPOSAL_ID = re.compile(r"^[0-9a-f]{32}$")
_DURABLE_EXISTS_SQL = """
    EXISTS (
        SELECT 1 FROM checkpoints AS df
        WHERE df.session_id = s.session_id
          AND df.durable_facts IS NOT NULL
          AND df.durable_facts NOT IN ('', '[]', 'null')
    )
"""
_BOOTSTRAP_CLASS_FILTERS = (
    # 1. Sessions carrying durable facts in any checkpoint.
    f"AND {_DURABLE_EXISTS_SQL}",
    # 2. Latest checkpoint still has unresolved blockers or pending work.
    f"""
    AND NOT {_DURABLE_EXISTS_SQL}
    AND (
        COALESCE(c.blockers, '') NOT IN ('', '[]', 'null')
        OR COALESCE(c.pending, '') NOT IN ('', '[]', 'null')
    )
    """,
    # 3. Routine history.
    f"""
    AND NOT {_DURABLE_EXISTS_SQL}
    AND COALESCE(c.blockers, '') IN ('', '[]', 'null')
    AND COALESCE(c.pending, '') IN ('', '[]', 'null')
    """,
)
# Structured checkpoint columns, each capped at _TEXT_MAX on write.
_CHECKPOINT_PAYLOAD_COLUMNS = (
    "decisions",
    "files_changed",
    "tests",
    "pending",
    "blockers",
    "durable_facts",
)
# durable_facts is excluded: the bootstrap entry replaces it with the deduped,
# item-capped merge across checkpoints, which can be *shorter* than the stored
# payload, so its stored length is not a lower bound on the entry.
_BOOTSTRAP_SIZED_COLUMNS = tuple(
    column for column in _CHECKPOINT_PAYLOAD_COLUMNS if column != "durable_facts"
)
# Shared by the size probe and the payload fetch so both see the same rows.
_IMPORTANT_CHECKPOINT_FILTER_SQL = """
    FROM checkpoints
    WHERE session_id = ?
      AND (? IS NULL OR checkpoint_id != ?)
      AND (
          status IN ('failed', 'timed_out', 'cancelled')
          OR (blockers IS NOT NULL AND blockers NOT IN ('', '[]', 'null'))
      )
    ORDER BY timestamp DESC, rowid DESC
    LIMIT ?
"""
_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "password",
    "passwd",
    "secret",
    "token",
    "accesskey",
    "accesstoken",
    "refreshtoken",
}
_SENSITIVE_KEY_SUFFIXES = (
    "accesskey",
    "apikey",
    "password",
    "privatekey",
    "secret",
    "token",
)
_local = threading.local()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_text(text: str) -> str:
    """Best-effort redaction; callers must still avoid submitting secrets."""
    redacted = re.sub(
        r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})",
        "[REDACTED]",
        text,
    )
    redacted = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", redacted)
    redacted = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}",
        "Bearer [REDACTED]",
        redacted,
    )
    redacted = re.sub(r"AKIA[A-Z0-9]{16}", "[REDACTED]", redacted)
    redacted = re.sub(
        r"(?is)-----BEGIN[^-\r\n]*PRIVATE KEY-----.*?"
        r"-----END[^-\r\n]*PRIVATE KEY-----",
        "[REDACTED]",
        redacted,
    )
    return re.sub(
        r"(?i)(password|secret|token|api[_-]?key)[\s:=]+"
        r"['\"]?([A-Za-z0-9_\-.]{16,})['\"]?",
        r"\1: [REDACTED]",
        redacted,
    )


def redact_memory_text(text: str) -> str:
    """Redact a caller-provided memory string before persistence or telemetry."""
    if not isinstance(text, str):
        raise ValueError("memory text must be a string")
    return _redact_text(text)


def _validate_identifier(value: Any, name: str, max_len: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len} characters")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} must not contain surrounding whitespace or control characters")
    return value


def _validate_optional_text(value: Any, name: str, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > max_len:
        raise ValueError(f"{name} must be at most {max_len} characters")
    return _redact_text(value)


def _validate_session_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("session_id must be a 32-character lowercase hex identifier")
    return session_id


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        _SENSITIVE_KEY_SUFFIXES
    )


def _clean_structured(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise ValueError("Structured memory fields may be nested at most 32 levels")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_clean_structured(item, depth + 1) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Structured memory object keys must be strings")
            cleaned_key = _redact_text(key)
            cleaned[cleaned_key] = (
                "[REDACTED]"
                if _is_sensitive_key(key)
                else _clean_structured(item, depth + 1)
            )
        return cleaned
    raise ValueError(
        "Structured memory fields must contain only JSON-compatible values"
    )


def _encode_structured(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, list, dict)):
        raise ValueError(f"{name} must be a string, list, or object")
    encoded = json.dumps(_clean_structured(value), ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > _TEXT_MAX:
        raise ValueError(f"{name} must serialize to at most {_TEXT_MAX} characters")
    return encoded


def _decode_structured(value: str | None) -> Any:
    if value is None or not value.strip():
        return None
    return json.loads(value)


def _size_probe_sql(columns: tuple[str, ...], prefix: str = "") -> str:
    """Project metadata used to lower-bound decoded JSON output sizes.

    ``length()`` and ``instr()`` are core SQLite scalars, so the engine answers
    from the stored text without handing a multi-kilobyte payload to Python.
    """
    return ", ".join(
        f"COALESCE(length({prefix}{column}), 0) AS {column}_len, "
        f"COALESCE(length({prefix}{column}) - "
        f"length(replace({prefix}{column}, '\\', '')), 0) AS {column}_slashes, "
        f"COALESCE((length({prefix}{column}) - "
        f"length(replace({prefix}{column}, '\\u', ''))) / 2, 0) "
        f"AS {column}_unicode_escapes"
        for column in columns
    )


def _min_serialized_len(
    stored_len: int, slash_count: int, unicode_escape_count: int
) -> int:
    """Lower bound on what a stored payload costs once decoded and re-encoded.

    ``_encode_structured`` writes with ``ensure_ascii=False`` and compact
    separators, so re-serializing the decoded value with the default separators
    cannot remove structure or whitespace produced by that encoder. Escaped
    slashes can shrink by one character; a Unicode escape can shrink further,
    including surrogate pairs. Subtracting one per backslash and five more per
    ``\\u`` token deliberately under-estimates every supported case, which keeps
    this gate from rejecting an entry that could fit.
    """
    possible_shrink = (
        slash_count
        + unicode_escape_count * _MAX_UNICODE_ESCAPE_EXTRA_SHRINK
    )
    return max(0, stored_len - possible_shrink)


def _min_row_len(row: sqlite3.Row, columns: tuple[str, ...]) -> int:
    """Sum the per-column lower bounds probed by ``_size_probe_sql``."""
    return sum(
        _min_serialized_len(
            row[f"{column}_len"],
            row[f"{column}_slashes"],
            row[f"{column}_unicode_escapes"],
        )
        for column in columns
    )


def _harden_db_files(db_path: Path) -> None:
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if not path.exists():
            continue
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _prepare_private_db_file(db_path: Path) -> None:
    """Create the main database privately before SQLite can write content."""
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(db_path, flags, 0o600)
    os.close(descriptor)
    try:
        db_path.chmod(0o600)
    except OSError:
        pass


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"Session-memory schema {version} is newer than supported "
                f"version {_SCHEMA_VERSION}"
            )
        if version < 1:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    workspace TEXT,
                    branch TEXT,
                    goal TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT,
                    decisions TEXT,
                    files_changed TEXT,
                    tests TEXT,
                    pending TEXT,
                    blockers TEXT,
                    durable_facts TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_project "
                "ON sessions(project_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_session "
                "ON checkpoints(session_id)"
            )
        if version < 2:
            _init_facts_schema(conn)
        if version < 3:
            _init_tier2_schema(conn)
        if version < _SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _init_facts_schema(conn: sqlite3.Connection) -> None:
    """Create the project-scoped fact store and backfill it (schema v2).

    Durable facts stay exactly where they are written. This lifts a *copy* out
    of session scope so the same lesson learned in twenty sessions becomes one
    row carrying a strength score, instead of twenty unrelated payloads that
    nothing ever merges.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            fact_id TEXT PRIMARY KEY,
            project_key TEXT NOT NULL,
            text TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_recalled TEXT,
            recall_count INTEGER NOT NULL DEFAULT 0,
            source_count INTEGER NOT NULL DEFAULT 0,
            is_generated INTEGER NOT NULL DEFAULT 0,
            superseded_by TEXT REFERENCES facts(fact_id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_sources (
            fact_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
            checkpoint_id TEXT NOT NULL
                REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
            PRIMARY KEY (fact_id, checkpoint_id)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_project_text "
        "ON facts(project_key, text)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_sources_checkpoint "
        "ON fact_sources(checkpoint_id)"
    )
    _backfill_facts(conn)


def _init_tier2_schema(conn: sqlite3.Connection) -> None:
    """Add reversible consolidation and embedding metadata (schema v3)."""
    fact_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(facts)").fetchall()
    }
    if "is_generated" not in fact_columns:
        conn.execute(
            "ALTER TABLE facts ADD COLUMN is_generated INTEGER NOT NULL DEFAULT 0"
        )
    if "superseded_by" not in fact_columns:
        conn.execute(
            "ALTER TABLE facts ADD COLUMN superseded_by TEXT "
            "REFERENCES facts(fact_id) ON DELETE SET NULL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_superseded_by "
        "ON facts(superseded_by)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_embeddings (
            fact_id TEXT PRIMARY KEY
                REFERENCES facts(fact_id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            text_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consolidation_proposals (
            proposal_id TEXT PRIMARY KEY,
            project_key TEXT NOT NULL,
            model TEXT NOT NULL,
            source_fact_ids TEXT NOT NULL,
            proposed_text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            applied_fact_id TEXT,
            applied_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_consolidation_project_status "
        "ON consolidation_proposals(project_key, status)"
    )


def _backfill_facts(conn: sqlite3.Connection) -> None:
    """Populate ``facts`` from durable facts already stored in checkpoints.

    Idempotent: every write is keyed on ``(project_key, text)`` or
    ``(fact_id, checkpoint_id)``, so a second run adds nothing.
    """
    rows = conn.execute(
        """
        SELECT c.checkpoint_id, c.timestamp, c.durable_facts, s.project_key
        FROM checkpoints AS c
        JOIN sessions AS s ON s.session_id = c.session_id
        WHERE c.durable_facts IS NOT NULL
          AND c.durable_facts NOT IN ('', '[]', 'null')
        ORDER BY c.timestamp, c.rowid
        """
    ).fetchall()
    for row in rows:
        _record_facts(
            conn,
            row["project_key"],
            row["checkpoint_id"],
            _decode_structured(row["durable_facts"]),
            observed_at=row["timestamp"],
        )


def _fact_texts(value: Any) -> list[str]:
    """Normalize a durable-facts payload into deduped fact strings.

    Payloads are stored as strings, lists, or objects. Strings are used as
    written; anything else is serialized with sorted keys so two equal objects
    yield one fact rather than two.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    texts: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            text = item.strip()
        else:
            text = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if text and text not in texts:
            texts.append(text)
    return texts


def _record_facts(
    conn: sqlite3.Connection,
    project_key: str,
    checkpoint_id: str,
    durable_facts: Any,
    observed_at: str | None = None,
) -> None:
    """Upsert one checkpoint's durable facts into the project fact store.

    ``source_count`` counts the checkpoints that have asserted a fact and is
    bumped only when a link is genuinely new, so re-recording the same
    checkpoint cannot inflate it. It is deliberately not decremented when a
    checkpoint is later pruned: it records how often the fact was observed,
    not how much provenance is still retained.

    ``observed_at`` stamps ``first_seen`` on insert. The backfill passes the
    originating checkpoint's timestamp so migrated history keeps its real age
    instead of collapsing onto the moment of migration; an existing fact never
    has its ``first_seen`` rewritten.

    The insert is conflict-safe rather than SELECT-then-INSERT. Both call sites
    already hold a ``BEGIN IMMEDIATE`` write transaction, which SQLite
    serializes, so two writers cannot currently interleave here -- but the fact
    store must not silently depend on that invariant holding for every future
    caller, because an IntegrityError raised here would roll back the caller's
    checkpoint, not just the fact promotion.
    """
    first_seen = observed_at or _utc_now()
    for text in _fact_texts(durable_facts):
        conn.execute(
            """
            INSERT INTO facts (fact_id, project_key, text, first_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project_key, text) DO NOTHING
            """,
            (uuid.uuid4().hex, project_key, text, first_seen),
        )
        fact_row = conn.execute(
            "SELECT fact_id, superseded_by FROM facts "
            "WHERE project_key = ? AND text = ?",
            (project_key, text),
        ).fetchone()
        # A repeated observation of a consolidated source must strengthen both
        # its original row and the generated fact currently standing in for it.
        for fact_id in filter(None, (fact_row["fact_id"], fact_row["superseded_by"])):
            cursor = conn.execute(
                "INSERT OR IGNORE INTO fact_sources (fact_id, checkpoint_id) "
                "VALUES (?, ?)",
                (fact_id, checkpoint_id),
            )
            if cursor.rowcount:
                conn.execute(
                    "UPDATE facts SET source_count = source_count + 1 "
                    "WHERE fact_id = ?",
                    (fact_id,),
                )


def _get_db() -> sqlite3.Connection:
    settings.ensure_dirs()
    db_path = settings.memory_db_file
    connection = getattr(_local, "db", None)
    connection_path = getattr(_local, "db_path", None)
    if connection is not None and connection_path != db_path:
        connection.close()
        if getattr(_local, "sqlite_vec_connection", None) == id(connection):
            delattr(_local, "sqlite_vec_connection")
        connection = None
    if connection is None:
        _prepare_private_db_file(db_path)
        connection = sqlite3.connect(
            str(db_path), isolation_level=None, timeout=5.0
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            _init_schema(connection)
        except Exception:
            connection.close()
            raise
        _local.db = connection
        _local.db_path = db_path
        _harden_db_files(db_path)
    return connection


def _close_local_db() -> None:
    connection = getattr(_local, "db", None)
    if connection is not None:
        connection.close()
    for attribute in ("db", "db_path", "sqlite_vec_connection"):
        if hasattr(_local, attribute):
            delattr(_local, attribute)


def session_start(
    project_key: str,
    agent: str,
    workspace: str | None = None,
    branch: str | None = None,
    goal: str | None = None,
) -> str:
    project_key = _validate_identifier(
        project_key, "project_key", _PROJECT_KEY_MAX
    )
    agent = _validate_identifier(agent, "agent", _AGENT_MAX)
    workspace = _validate_optional_text(workspace, "workspace", _WORKSPACE_MAX)
    branch = _validate_optional_text(branch, "branch", _BRANCH_MAX)
    goal = _validate_optional_text(goal, "goal", _TEXT_MAX)
    session_id = uuid.uuid4().hex
    db = _get_db()
    db.execute(
        """
        INSERT INTO sessions (
            session_id, project_key, agent, workspace, branch, goal, status, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            project_key,
            agent,
            workspace,
            branch,
            goal,
            "active",
            _utc_now(),
        ),
    )
    _harden_db_files(settings.memory_db_file)
    return session_id


def memory_checkpoint(
    session_id: str,
    status: str | None = None,
    decisions: Any = None,
    files_changed: Any = None,
    tests: Any = None,
    pending: Any = None,
    blockers: Any = None,
    durable_facts: Any = None,
) -> str:
    session_id = _validate_session_id(session_id)
    status = _validate_optional_text(status, "status", _STATUS_MAX)
    fields = {
        "decisions": _encode_structured(decisions, "decisions"),
        "files_changed": _encode_structured(files_changed, "files_changed"),
        "tests": _encode_structured(tests, "tests"),
        "pending": _encode_structured(pending, "pending"),
        "blockers": _encode_structured(blockers, "blockers"),
        "durable_facts": _encode_structured(durable_facts, "durable_facts"),
    }
    db = _get_db()
    session_row = db.execute(
        "SELECT project_key FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if session_row is None:
        raise ValueError(f"Unknown session {session_id}")
    checkpoint_id = uuid.uuid4().hex
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            """
            INSERT INTO checkpoints (
                checkpoint_id, session_id, timestamp, status, decisions,
                files_changed, tests, pending, blockers, durable_facts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                session_id,
                _utc_now(),
                status,
                fields["decisions"],
                fields["files_changed"],
                fields["tests"],
                fields["pending"],
                fields["blockers"],
                fields["durable_facts"],
            ),
        )
        if status is not None:
            db.execute(
                "UPDATE sessions SET status = ? WHERE session_id = ?",
                (status, session_id),
            )
        # Same transaction as the checkpoint insert: a fact can never be
        # recorded for a checkpoint that was rolled back.
        _record_facts(
            db,
            session_row["project_key"],
            checkpoint_id,
            _decode_structured(fields["durable_facts"]),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    _harden_db_files(settings.memory_db_file)
    return checkpoint_id


def session_end(session_id: str, status: str = "completed") -> None:
    session_id = _validate_session_id(session_id)
    status = _redact_text(_validate_identifier(status, "status", _STATUS_MAX))
    cursor = _get_db().execute(
        "UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
        (status, _utc_now(), session_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Unknown session {session_id}")
    _harden_db_files(settings.memory_db_file)


def memory_bootstrap(project_key: str, budget_chars: int = 20_000) -> dict[str, Any]:
    """Return bounded cross-session context in strict priority order.

    Priority classes, each scanned with its own cap of
    ``_MAX_BOOTSTRAP_SESSIONS_PER_CLASS`` sessions so a flood of routine
    history can never crowd out important sessions:

    1. Sessions with durable facts in *any* checkpoint.
    2. Sessions whose latest checkpoint has unresolved blockers or pending items.
    3. Routine history.

    Within each class the most recently active session wins. Durable facts are
    collected from every retained checkpoint of an included session (not just
    the latest one), and up to ``_MAX_IMPORTANT_CHECKPOINTS`` earlier
    failed/blocked checkpoints are attached as ``earlier_checkpoints``.
    Entries that do not fit the budget are dropped last-first within each
    class order.
    """
    project_key = _validate_identifier(
        project_key, "project_key", _PROJECT_KEY_MAX
    )
    if isinstance(budget_chars, bool) or not isinstance(budget_chars, int):
        raise ValueError("budget_chars must be an integer")
    if budget_chars <= 0 or budget_chars > _MAX_BOOTSTRAP_BUDGET:
        raise ValueError(
            f"budget_chars must be between 1 and {_MAX_BOOTSTRAP_BUDGET}"
        )
    result: dict[str, Any] = {
        "project_key": project_key,
        "project_facts": [],
        "bootstraps": [],
    }
    if len(json.dumps(result, ensure_ascii=False)) > budget_chars:
        raise ValueError("budget_chars is too small for the response envelope")

    db = _get_db()
    result["project_facts"] = _project_facts(
        db, project_key, budget_chars // _BOOTSTRAP_FACTS_BUDGET_DIVISOR
    )
    committed_len = len(json.dumps(result, ensure_ascii=False))
    bootstraps: list[dict[str, Any]] = result["bootstraps"]
    for row in _iter_bootstrap_candidates(db, project_key):
        # Default json.dumps separators join list items with ", " (two chars).
        separator_len = 2 if bootstraps else 0
        max_entry_len = budget_chars - committed_len - separator_len
        # Stored lengths alone can prove an entry is too large. Gating here is
        # what keeps working memory proportional to budget_chars instead of to
        # the database: a payload that cannot fit is never selected, so it is
        # never handed to Python and never decoded.
        if _bootstrap_min_entry_len(row) > max_entry_len:
            continue
        payload = _bootstrap_payload_row(db, row["session_id"], row["checkpoint_id"])
        if payload is None:  # session vanished between the two statements
            continue
        entry = {
            "session_id": row["session_id"],
            "agent": row["agent"],
            "workspace": row["workspace"],
            "branch": row["branch"],
            "goal": payload["goal"],
            "session_status": row["session_status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "checkpoint_time": row["timestamp"],
            "decisions": _decode_structured(payload["decisions"]),
            "files_changed": _decode_structured(payload["files_changed"]),
            "tests": _decode_structured(payload["tests"]),
            "pending": _decode_structured(payload["pending"]),
            "blockers": _decode_structured(payload["blockers"]),
            "durable_facts": _decode_structured(row["base_durable_facts"]),
        }
        entry = {key: value for key, value in entry.items() if value is not None}
        # Enrichment below only ever grows the entry, so an over-budget base
        # entry can be dropped before any enrichment query runs.
        if len(json.dumps(entry, ensure_ascii=False)) > max_entry_len:
            continue

        merged_facts = _merged_durable_facts(db, row["session_id"], max_entry_len)
        if merged_facts is None:
            continue
        if merged_facts:
            entry["durable_facts"] = merged_facts
        earlier = _earlier_important_checkpoints(
            db,
            row["session_id"],
            row["checkpoint_id"],
            max_entry_len - len(json.dumps(entry, ensure_ascii=False)),
        )
        if earlier is None:
            continue
        if earlier:
            entry["earlier_checkpoints"] = earlier
        # The exact serialized length is the authority; every gate above only
        # ever under-estimates it.
        serialized_len = len(json.dumps(entry, ensure_ascii=False))
        if serialized_len > max_entry_len:
            continue
        bootstraps.append(entry)
        committed_len += serialized_len + separator_len
    return result


def _iter_bootstrap_candidates(
    db: sqlite3.Connection, project_key: str
) -> Iterator[sqlite3.Row]:
    """Stream candidate metadata one row at a time, in priority-class order."""
    for extra_where in _BOOTSTRAP_CLASS_FILTERS:
        yield from _bootstrap_class_rows(db, project_key, extra_where)


def _bootstrap_class_rows(
    db: sqlite3.Connection,
    project_key: str,
    extra_where: str,
) -> sqlite3.Cursor:
    """Probe one priority class of bootstrap candidates, capped per class.

    Selects identifiers plus size metadata only. ``goal`` is raw text rather
    than JSON, and escaping a raw string never shortens it, so its stored
    length is a lower bound with no escape probe needed. ``durable_facts`` is
    carried verbatim only for the sentinel values that make
    ``_merged_durable_facts`` come back empty -- every other value is replaced
    by the merge, so loading it here would be wasted work.
    """
    return db.execute(
        f"""
        SELECT s.session_id, s.agent, s.workspace, s.branch,
               s.status AS session_status, s.started_at, s.ended_at,
               c.checkpoint_id, c.timestamp,
               COALESCE(length(s.goal), 0) AS goal_len,
               CASE WHEN c.durable_facts IN ('', '[]', 'null')
                    THEN c.durable_facts END AS base_durable_facts,
               {_size_probe_sql(_BOOTSTRAP_SIZED_COLUMNS, prefix="c.")}
        FROM sessions AS s
        {_latest_checkpoint_join()}
        WHERE s.project_key = ?
          {extra_where}
        ORDER BY max(COALESCE(c.timestamp, ''), COALESCE(s.ended_at, ''),
                     COALESCE(s.started_at, '')) DESC,
                 s.rowid DESC
        LIMIT ?
        """,
        (project_key, _MAX_BOOTSTRAP_SESSIONS_PER_CLASS),
    )


def _bootstrap_min_entry_len(row: sqlite3.Row) -> int:
    """Lower bound on a candidate's serialized entry, from metadata only."""
    return row["goal_len"] + _min_row_len(row, _BOOTSTRAP_SIZED_COLUMNS)


def _bootstrap_payload_row(
    db: sqlite3.Connection,
    session_id: str,
    checkpoint_id: str | None,
) -> sqlite3.Row | None:
    """Load the large fields for one candidate that cleared the size gate."""
    return db.execute(
        """
        SELECT s.goal, c.decisions, c.files_changed, c.tests, c.pending,
               c.blockers
        FROM sessions AS s
        LEFT JOIN checkpoints AS c
          ON c.session_id = s.session_id AND c.checkpoint_id = ?
        WHERE s.session_id = ?
        """,
        (checkpoint_id, session_id),
    ).fetchone()


def _project_facts(
    db: sqlite3.Connection, project_key: str, max_chars: int
) -> list[str]:
    """Return the strongest project facts that fit within ``max_chars``.

    Strength is how often a fact has been recalled plus how many checkpoints
    asserted it, with recency breaking ties. Weak facts sort last and fall off
    the budget; nothing is ever deleted by decay.
    """
    if max_chars <= 0:
        return []
    rows = db.execute(
        """
        SELECT fact_id, text
        FROM facts
        WHERE project_key = ? AND superseded_by IS NULL
        ORDER BY (recall_count + source_count) DESC,
                 COALESCE(last_recalled, first_seen) DESC,
                 rowid DESC
        LIMIT ?
        """,
        (project_key, _MAX_BOOTSTRAP_PROJECT_FACTS),
    ).fetchall()
    selected: list[str] = []
    fact_ids: list[str] = []
    for row in rows:
        candidate = selected + [row["text"]]
        if len(json.dumps(candidate, ensure_ascii=False)) > max_chars:
            break
        selected = candidate
        fact_ids.append(row["fact_id"])
    if fact_ids:
        _bump_fact_recalls(db, fact_ids)
    return selected


def _bump_fact_recalls(db: sqlite3.Connection, fact_ids: list[str]) -> None:
    """Record that these facts were served, best effort.

    Bootstrap is the read path dispatch calls at session start. A lost counter
    update costs only strength accuracy, while a raised error would cost the
    caller its memory, so a write failure here degrades rather than propagates.
    """
    placeholders = ",".join("?" * len(fact_ids))
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            f"""
            UPDATE facts
            SET recall_count = recall_count + 1, last_recalled = ?
            WHERE fact_id IN ({placeholders})
            """,
            (_utc_now(), *fact_ids),
        )
        db.execute("COMMIT")
    except sqlite3.Error:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _validate_fact_id(fact_id: Any) -> str:
    if not isinstance(fact_id, str) or not _FACT_ID.fullmatch(fact_id):
        raise ValueError("fact_id must be a 32-character lowercase hex identifier")
    return fact_id


def _validate_proposal_id(proposal_id: Any) -> str:
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id):
        raise ValueError(
            "proposal_id must be a 32-character lowercase hex identifier"
        )
    return proposal_id


def _validate_model(model: str | None, configured: str, kind: str) -> str:
    selected = configured if model is None else model
    return _validate_identifier(selected, f"{kind}_model", 256)


def _validate_limit(value: Any, name: str, maximum: int, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_similarity(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("min_similarity must be a number")
    normalized = float(value)
    if not -1.0 <= normalized <= 1.0:
        raise ValueError("min_similarity must be between -1 and 1")
    return normalized


def _load_sqlite_vec(db: sqlite3.Connection) -> None:
    if getattr(_local, "sqlite_vec_connection", None) == id(db):
        return
    try:
        import sqlite_vec
    except ImportError as exc:  # pragma: no cover - packaging guarantees this
        raise RuntimeError("sqlite-vec is required for semantic memory recall") from exc
    try:
        db.enable_load_extension(True)
    except AttributeError as exc:
        raise RuntimeError(
            "This Python build does not support SQLite extension loading"
        ) from exc
    try:
        sqlite_vec.load(db)
    finally:
        db.enable_load_extension(False)
    _local.sqlite_vec_connection = id(db)


def _embedding_blob(vector: list[float]) -> bytes:
    if any(not math.isfinite(value) or abs(value) > _FLOAT32_MAX for value in vector):
        raise ValueError("embedding provider returned values outside float32 range")
    try:
        return struct.pack(f"{len(vector)}f", *vector)
    except (OverflowError, struct.error) as exc:
        raise ValueError("embedding provider returned values outside float32 range") from exc


def _embedding_vector(blob: bytes, dimensions: int) -> list[float]:
    return list(struct.unpack(f"{dimensions}f", blob))


def _fact_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embedding_text(text: str) -> str:
    return text[:_MAX_EMBEDDING_TEXT_CHARS]


def _embedding_batches(
    stale: list[tuple[sqlite3.Row, str]],
) -> Iterator[list[tuple[sqlite3.Row, str]]]:
    batch: list[tuple[sqlite3.Row, str]] = []
    batch_chars = 0
    for item in stale:
        text_chars = len(_embedding_text(item[0]["text"]))
        if batch and (
            len(batch) >= _MAX_EMBEDDING_BATCH_SIZE
            or batch_chars + text_chars > _MAX_EMBEDDING_BATCH_CHARS
        ):
            yield batch
            batch = []
            batch_chars = 0
        batch.append(item)
        batch_chars += text_chars
    if batch:
        yield batch


def _validate_embedding_vectors(
    vectors: list[list[float]], expected_count: int
) -> int:
    if len(vectors) != expected_count:
        raise ValueError("embedding provider returned the wrong vector count")
    dimensions = {len(vector) for vector in vectors}
    if (
        not dimensions
        or 0 in dimensions
        or len(dimensions) != 1
        or next(iter(dimensions)) > _MAX_EMBEDDING_DIMENSIONS
    ):
        raise ValueError("embedding provider returned invalid dimensions")
    for vector in vectors:
        _embedding_blob(vector)
    return next(iter(dimensions))


def _ensure_fact_embeddings(
    db: sqlite3.Connection,
    rows: list[sqlite3.Row],
    model: str,
    embedder: Callable[[list[str], str], list[list[float]]],
    expected_dimensions: int,
) -> int:
    """Refresh embeddings in bounded, independently committed batches."""
    stale = []
    for row in rows:
        cached = db.execute(
            "SELECT model, dimensions, text_hash FROM fact_embeddings "
            "WHERE fact_id = ?",
            (row["fact_id"],),
        ).fetchone()
        text_hash = _fact_text_hash(row["text"])
        if (
            cached is None
            or cached["model"] != model
            or cached["dimensions"] != expected_dimensions
            or cached["text_hash"] != text_hash
        ):
            stale.append((row, text_hash))
    if not stale:
        return 0
    indexed = 0
    for batch in _embedding_batches(stale):
        vectors = embedder([_embedding_text(row["text"]) for row, _ in batch], model)
        dimensions = _validate_embedding_vectors(vectors, len(batch))
        if dimensions != expected_dimensions:
            raise ValueError("embedding provider changed dimensions within one operation")
        now = _utc_now()
        db.execute("BEGIN IMMEDIATE")
        try:
            batch_indexed = 0
            for (row, text_hash), vector in zip(batch, vectors, strict=True):
                cursor = db.execute(
                    """
                    INSERT INTO fact_embeddings (
                        fact_id, model, dimensions, embedding, text_hash, updated_at
                    ) SELECT ?, ?, ?, ?, ?, ?
                    WHERE EXISTS (SELECT 1 FROM facts WHERE fact_id = ?)
                    ON CONFLICT(fact_id) DO UPDATE SET
                        model = excluded.model,
                        dimensions = excluded.dimensions,
                        embedding = excluded.embedding,
                        text_hash = excluded.text_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row["fact_id"],
                        model,
                        dimensions,
                        _embedding_blob(vector),
                        text_hash,
                        now,
                        row["fact_id"],
                    ),
                )
                batch_indexed += max(cursor.rowcount, 0)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        indexed += batch_indexed
    return indexed


def _active_fact_rows(
    db: sqlite3.Connection, project_key: str, limit: int
) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT fact_id, text, first_seen, last_recalled, recall_count,
               source_count, is_generated
        FROM facts
        WHERE project_key = ? AND superseded_by IS NULL
        ORDER BY (recall_count + source_count) DESC,
                 COALESCE(last_recalled, first_seen) DESC,
                 rowid DESC
        LIMIT ?
        """,
        (project_key, limit),
    ).fetchall()


def _semantic_rows(
    db: sqlite3.Connection,
    project_key: str,
    model: str,
    query_vector: list[float],
    limit: int,
    min_similarity: float,
    fact_ids: list[str] | None = None,
) -> list[sqlite3.Row]:
    _load_sqlite_vec(db)
    fact_filter = ""
    fact_params: list[Any] = []
    if fact_ids is not None:
        if not fact_ids:
            return []
        fact_filter = f"AND f.fact_id IN ({','.join('?' * len(fact_ids))})"
        fact_params = fact_ids
    query_blob = _embedding_blob(query_vector)
    return db.execute(
        f"""
        SELECT f.fact_id, f.text, f.source_count, f.recall_count,
               f.is_generated,
               1.0 - vec_distance_cosine(e.embedding, ?) AS similarity
        FROM facts AS f
        JOIN fact_embeddings AS e ON e.fact_id = f.fact_id
        WHERE f.project_key = ?
          AND f.superseded_by IS NULL
          AND e.model = ?
          AND e.dimensions = ?
          {fact_filter}
          AND 1.0 - vec_distance_cosine(e.embedding, ?) >= ?
        ORDER BY similarity DESC,
                 (f.recall_count + f.source_count) DESC,
                 f.rowid DESC
        LIMIT ?
        """,
        (
            query_blob,
            project_key,
            model,
            len(query_vector),
            *fact_params,
            query_blob,
            min_similarity,
            limit,
        ),
    ).fetchall()


def memory_recall(
    project_key: str,
    query: str,
    limit: int = 5,
    min_similarity: float = 0.0,
    model: str | None = None,
    *,
    _embedder: Callable[[list[str], str], list[list[float]]] | None = None,
) -> dict[str, Any]:
    """Recall active project facts by semantic similarity.

    The cue is redacted before it reaches the loopback-only provider and is
    never persisted. Only already-redacted fact text is embedded and cached.
    """
    project_key = _validate_identifier(project_key, "project_key", _PROJECT_KEY_MAX)
    if not isinstance(query, str) or len(query) > _MAX_RECALL_QUERY_CHARS:
        raise ValueError(
            f"query must be a string up to {_MAX_RECALL_QUERY_CHARS} characters"
        )
    query = " ".join(query.split())
    if not query:
        raise ValueError("query must contain non-whitespace text")
    limit = _validate_limit(limit, "limit", _MAX_RECALL_LIMIT)
    min_similarity = _validate_similarity(min_similarity)
    model = _validate_model(model, settings.memory_embedding_model, "embedding")
    safe_query = _redact_text(query)
    if _embedder is None:
        from mindsync.memory_models import embed_texts

        _embedder = embed_texts
    db = _get_db()
    rows = _active_fact_rows(db, project_key, _MAX_RECALL_INDEX_FACTS)
    if not rows:
        return {
            "project_key": project_key,
            "model": model,
            "indexed": 0,
            "matches": [],
        }
    query_vectors = _embedder([_embedding_text(safe_query)], model)
    query_dimensions = _validate_embedding_vectors(query_vectors, 1)
    indexed = _ensure_fact_embeddings(
        db, rows, model, _embedder, query_dimensions
    )
    matches = _semantic_rows(
        db, project_key, model, query_vectors[0], limit, min_similarity
    )
    fact_ids = [row["fact_id"] for row in matches]
    if fact_ids:
        _bump_fact_recalls(db, fact_ids)
    recalled_counts: dict[str, int] = {}
    if fact_ids:
        placeholders = ",".join("?" * len(fact_ids))
        recalled_counts = {
            row["fact_id"]: int(row["recall_count"])
            for row in db.execute(
                f"SELECT fact_id, recall_count FROM facts "
                f"WHERE fact_id IN ({placeholders})",
                fact_ids,
            ).fetchall()
        }
    return {
        "project_key": project_key,
        "model": model,
        "indexed": indexed,
        "matches": [
            {
                "fact_id": row["fact_id"],
                "text": row["text"],
                "similarity": round(float(row["similarity"]), 6),
                "source_count": int(row["source_count"]),
                "recall_count": recalled_counts.get(
                    row["fact_id"], int(row["recall_count"])
                ),
                "generated": bool(row["is_generated"]),
            }
            for row in matches
        ],
    }


def memory_consolidate_preview(
    project_key: str,
    limit: int = 5,
    min_similarity: float = 0.45,
    embedding_model: str | None = None,
    consolidation_model: str | None = None,
    *,
    _embedder: Callable[[list[str], str], list[list[float]]] | None = None,
    _consolidator: Callable[[list[dict[str, str]], str], dict[str, Any]]
    | None = None,
) -> dict[str, Any]:
    """Create and persist a reviewable proposal without changing any fact."""
    project_key = _validate_identifier(project_key, "project_key", _PROJECT_KEY_MAX)
    limit = _validate_limit(limit, "limit", _MAX_CONSOLIDATION_FACTS, minimum=2)
    min_similarity = _validate_similarity(min_similarity)
    embedding_model = _validate_model(
        embedding_model, settings.memory_embedding_model, "embedding"
    )
    consolidation_model = _validate_model(
        consolidation_model,
        settings.memory_consolidation_model,
        "consolidation",
    )
    if _embedder is None or _consolidator is None:
        from mindsync.memory_models import consolidate_facts, embed_texts

        _embedder = _embedder or embed_texts
        _consolidator = _consolidator or consolidate_facts
    db = _get_db()
    pending_count = int(
        db.execute(
            "SELECT COUNT(*) FROM consolidation_proposals "
            "WHERE project_key = ? AND status = 'pending'",
            (project_key,),
        ).fetchone()[0]
    )
    if pending_count >= _MAX_PENDING_CONSOLIDATIONS_PER_PROJECT:
        raise ValueError(
            "project has too many pending consolidation proposals; "
            "review existing proposals first"
        )
    candidates = [
        row
        for row in _active_fact_rows(db, project_key, _MAX_CONSOLIDATION_FACTS)
        if not row["is_generated"]
    ]
    if len(candidates) < 2:
        raise ValueError("project needs at least two unconsolidated facts")
    dimension_probe = _embedder(
        [_embedding_text(candidates[0]["text"])], embedding_model
    )
    expected_dimensions = _validate_embedding_vectors(dimension_probe, 1)
    _ensure_fact_embeddings(
        db, candidates, embedding_model, _embedder, expected_dimensions
    )
    candidate_ids = {row["fact_id"] for row in candidates}
    clusters: list[list[sqlite3.Row]] = []
    for candidate in candidates:
        cached = db.execute(
            "SELECT dimensions, embedding FROM fact_embeddings WHERE fact_id = ?",
            (candidate["fact_id"],),
        ).fetchone()
        if cached is None:
            continue
        query_vector = _embedding_vector(cached["embedding"], cached["dimensions"])
        related = _semantic_rows(
            db,
            project_key,
            embedding_model,
            query_vector,
            limit,
            min_similarity,
            list(candidate_ids),
        )
        clusters.append(
            [
                row
                for row in related
                if not row["is_generated"] and row["fact_id"] in candidate_ids
            ]
        )
    if not clusters:
        raise ValueError("candidate facts changed; create a fresh preview")
    source_rows = max(
        clusters,
        key=lambda cluster: (
            len(cluster),
            sum(float(row["similarity"]) for row in cluster),
        ),
    )
    if len(source_rows) < 2:
        raise ValueError("fewer than two related unconsolidated facts met the threshold")
    supplied: list[dict[str, Any]] = []
    supplied_chars = 0
    for row in source_rows:
        text = _embedding_text(row["text"])
        item_chars = len(row["fact_id"]) + len(text)
        if supplied and supplied_chars + item_chars > _MAX_CONSOLIDATION_INPUT_CHARS:
            break
        supplied.append(
            {
                "fact_id": row["fact_id"],
                "text": text,
                "truncated": len(text) < len(row["text"]),
            }
        )
        supplied_chars += item_chars
    if len(supplied) < 2:
        raise ValueError("fewer than two related facts fit the model input limit")
    model_facts = [
        {"fact_id": item["fact_id"], "text": item["text"]} for item in supplied
    ]
    raw_proposal = _consolidator(model_facts, consolidation_model)
    proposed_text = raw_proposal.get("text")
    supporting_ids = raw_proposal.get("supporting_fact_ids")
    if not isinstance(proposed_text, str):
        raise ValueError("consolidation provider returned no fact text")
    proposed_text = _redact_text(proposed_text.strip())
    if not proposed_text or len(proposed_text) > _TEXT_MAX:
        raise ValueError("proposed fact text must be non-empty and within the size limit")
    allowed_ids = {item["fact_id"] for item in supplied}
    if not isinstance(supporting_ids, list) or any(
        not isinstance(item, str) for item in supporting_ids
    ):
        raise ValueError("consolidation provider returned invalid supporting fact IDs")
    source_ids = list(dict.fromkeys(supporting_ids))
    if len(source_ids) < 2 or not set(source_ids) <= allowed_ids:
        raise ValueError("proposal must cite at least two supplied fact IDs")
    source_texts = {
        item["text"] for item in supplied if item["fact_id"] in source_ids
    }
    if proposed_text in source_texts:
        raise ValueError("proposed fact must generalize rather than copy one source")
    proposal_id = uuid.uuid4().hex
    db.execute(
        """
        INSERT INTO consolidation_proposals (
            proposal_id, project_key, model, source_fact_ids,
            proposed_text, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            proposal_id,
            project_key,
            consolidation_model,
            json.dumps(source_ids, separators=(",", ":")),
            proposed_text,
            _utc_now(),
        ),
    )
    return {
        "proposal_id": proposal_id,
        "project_key": project_key,
        "status": "pending",
        "proposed_text": proposed_text,
        "sources": [item for item in supplied if item["fact_id"] in source_ids],
        "note": "Preview only; run apply explicitly to supersede source facts.",
    }


def memory_consolidation_apply(proposal_id: str) -> dict[str, Any]:
    """Apply one pending proposal atomically while retaining all provenance."""
    proposal_id = _validate_proposal_id(proposal_id)
    db = _get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        proposal = db.execute(
            "SELECT * FROM consolidation_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if proposal is None:
            raise ValueError(f"Unknown consolidation proposal {proposal_id}")
        if proposal["status"] != "pending":
            raise ValueError(f"Proposal is already {proposal['status']}")
        source_ids = json.loads(proposal["source_fact_ids"])
        placeholders = ",".join("?" * len(source_ids))
        source_rows = db.execute(
            f"""
            SELECT fact_id FROM facts
            WHERE fact_id IN ({placeholders})
              AND project_key = ? AND superseded_by IS NULL AND is_generated = 0
            """,
            (*source_ids, proposal["project_key"]),
        ).fetchall()
        if {row["fact_id"] for row in source_rows} != set(source_ids):
            raise ValueError("Proposal sources changed; create a fresh preview")
        duplicate = db.execute(
            "SELECT 1 FROM facts WHERE project_key = ? AND text = ?",
            (proposal["project_key"], proposal["proposed_text"]),
        ).fetchone()
        if duplicate is not None:
            raise ValueError("Proposed fact already exists in this project")
        fact_id = uuid.uuid4().hex
        source_count = int(
            db.execute(
                f"SELECT COUNT(DISTINCT checkpoint_id) FROM fact_sources "
                f"WHERE fact_id IN ({placeholders})",
                source_ids,
            ).fetchone()[0]
        )
        db.execute(
            """
            INSERT INTO facts (
                fact_id, project_key, text, first_seen, source_count, is_generated
            ) VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                fact_id,
                proposal["project_key"],
                proposal["proposed_text"],
                _utc_now(),
                source_count,
            ),
        )
        db.execute(
            f"""
            INSERT OR IGNORE INTO fact_sources (fact_id, checkpoint_id)
            SELECT ?, checkpoint_id FROM fact_sources
            WHERE fact_id IN ({placeholders})
            """,
            (fact_id, *source_ids),
        )
        db.execute(
            f"UPDATE facts SET superseded_by = ? WHERE fact_id IN ({placeholders})",
            (fact_id, *source_ids),
        )
        applied_at = _utc_now()
        db.execute(
            """
            UPDATE consolidation_proposals
            SET status = 'applied', applied_fact_id = ?, applied_at = ?
            WHERE proposal_id = ?
            """,
            (fact_id, applied_at, proposal_id),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {
        "proposal_id": proposal_id,
        "status": "applied",
        "fact_id": fact_id,
        "source_fact_ids": source_ids,
    }


def memory_consolidation_undo(fact_id: str) -> dict[str, Any]:
    """Restore superseded source facts and delete their generated replacement."""
    fact_id = _validate_fact_id(fact_id)
    db = _get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        fact = db.execute(
            "SELECT is_generated FROM facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if fact is None:
            raise ValueError(f"Unknown fact {fact_id}")
        if not fact["is_generated"]:
            raise ValueError("Only generated consolidation facts can be undone")
        source_ids = [
            row["fact_id"]
            for row in db.execute(
                "SELECT fact_id FROM facts WHERE superseded_by = ? ORDER BY rowid",
                (fact_id,),
            ).fetchall()
        ]
        db.execute("UPDATE facts SET superseded_by = NULL WHERE superseded_by = ?", (fact_id,))
        db.execute(
            """
            UPDATE consolidation_proposals
            SET status = 'undone'
            WHERE applied_fact_id = ? AND status = 'applied'
            """,
            (fact_id,),
        )
        db.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {
        "fact_id": fact_id,
        "status": "undone",
        "restored_source_fact_ids": source_ids,
    }


def memory_consolidation_list(
    project_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List reviewable consolidation proposals without exposing model traces."""
    if project_key is not None:
        project_key = _validate_identifier(
            project_key, "project_key", _PROJECT_KEY_MAX
        )
    if status is not None and status not in {"pending", "applied", "undone"}:
        raise ValueError("status must be pending, applied, or undone")
    limit = _validate_limit(limit, "limit", _MAX_LIST_LIMIT)
    rows = _get_db().execute(
        """
        SELECT proposal_id, project_key, model, source_fact_ids,
               proposed_text, status, created_at, applied_fact_id, applied_at
        FROM consolidation_proposals
        WHERE (? IS NULL OR project_key = ?)
          AND (? IS NULL OR status = ?)
        ORDER BY created_at DESC, rowid DESC
        LIMIT ?
        """,
        (project_key, project_key, status, status, limit),
    ).fetchall()
    return [
        {
            "proposal_id": row["proposal_id"],
            "project_key": row["project_key"],
            "model": row["model"],
            "source_fact_ids": json.loads(row["source_fact_ids"]),
            "proposed_text": row["proposed_text"],
            "status": row["status"],
            "created_at": row["created_at"],
            "applied_fact_id": row["applied_fact_id"],
            "applied_at": row["applied_at"],
        }
        for row in rows
    ]


def _merged_durable_facts(
    db: sqlite3.Connection, session_id: str, max_chars: int
) -> list[Any] | None:
    """Collect durable facts from every retained checkpoint of a session.

    Facts may have been stored as strings, lists, or objects; every allowed
    payload shape is normalized into a flat list, newest checkpoint first.

    Returns ``None`` as soon as the merged list alone outgrows ``max_chars``:
    the caller's entry can then never fit, so the remaining checkpoints are
    left undecoded. The cap is measured against the merged output rather than
    against stored lengths because dedupe means a checkpoint can contribute
    fewer characters than it stores -- the output, by contrast, only grows.
    """
    rows = db.execute(
        """
        SELECT durable_facts FROM checkpoints
        WHERE session_id = ?
          AND durable_facts IS NOT NULL
          AND durable_facts NOT IN ('', '[]', 'null')
        ORDER BY timestamp DESC, rowid DESC
        LIMIT ?
        """,
        (session_id, _MAX_FACT_CHECKPOINTS_PER_SESSION),
    )
    merged: list[Any] = []
    for row in rows:
        decoded = _decode_structured(row["durable_facts"])
        if decoded is not None:
            items = decoded if isinstance(decoded, list) else [decoded]
            for item in items:
                if item not in merged:
                    merged.append(item)
                    if len(merged) >= _MAX_MERGED_DURABLE_FACTS:
                        break
        if len(json.dumps(merged, ensure_ascii=False)) > max_chars:
            return None
        if len(merged) >= _MAX_MERGED_DURABLE_FACTS:
            break
    return merged


def _earlier_important_checkpoints(
    db: sqlite3.Connection,
    session_id: str,
    latest_checkpoint_id: str | None,
    max_chars: int,
) -> list[dict[str, Any]] | None:
    """Fetch up to _MAX_IMPORTANT_CHECKPOINTS older failed/blocked checkpoints.

    Returns ``None`` when the stored lengths prove these checkpoints cannot fit
    in ``max_chars``, leaving their payloads unselected. Every field of every
    matched checkpoint is appended verbatim -- no dedupe, no cap on content --
    so stored length is a sound lower bound here.
    """
    params = (
        session_id,
        latest_checkpoint_id,
        latest_checkpoint_id,
        _MAX_IMPORTANT_CHECKPOINTS,
    )
    minimum = 0
    matched = False
    for row in db.execute(
        f"SELECT {_size_probe_sql(_CHECKPOINT_PAYLOAD_COLUMNS)}"
        f"{_IMPORTANT_CHECKPOINT_FILTER_SQL}",
        params,
    ):
        matched = True
        minimum += _min_row_len(row, _CHECKPOINT_PAYLOAD_COLUMNS)
        if minimum > max_chars:
            return None
    if not matched:
        return []

    entries = []
    for row in db.execute(
        f"SELECT timestamp, status, {', '.join(_CHECKPOINT_PAYLOAD_COLUMNS)}"
        f"{_IMPORTANT_CHECKPOINT_FILTER_SQL}",
        params,
    ):
        item = {
            "checkpoint_time": row["timestamp"],
            "status": row["status"],
            "decisions": _decode_structured(row["decisions"]),
            "files_changed": _decode_structured(row["files_changed"]),
            "tests": _decode_structured(row["tests"]),
            "pending": _decode_structured(row["pending"]),
            "blockers": _decode_structured(row["blockers"]),
            "durable_facts": _decode_structured(row["durable_facts"]),
        }
        entries.append(
            {key: value for key, value in item.items() if value is not None}
        )
    return entries


def _latest_checkpoint_join() -> str:
    """SQL fragment joining each session to its most recent checkpoint."""
    return """
        LEFT JOIN checkpoints AS c ON c.rowid = (
            SELECT c2.rowid
            FROM checkpoints AS c2
            WHERE c2.session_id = s.session_id
            ORDER BY c2.timestamp DESC, c2.rowid DESC
            LIMIT 1
        )
    """


def memory_stats() -> dict[str, Any]:
    """Return human-facing totals for the session-memory database."""
    db = _get_db()
    totals = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM sessions) AS total_sessions,
            (SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL) AS active_sessions,
            (SELECT COUNT(*) FROM checkpoints) AS total_checkpoints,
            (SELECT COUNT(*) FROM facts) AS total_facts,
            (SELECT COUNT(*) FROM facts WHERE is_generated = 1) AS generated_facts,
            (SELECT COUNT(*) FROM consolidation_proposals
             WHERE status = 'pending') AS pending_consolidations
        """
    ).fetchone()
    projects = db.execute(
        """
        SELECT s.project_key, COUNT(*) AS sessions,
               SUM(CASE WHEN s.ended_at IS NULL THEN 1 ELSE 0 END)
                   AS active_sessions,
               (SELECT COUNT(*) FROM facts AS f
                 WHERE f.project_key = s.project_key) AS facts
        FROM sessions AS s
        GROUP BY s.project_key
        ORDER BY s.project_key
        """
    ).fetchall()
    try:
        db_size_bytes = settings.memory_db_file.stat().st_size
    except OSError:
        db_size_bytes = 0
    return {
        "db_file": str(settings.memory_db_file),
        "db_size_bytes": db_size_bytes,
        "total_sessions": int(totals["total_sessions"]),
        "active_sessions": int(totals["active_sessions"]),
        "total_checkpoints": int(totals["total_checkpoints"]),
        "total_facts": int(totals["total_facts"]),
        "generated_facts": int(totals["generated_facts"]),
        "pending_consolidations": int(totals["pending_consolidations"]),
        "projects": [
            {
                "project_key": row["project_key"],
                "sessions": int(row["sessions"]),
                "active_sessions": int(row["active_sessions"] or 0),
                "facts": int(row["facts"] or 0),
            }
            for row in projects
        ],
    }


def memory_list(
    project_key: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List sessions ordered by greatest non-null activity timestamp (last
    checkpoint, ended_at, started_at), with checkpoint counts."""
    if project_key is not None:
        project_key = _validate_identifier(
            project_key, "project_key", _PROJECT_KEY_MAX
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit <= 0 or limit > _MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
    rows = _get_db().execute(
        """
        SELECT s.session_id, s.project_key, s.agent, s.workspace, s.branch,
               s.goal, s.status AS session_status, s.started_at, s.ended_at,
               (SELECT COUNT(*) FROM checkpoints c
                WHERE c.session_id = s.session_id) AS checkpoint_count,
               (SELECT MAX(c2.timestamp) FROM checkpoints c2
                WHERE c2.session_id = s.session_id) AS last_checkpoint_at
        FROM sessions AS s
        WHERE (? IS NULL OR s.project_key = ?)
        ORDER BY max(COALESCE(last_checkpoint_at, ''), COALESCE(s.ended_at, ''),
                     COALESCE(s.started_at, '')) DESC,
                 s.rowid DESC
        LIMIT ?
        """,
        (project_key, project_key, limit),
    )
    entries = []
    for row in rows:
        entry = {
            "session_id": row["session_id"],
            "project_key": row["project_key"],
            "agent": row["agent"],
            "workspace": row["workspace"],
            "branch": row["branch"],
            "goal": row["goal"],
            "session_status": row["session_status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "checkpoint_count": int(row["checkpoint_count"]),
            "last_checkpoint_at": row["last_checkpoint_at"],
        }
        entries.append(
            {key: value for key, value in entry.items() if value is not None}
        )
    return entries


def memory_show(session_id: str) -> dict[str, Any]:
    """Return one session with every checkpoint, oldest first."""
    session_id = _validate_session_id(session_id)
    db = _get_db()
    row = db.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown session {session_id}")
    session: dict[str, Any] = {
        ("session_status" if key == "status" else key): row[key]
        for key in row.keys()
    }
    checkpoints: list[dict[str, Any]] = []
    for checkpoint in db.execute(
        """
        SELECT * FROM checkpoints WHERE session_id = ?
        ORDER BY timestamp ASC, rowid ASC
        """,
        (session_id,),
    ):
        item = {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "timestamp": checkpoint["timestamp"],
            "status": checkpoint["status"],
            "decisions": _decode_structured(checkpoint["decisions"]),
            "files_changed": _decode_structured(checkpoint["files_changed"]),
            "tests": _decode_structured(checkpoint["tests"]),
            "pending": _decode_structured(checkpoint["pending"]),
            "blockers": _decode_structured(checkpoint["blockers"]),
            "durable_facts": _decode_structured(checkpoint["durable_facts"]),
        }
        checkpoints.append(
            {key: value for key, value in item.items() if value is not None}
        )
    session["checkpoints"] = checkpoints
    return session


def memory_prune(
    project_key: str | None = None,
    older_than_days: int | None = None,
    keep_last: int = 0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Delete old ended sessions; never touch active or durable-fact sessions.

    Project facts survive this: ``fact_sources`` rows cascade away with their
    checkpoints, but the fact itself is project-scoped and stays. ``facts.
    source_count`` is a historical observation counter and is deliberately not
    decremented when provenance is pruned -- decrementing it would silently
    re-rank the fact store, so leave it alone.

    Only sessions that have ended are eligible. A session is protected when
    *any* retained checkpoint carries durable facts (not just the latest one),
    so long-term memory survives pruning. ``keep_last`` preserves the most
    recent N ended sessions per project regardless of age.

    ``dry_run`` must be a real boolean; deletion happens only when it is
    exactly ``False``. Candidate selection and deletion run inside one
    ``BEGIN IMMEDIATE`` transaction so a concurrent durable checkpoint can
    never be deleted after being missed by a stale candidate list.
    """
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if project_key is not None:
        project_key = _validate_identifier(
            project_key, "project_key", _PROJECT_KEY_MAX
        )
    if older_than_days is not None:
        if (
            isinstance(older_than_days, bool)
            or not isinstance(older_than_days, int)
            or older_than_days <= 0
        ):
            raise ValueError("older_than_days must be a positive integer")
    if isinstance(keep_last, bool) or not isinstance(keep_last, int):
        raise ValueError("keep_last must be an integer")
    if keep_last < 0:
        raise ValueError("keep_last must be at least 0")

    cutoff: str | None = None
    if older_than_days is not None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat()

    db = _get_db()
    deleted = 0
    targets: list[str] = []
    protected_durable = 0
    kept_by_keep_last = 0

    if dry_run:
        rows = _prune_candidate_rows(db, project_key).fetchall()
        targets, protected_durable, kept_by_keep_last = _plan_prune(
            rows, keep_last, cutoff
        )
    else:
        db.execute("BEGIN IMMEDIATE")
        try:
            rows = _prune_candidate_rows(db, project_key).fetchall()
            targets, protected_durable, kept_by_keep_last = _plan_prune(
                rows, keep_last, cutoff
            )
            for session_id in targets:
                db.execute(
                    "DELETE FROM checkpoints WHERE session_id = ?",
                    (session_id,),
                )
                cursor = db.execute(
                    "DELETE FROM sessions WHERE session_id = ? "
                    "AND ended_at IS NOT NULL",
                    (session_id,),
                )
                deleted += int(cursor.rowcount)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

    return {
        "dry_run": dry_run,
        "candidates": len(targets),
        "deleted": deleted if not dry_run else None,
        "protected_durable": protected_durable,
        "kept_by_keep_last": kept_by_keep_last,
        "session_ids": targets[:_MAX_PRUNE_SAMPLE],
    }


def _prune_candidate_rows(
    db: sqlite3.Connection,
    project_key: str | None,
) -> sqlite3.Cursor:
    """Select all ended sessions in scope; durable-fact sessions flagged.

    Age filtering is deliberately NOT applied here: ``_plan_prune`` must see
    every ended session so ``keep_last`` can protect the most recent N per
    project before the age cutoff selects deletion candidates.
    """
    return db.execute(
        f"""
        SELECT s.session_id, s.project_key, s.started_at, s.ended_at,
               {_DURABLE_EXISTS_SQL} AS has_durable_facts
        FROM sessions AS s
        WHERE s.ended_at IS NOT NULL
          AND (? IS NULL OR s.project_key = ?)
        ORDER BY s.project_key ASC, s.ended_at DESC, s.started_at DESC,
                 s.rowid DESC
        """,
        (project_key, project_key),
    )


def _plan_prune(
    rows: list[sqlite3.Row],
    keep_last: int,
    cutoff: str | None,
) -> tuple[list[str], int, int]:
    """Turn candidate rows into a delete plan honoring protections.

    Order of operations: keep_last claims recency slots over the full order,
    then durable-fact protection, and only then the age cutoff marks deletion
    candidates — so "most recent N regardless of age" holds even when those N
    are fresher than the cutoff.

    A durable-fact session consumes a recency slot as it passes: it is retained
    either way, so letting an older session claim that slot instead would keep
    more than the caller asked for. It is reported under ``protected_durable``
    rather than ``kept_by_keep_last``, so the two counts never double-count the
    same session.
    """
    kept_per_project: dict[str, int] = {}
    targets: list[str] = []
    protected_durable = 0
    kept_by_keep_last = 0
    for row in rows:
        count = kept_per_project.get(row["project_key"], 0)
        claims_recency_slot = count < keep_last
        if claims_recency_slot:
            kept_per_project[row["project_key"]] = count + 1

        if row["has_durable_facts"]:
            protected_durable += 1
            continue
        if claims_recency_slot:
            kept_by_keep_last += 1
            continue

        activity = row["ended_at"] or row["started_at"]
        if cutoff is not None and activity >= cutoff:
            continue
        targets.append(row["session_id"])
    return targets, protected_durable, kept_by_keep_last
