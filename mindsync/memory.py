"""Local, structured session memory backed by SQLite."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
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
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
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

    rows = _get_db().execute(
        """
        SELECT s.session_id, s.agent, s.workspace, s.branch, s.goal,
               s.status AS session_status, s.started_at, s.ended_at,
               c.timestamp, c.decisions, c.files_changed, c.tests,
               c.pending, c.blockers, c.durable_facts
        FROM sessions AS s
        LEFT JOIN checkpoints AS c ON c.rowid = (
            SELECT c2.rowid
            FROM checkpoints AS c2
            WHERE c2.session_id = s.session_id
            ORDER BY c2.timestamp DESC, c2.rowid DESC
            LIMIT 1
        )
        WHERE s.project_key = ?
        ORDER BY
            CASE WHEN s.ended_at IS NULL THEN 0 ELSE 1 END,
            COALESCE(c.timestamp, s.started_at) DESC,
            s.started_at DESC
        """,
        (project_key,),
    )
    for row in rows:
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
        candidate = {
            "project_key": project_key,
            "bootstraps": [*result["bootstraps"], entry],
        }
        if len(json.dumps(candidate, ensure_ascii=False)) <= budget_chars:
            result = candidate
    return result
