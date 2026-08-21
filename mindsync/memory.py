"""Local, structured session memory backed by SQLite."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mindsync.config import settings

_SCHEMA_VERSION = 1
_PROJECT_KEY_MAX = 256
_AGENT_MAX = 128
_WORKSPACE_MAX = 4096
_BRANCH_MAX = 1024
_TEXT_MAX = 100_000
_STATUS_MAX = 128
_MAX_BOOTSTRAP_BUDGET = 200_000
_MAX_BOOTSTRAP_SESSIONS_PER_CLASS = 200
_SIZE_PRECHECK_SLACK = 4096
_MAX_FACT_CHECKPOINTS_PER_SESSION = 10
_MAX_MERGED_DURABLE_FACTS = 20
_MAX_IMPORTANT_CHECKPOINTS = 3
_MAX_LIST_LIMIT = 500
_MAX_PRUNE_SAMPLE = 100
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_DURABLE_EXISTS_SQL = """
    EXISTS (
        SELECT 1 FROM checkpoints AS df
        WHERE df.session_id = s.session_id
          AND df.durable_facts IS NOT NULL
          AND df.durable_facts NOT IN ('', '[]', 'null')
    )
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
    if value is None:
        return None
    return json.loads(value)


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
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _get_db() -> sqlite3.Connection:
    settings.ensure_dirs()
    db_path = settings.memory_db_file
    connection = getattr(_local, "db", None)
    connection_path = getattr(_local, "db_path", None)
    if connection is not None and connection_path != db_path:
        connection.close()
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
    for attribute in ("db", "db_path"):
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
    if db.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone() is None:
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
    result: dict[str, Any] = {"project_key": project_key, "bootstraps": []}
    if len(json.dumps(result, ensure_ascii=False)) > budget_chars:
        raise ValueError("budget_chars is too small for the response envelope")

    db = _get_db()
    rows = (
        _bootstrap_class_rows(
            db,
            project_key,
            f"AND {_DURABLE_EXISTS_SQL}",
        ).fetchall()
        + _bootstrap_class_rows(
            db,
            project_key,
            f"""
            AND NOT {_DURABLE_EXISTS_SQL}
            AND (
                COALESCE(c.blockers, '') NOT IN ('', '[]', 'null')
                OR COALESCE(c.pending, '') NOT IN ('', '[]', 'null')
            )
            """,
        ).fetchall()
        + _bootstrap_class_rows(
            db,
            project_key,
            f"""
            AND NOT {_DURABLE_EXISTS_SQL}
            AND COALESCE(c.blockers, '') IN ('', '[]', 'null')
            AND COALESCE(c.pending, '') IN ('', '[]', 'null')
            """,
        ).fetchall()
    )

    committed_len = len(json.dumps(result, ensure_ascii=False))
    bootstraps: list[dict[str, Any]] = result["bootstraps"]
    for row in rows:
        # Gross-oversize gate before decoding: structured payloads can
        # approach _TEXT_MAX each, so decoding every candidate would let
        # working memory scale with database size instead of budget_chars.
        # The slack keeps borderline small entries eligible; the exact check
        # after enrichment is what actually enforces the budget.
        remaining = budget_chars - committed_len
        if _estimate_entry_size(row) > remaining + _SIZE_PRECHECK_SLACK:
            continue
        entry = {
            "session_id": row["session_id"],
            "agent": row["agent"],
            "workspace": row["workspace"],
            "branch": row["branch"],
            "goal": row["goal"],
            "session_status": row["session_status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "checkpoint_time": row["timestamp"],
            "decisions": _decode_structured(row["decisions"]),
            "files_changed": _decode_structured(row["files_changed"]),
            "tests": _decode_structured(row["tests"]),
            "pending": _decode_structured(row["pending"]),
            "blockers": _decode_structured(row["blockers"]),
            "durable_facts": _decode_structured(row["durable_facts"]),
        }
        entry = {key: value for key, value in entry.items() if value is not None}
        merged_facts = _merged_durable_facts(db, row["session_id"])
        if merged_facts:
            entry["durable_facts"] = merged_facts
        earlier = _earlier_important_checkpoints(
            db,
            row["session_id"],
            row["checkpoint_id"],
        )
        if earlier:
            entry["earlier_checkpoints"] = earlier
        serialized_len = len(json.dumps(entry, ensure_ascii=False))
        # Default json.dumps separators join list items with ", " (two chars).
        delta = serialized_len + (2 if bootstraps else 0)
        if committed_len + delta > budget_chars:
            continue
        bootstraps.append(entry)
        committed_len += delta
    return {"project_key": project_key, "bootstraps": bootstraps}


def _estimate_entry_size(row: sqlite3.Row) -> int:
    """Conservative upper bound for an entry's serialized size, no decoding.

    Structured columns are counted as their stored JSON length with 2x slack
    for escaping; a flat pad covers keys, identifiers, and enrichment
    headroom. False oversizing only skips an entry that might have fit — the
    exact check after enrichment keeps the response within budget.
    """
    raw = 0
    for column in (
        "decisions",
        "files_changed",
        "tests",
        "pending",
        "blockers",
        "durable_facts",
    ):
        value = row[column]
        raw += len(value) if value else 0
    identifiers = (
        len(row["session_id"])
        + len(row["agent"])
        + len(row["session_status"] or "")
        + len(row["timestamp"] or "")
    )
    return identifiers + raw * 2 + 512


def _bootstrap_class_rows(
    db: sqlite3.Connection,
    project_key: str,
    extra_where: str,
) -> sqlite3.Cursor:
    """Fetch one priority class of bootstrap candidates, capped per class."""
    return db.execute(
        f"""
        SELECT s.session_id, s.agent, s.workspace, s.branch, s.goal,
               s.status AS session_status, s.started_at, s.ended_at,
               c.checkpoint_id, c.timestamp, c.decisions, c.files_changed,
               c.tests, c.pending, c.blockers, c.durable_facts
        FROM sessions AS s
        {_latest_checkpoint_join()}
        WHERE s.project_key = ?
          {extra_where}
        ORDER BY COALESCE(c.timestamp, s.ended_at, s.started_at) DESC,
                 s.rowid DESC
        LIMIT ?
        """,
        (project_key, _MAX_BOOTSTRAP_SESSIONS_PER_CLASS),
    )


def _merged_durable_facts(db: sqlite3.Connection, session_id: str) -> list[Any]:
    """Collect durable facts from every retained checkpoint of a session.

    Facts may have been stored as strings, lists, or objects; every allowed
    payload shape is normalized into a flat list, newest checkpoint first.
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
    ).fetchall()
    merged: list[Any] = []
    for row in rows:
        decoded = _decode_structured(row["durable_facts"])
        if decoded is None:
            continue
        items = decoded if isinstance(decoded, list) else [decoded]
        for item in items:
            if item not in merged:
                merged.append(item)
                if len(merged) >= _MAX_MERGED_DURABLE_FACTS:
                    return merged
    return merged


def _earlier_important_checkpoints(
    db: sqlite3.Connection,
    session_id: str,
    latest_checkpoint_id: str | None,
) -> list[dict[str, Any]]:
    """Fetch up to _MAX_IMPORTANT_CHECKPOINTS older failed/blocked checkpoints."""
    rows = db.execute(
        """
        SELECT timestamp, status, decisions, files_changed, tests,
               pending, blockers, durable_facts
        FROM checkpoints
        WHERE session_id = ?
          AND (? IS NULL OR checkpoint_id != ?)
          AND (
              status IN ('failed', 'timed_out', 'cancelled')
              OR (blockers IS NOT NULL AND blockers NOT IN ('', '[]', 'null'))
          )
        ORDER BY timestamp DESC, rowid DESC
        LIMIT ?
        """,
        (
            session_id,
            latest_checkpoint_id,
            latest_checkpoint_id,
            _MAX_IMPORTANT_CHECKPOINTS,
        ),
    )
    entries = []
    for row in rows:
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
            (SELECT COUNT(*) FROM checkpoints) AS total_checkpoints
        """
    ).fetchone()
    projects = db.execute(
        """
        SELECT project_key, COUNT(*) AS sessions,
               SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END) AS active_sessions
        FROM sessions
        GROUP BY project_key
        ORDER BY project_key
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
        "projects": [
            {
                "project_key": row["project_key"],
                "sessions": int(row["sessions"]),
                "active_sessions": int(row["active_sessions"] or 0),
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

    Order of operations: durable-fact protection first, then keep_last over
    the full recency order, and only then the age cutoff marks deletion
    candidates — so "most recent N regardless of age" holds even when those N
    are fresher than the cutoff.
    """
    kept_per_project: dict[str, int] = {}
    targets: list[str] = []
    protected_durable = 0
    kept_by_keep_last = 0
    for row in rows:
        if row["has_durable_facts"]:
            protected_durable += 1
            continue
        count = kept_per_project.get(row["project_key"], 0)
        if count < keep_last:
            kept_per_project[row["project_key"]] = count + 1
            kept_by_keep_last = sum(kept_per_project.values())
            continue
        activity = row["ended_at"] or row["started_at"]
        if cutoff is not None and activity >= cutoff:
            continue
        targets.append(row["session_id"])
    return targets, protected_durable, kept_by_keep_last
