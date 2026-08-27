"""Native OpenAI Codex hook adapter for standalone MindSync session memory.

Codex runs this module as a short-lived process per hook event, feeding it a JSON
payload on stdin and reading JSON back on stdout. Three rules shape everything
below:

* **Privacy.** Codex sends far more than the lifecycle needs — a transcript path,
  the last assistant message, tool output. Only the four keys in ``_ALLOWED_KEYS``
  are read; everything else is dropped before any core call, so conversation
  content can never reach the fact store even by accident.
* **Never block the agent.** A hook failure must not fail the user's session. Every
  path exits 0, degrades to a single bounded stderr line, and still writes the
  stdout shape the event requires.
* **Stay inside the budget.** Codex gives hooks 3s (SessionEnd is synchronous), so
  stdin, stdout, and the one subprocess call are all explicitly bounded.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ADAPTER = "codex"

# Payload keys this hook is allowed to look at. Codex also sends transcript_path,
# last_assistant_message, turn_id, stop_hook_active, and reason; those describe
# conversation content or harness bookkeeping, so they are never read, forwarded,
# or persisted. Adding a key here means auditing what it can leak.
_ALLOWED_KEYS = ("session_id", "cwd", "hook_event_name", "source")

_MEMORY_MODE_ENV = "MINDSYNC_STANDALONE_MEMORY_MODE"
# "explicit" is deliberately absent: this adapter never supplies a
# memory_project, so the core would resolve it to no project and no
# warning — memory silently off, with less feedback than a typo gets.
_MEMORY_MODES = ("auto", "off")
_DEFAULT_MEMORY_MODE = "auto"

_MAX_STDIN_BYTES = 1 << 20
_MAX_STDOUT_BYTES = 32_000
# Mirrors additionalContextLimit in .codex/hooks.json — Codex truncates past this
# anyway, and truncating here keeps the warning visible to the user.
_MAX_WARNING_CHARS = 600
_MAX_SESSION_ID_CHARS = 200
_MAX_CWD_CHARS = 4_096
_MAX_EVENT_CHARS = 64
_MAX_SOURCE_CHARS = 32
_MAX_FILES = 50
_MAX_PATH_CHARS = 512

_GIT_TIMEOUT_SECONDS = 2.0
# Leave headroom inside the 3-second Codex hook timeout for JSON I/O and process
# startup/teardown. Every blocking operation shares this one deadline.
_HOOK_WORK_BUDGET_SECONDS = 2.5
# git -C does not override these; leaving them set would describe whatever
# repository invoked Codex instead of the session's own workspace.
_AMBIENT_REPO_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")

_STOP_OUTPUT = '{"continue": true}'
_SOURCE_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TRUNCATION_MARKER = "\n[mindsync: context truncated]"


class _PayloadError(ValueError):
    """A stdin payload this hook refuses to interpret."""


def _lifecycle() -> Any:
    """Import the core lazily so a missing/broken core degrades instead of crashing."""
    from mindsync import standalone_lifecycle

    return standalone_lifecycle


def _result_field(result: Any, name: str) -> Any:
    """Read a field from the start result, whether it is a mapping or an object."""
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def _read_payload(stream: Any = None) -> dict[str, Any]:
    """Read a size-bounded JSON object from stdin."""
    if stream is None:
        stream = getattr(sys.stdin, "buffer", None) or sys.stdin
    try:
        raw = stream.read(_MAX_STDIN_BYTES + 1)
    except Exception as exc:
        raise _PayloadError(f"stdin unreadable: {type(exc).__name__}") from exc
    if raw is None:
        raise _PayloadError("stdin payload was empty")
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    if len(raw) > _MAX_STDIN_BYTES:
        raise _PayloadError(f"stdin payload exceeded {_MAX_STDIN_BYTES} bytes")
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise _PayloadError("stdin payload was empty")
    try:
        payload = json.loads(text)
    except ValueError:
        raise _PayloadError("stdin payload was not valid JSON") from None
    if not isinstance(payload, dict):
        raise _PayloadError("stdin payload was not a JSON object")
    return payload


def _bounded_str(value: Any, limit: int) -> str | None:
    """Coerce an allowlisted scalar to a bounded single-line string."""
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_RE.sub(" ", value).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _allowlisted_fields(payload: Mapping[str, Any]) -> dict[str, str | None]:
    """Project the payload down to the four fields this hook may use.

    Unknown keys are not merely unused — they are never copied anywhere, so the
    rest of this module has no way to reach transcript or message content.
    """
    limits = {
        "session_id": _MAX_SESSION_ID_CHARS,
        "cwd": _MAX_CWD_CHARS,
        "hook_event_name": _MAX_EVENT_CHARS,
        "source": _MAX_SOURCE_CHARS,
    }
    return {key: _bounded_str(payload.get(key), limits[key]) for key in _ALLOWED_KEYS}


def _resolve_memory_mode(warnings: list[str]) -> str:
    """Resolve the rollout mode from the environment, failing closed on garbage."""
    raw = (os.environ.get(_MEMORY_MODE_ENV) or "").strip()
    if not raw:
        return _DEFAULT_MEMORY_MODE
    mode = raw.lower()
    if mode not in _MEMORY_MODES:
        # A typo must not silently enable memory the user meant to switch off.
        warnings.append(
            f"{_MEMORY_MODE_ENV} value is not one of "
            f"{', '.join(_MEMORY_MODES)}; session memory disabled"
        )
        return "off"
    return mode


_ACCEPTED_SOURCES = ("startup", "resume", "clear", "compact")


def _resolve_source(value: str | None, warnings: list[str]) -> str | None:
    """Accept only the source tokens the core accepts.

    Shape alone is not enough. The core raises on anything outside its own set,
    and start_standalone_session re-raises rather than degrading — so a new
    lowercase token from a future Codex would cost that whole session its
    memory. Drop what we do not recognise, the same as a malformed value.
    """
    if value is None:
        return None
    if not _SOURCE_RE.match(value) or value not in _ACCEPTED_SOURCES:
        warnings.append("ignored unrecognized SessionStart source")
        return None
    return value


def _remaining_seconds(deadline: float, cap: float | None = None) -> float:
    remaining = max(0.0, deadline - time.monotonic())
    return remaining if cap is None else min(remaining, cap)


def _git(cwd: str, *args: str, timeout_seconds: float | None = None) -> str | None:
    """Run one git command by argv (never a shell), returning stdout or None."""
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_REPO_ENV}
    timeout = (
        _GIT_TIMEOUT_SECONDS
        if timeout_seconds is None
        else min(_GIT_TIMEOUT_SECONDS, max(0.0, timeout_seconds))
    )
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", cwd, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
            timeout=timeout,
            env=env,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _acceptable_path(candidate: str) -> str | None:
    """Keep only bounded, repo-relative paths — never absolute or escaping ones."""
    path = candidate.strip()
    if not path or len(path) > _MAX_PATH_CHARS:
        return None
    if path.startswith("/") or path.startswith("\\"):
        return None
    try:
        if Path(path).is_absolute() or Path(path).drive:
            return None
    except (OSError, ValueError):
        return None
    parts = path.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        return None
    return path


def _parse_porcelain_z(output: str) -> list[str]:
    """Parse ``git status --porcelain -z`` entries into changed paths.

    ``-z`` is what makes odd filenames safe: git emits raw NUL-terminated paths
    instead of C-quoting anything with a space, quote, or newline in it. Renames
    and copies carry a second NUL-terminated field (the original path) that must
    be consumed, not read as another entry; the new path is the current state, so
    that is what gets recorded.
    """
    fields = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields) and len(paths) < _MAX_FILES:
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # origin path of the rename/copy
        accepted = _acceptable_path(path)
        if accepted is not None and accepted not in paths:
            paths.append(accepted)
    return paths


def _changed_files(
    cwd: str | None, warnings: list[str], *, deadline: float
) -> list[str] | None:
    """Return bounded repo-relative changed paths, or None when git cannot answer."""
    if not cwd:
        return None
    try:
        if not Path(cwd).is_dir():
            return None
    except (OSError, ValueError):
        return None

    inside = _git(
        cwd,
        "rev-parse",
        "--is-inside-work-tree",
        timeout_seconds=_remaining_seconds(deadline, _GIT_TIMEOUT_SECONDS),
    )
    if inside is None or inside.strip() != "true":
        # Not a repo, or no git on PATH. Checkpointing without a file list is
        # still useful, so this is not worth a warning line.
        return None

    # --untracked-files=normal keeps a brand-new build/ directory from costing a
    # full walk on every Stop; git reports it as one "build/" entry.
    status = _git(
        cwd,
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=normal",
        timeout_seconds=_remaining_seconds(deadline, _GIT_TIMEOUT_SECONDS),
    )
    if status is None:
        warnings.append("changed-file detection degraded: git status failed")
        return None
    paths = _parse_porcelain_z(status)
    return paths or None


def _context_cap() -> int:
    """The cap the core budgets its bootstrap against.

    Read from the core rather than duplicated here: two copies of this number
    drift, and the failure that causes is a context blob cut mid-JSON.
    """
    try:
        return int(_lifecycle().MAX_CONTEXT_CHARS)
    except Exception:
        return 8_000


def _bounded_context(context: Any, warnings: list[str]) -> str | None:
    """Bound the core's context blob to what Codex will accept."""
    if not isinstance(context, str) or not context.strip():
        return None
    cap = _context_cap()
    if len(context) <= cap:
        return context
    warnings.append("session memory context truncated to fit the Codex hook limit")
    keep = cap - len(_TRUNCATION_MARKER)
    return context[:keep] + _TRUNCATION_MARKER


def _collect_warnings(source: Any, warnings: list[str]) -> None:
    """Fold core warnings into the local list without trusting their shape."""
    if not isinstance(source, (list, tuple)):
        return
    for item in source[:_MAX_FILES]:
        text = _bounded_str(item, _MAX_WARNING_CHARS)
        if text:
            warnings.append(text)


def _handle_session_start(
    fields: Mapping[str, str | None], warnings: list[str], deadline: float
) -> str:
    """Start the standalone session and hand Codex the memory context."""
    session_id = fields["session_id"]
    if not session_id:
        warnings.append("SessionStart ignored: payload had no session_id")
        return ""

    kwargs: dict[str, Any] = {
        "memory_mode": _resolve_memory_mode(warnings),
        "memory_project": None,
    }
    # Omitted rather than passed as None when Codex sends nothing usable, so the
    # core's own default applies instead of this hook inventing a source.
    source = _resolve_source(fields["source"], warnings)
    if source is not None:
        kwargs["source"] = source

    try:
        result = _lifecycle().start_standalone_session(
            ADAPTER,
            session_id,
            fields["cwd"],
            timeout_seconds=_remaining_seconds(deadline),
            **kwargs,
        )
    except Exception as exc:
        warnings.append(f"session memory start degraded: {type(exc).__name__}: {exc}")
        return ""

    _collect_warnings(_result_field(result, "warnings"), warnings)
    context = _bounded_context(_result_field(result, "context"), warnings)
    if context is None:
        return ""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        },
        ensure_ascii=False,
    )


def _handle_stop(
    fields: Mapping[str, str | None], warnings: list[str], deadline: float
) -> str:
    """Checkpoint mid-session progress. Stop must always answer with JSON."""
    session_id = fields["session_id"]
    if not session_id:
        warnings.append("Stop checkpoint skipped: payload had no session_id")
        return _STOP_OUTPUT
    if _resolve_memory_mode([]) == "off":
        return _STOP_OUTPUT

    try:
        files_changed = _changed_files(fields["cwd"], warnings, deadline=deadline)
        result = _lifecycle().checkpoint_standalone_session(
            ADAPTER,
            session_id,
            status="active",
            files_changed=files_changed,
            timeout_seconds=_remaining_seconds(deadline),
        )
        _collect_warnings(result, warnings)
    except Exception as exc:
        warnings.append(
            f"session memory checkpoint degraded: {type(exc).__name__}: {exc}"
        )
    return _STOP_OUTPUT


def _handle_session_end(
    fields: Mapping[str, str | None], warnings: list[str], deadline: float
) -> str:
    """Close the session. Codex blocks on this event, so it does no I/O of its own."""
    session_id = fields["session_id"]
    if not session_id:
        warnings.append("SessionEnd ignored: payload had no session_id")
        return ""
    if _resolve_memory_mode([]) == "off":
        return ""
    try:
        result = _lifecycle().end_standalone_session(
            ADAPTER,
            session_id,
            status="completed",
            timeout_seconds=_remaining_seconds(deadline),
        )
        _collect_warnings(result, warnings)
    except Exception as exc:
        warnings.append(f"session memory end degraded: {type(exc).__name__}: {exc}")
    return ""


_HANDLERS = {
    "SessionStart": _handle_session_start,
    "Stop": _handle_stop,
    "SessionEnd": _handle_session_end,
}


def _emit_stdout(text: str) -> None:
    if not text:
        return
    if len(text.encode("utf-8", errors="replace")) > _MAX_STDOUT_BYTES:
        # Only the context blob can grow, so drop it rather than emit half a JSON
        # document that Codex would fail to parse.
        return
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _emit_warnings(warnings: list[str]) -> None:
    """Report every degradation as exactly one bounded stderr line."""
    if not warnings:
        return
    joined = "; ".join(_CONTROL_RE.sub(" ", str(item)) for item in warnings)
    if len(joined) > _MAX_WARNING_CHARS:
        joined = joined[: _MAX_WARNING_CHARS - 3] + "..."
    sys.stderr.write(f"mindsync-codex-hook: {joined}\n")
    sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    """Codex hook entry point. Always exits 0 — a hook must not fail a session."""
    del argv
    warnings: list[str] = []
    stdout_text = ""
    event: str | None = None
    deadline = time.monotonic() + _HOOK_WORK_BUDGET_SECONDS
    try:
        fields = _allowlisted_fields(_read_payload())
        event = fields["hook_event_name"]
        handler = _HANDLERS.get(event or "")
        if handler is None:
            warnings.append(f"ignored unsupported hook event: {event or 'missing'}")
        else:
            stdout_text = handler(fields, warnings, deadline)
    except _PayloadError as exc:
        warnings.append(str(exc))
    except Exception as exc:  # last resort: a defect here must not break Codex
        warnings.append(f"hook degraded: {type(exc).__name__}: {exc}")
        if event == "Stop":
            stdout_text = _STOP_OUTPUT

    _emit_stdout(stdout_text)
    _emit_warnings(warnings)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
