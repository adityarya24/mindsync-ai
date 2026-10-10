"""Claude Code Stop hook: ask the user about a handoff when the seat runs low.

Claude Code runs this per Stop event with a JSON payload on stdin. It reads only
``session_id`` and ``hook_event_name``; the transcript path and everything else
are dropped unread. When the shared core (``mindsync.quota_handoff``) says this
seat crossed the orchestrator reserve for the first time in the current window,
the hook answers ``{"decision": "block", "reason": ...}`` so Claude keeps going
long enough to tell the user and ask whether to hand off. Otherwise it prints
nothing and Claude stops normally. Every path exits 0.
"""

from __future__ import annotations

import os
import sys
import time

from mindsync.codex_hook import (
    _MEMORY_MODE_ENV,
    _PayloadError,
    _bounded_str,
    _emit_stdout,
    _read_payload,
)

ADAPTER = "claude"
# Claude Code's own hook timeout is set to 5s by the installer; stay well inside.
_HOOK_WORK_BUDGET_SECONDS = 2.5
_MAX_WARNING_CHARS = 600


def _memory_mode() -> str:
    raw = (os.environ.get(_MEMORY_MODE_ENV) or "").strip().lower()
    return raw if raw in ("auto", "off") else ("auto" if not raw else "off")


def _handle_stop(session_id: str, deadline: float, warnings: list[str]) -> str:
    from mindsync.quota_handoff import check_handoff, stop_block_output

    notice = check_handoff(
        ADAPTER,
        session_id,
        memory_mode=_memory_mode(),
        timeout_seconds=max(0.0, deadline - time.monotonic()),
        checkpoint_saved=False,
    )
    if notice is None:
        return ""
    warnings.append(notice.warning())
    return stop_block_output(notice)


def main(argv: list[str] | None = None) -> int:
    del argv
    warnings: list[str] = []
    stdout_text = ""
    deadline = time.monotonic() + _HOOK_WORK_BUDGET_SECONDS
    try:
        payload = _read_payload()
        event = _bounded_str(payload.get("hook_event_name"), 64)
        session_id = _bounded_str(payload.get("session_id"), 200)
        if event != "Stop":
            warnings.append(f"ignored unsupported hook event: {event or 'missing'}")
        elif not session_id:
            warnings.append("Stop ignored: payload had no session_id")
        else:
            stdout_text = _handle_stop(session_id, deadline, warnings)
    except _PayloadError as exc:
        warnings.append(str(exc))
    except Exception as exc:  # a defect here must not break the session
        warnings.append(f"hook degraded: {type(exc).__name__}: {exc}")
        stdout_text = ""

    _emit_stdout(stdout_text)
    if warnings:
        joined = "; ".join(warnings)[:_MAX_WARNING_CHARS]
        sys.stderr.write(f"mindsync-claude-hook: {joined}\n")
        sys.stderr.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
