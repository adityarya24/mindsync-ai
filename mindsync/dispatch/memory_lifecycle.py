"""Automatic dispatch session-memory lifecycle (Phase 2).

Shared runner integration only — no per-adapter hooks. When ``memory_project`` is
omitted, this module is never invoked and dispatch behavior is unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from mindsync.dispatch import store
from mindsync.memory import (
    memory_bootstrap,
    memory_checkpoint,
    session_end,
    session_start,
)
from mindsync.storage import file_lock

_DISPATCH_GOAL = "dispatch-automatic-lifecycle"
_BOOTSTRAP_BUDGET_CHARS = 8_000
_CONTEXT_START = "--- MindSync session memory"
_CONTEXT_END = "--- end MindSync session memory ---"
_MAX_FILES = 50
_MAX_CHECK_ENTRIES = 20


def _validate_project_key(project_key: str) -> str:
    """Validate a memory project key using Phase 1 rules."""
    from mindsync.memory import _validate_identifier, _PROJECT_KEY_MAX

    return _validate_identifier(project_key, "project_key", _PROJECT_KEY_MAX)


def _format_context_prefix(bootstrap: dict[str, Any]) -> str:
    # Security invariant: keep untrusted memory inside one compact JSON line so
    # embedded newlines stay escaped and cannot manufacture delimiter lines.
    # Do not pretty-print this payload without replacing that framing guarantee.
    compact = json.dumps(bootstrap, ensure_ascii=False, separators=(",", ":"))
    return f"{_CONTEXT_START} ---\n{compact}\n{_CONTEXT_END}\n\n"


def _bounded_files(diff: dict[str, Any] | None) -> list[str] | None:
    if not diff:
        return None
    files = diff.get("files") or []
    if not isinstance(files, list):
        return None
    bounded = [str(item) for item in files[:_MAX_FILES]]
    return bounded or None


def _bounded_check_summaries(check_results: list[Any] | None) -> list[dict[str, Any]] | None:
    if not check_results:
        return None
    summaries: list[dict[str, Any]] = []
    for entry in check_results[:_MAX_CHECK_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        summary: dict[str, Any] = {"name": str(entry.get("name") or "check")}
        if "passed" in entry:
            summary["passed"] = bool(entry["passed"])
        if entry.get("timedOut"):
            summary["timedOut"] = True
        summaries.append(summary)
    return summaries or None


def _memory_end_status(job_status: str | None, timed_out: bool | None) -> str:
    if job_status == "done":
        return "completed"
    if job_status == "cancelled":
        return "cancelled"
    if timed_out:
        return "timed_out"
    return "failed"


def _checkpoint_status(job_status: str | None, timed_out: bool | None) -> str:
    if job_status == "done":
        return "done"
    if job_status == "cancelled":
        return "cancelled"
    if timed_out:
        return "timed_out"
    return "failed"


def prepare_dispatch_memory(
    job_id: str,
    memory_project: str,
    *,
    agent: str,
    workspace: str | None,
    branch: str | None,
) -> tuple[str | None, list[str]]:
    """Bootstrap, start a session, and return a prompt prefix plus any warnings."""
    warnings: list[str] = []
    try:
        project_key = _validate_project_key(memory_project)
    except ValueError as exc:
        warnings.append(f"session memory degraded: {exc}")
        return None, warnings

    bootstrap: dict[str, Any] | None = None
    try:
        bootstrap = memory_bootstrap(project_key, budget_chars=_BOOTSTRAP_BUDGET_CHARS)
    except Exception as exc:
        warnings.append(f"session memory bootstrap degraded: {exc}")

    session_id: str | None = None
    try:
        session_id = session_start(
            project_key=project_key,
            agent=agent,
            workspace=workspace,
            branch=branch,
            goal=_DISPATCH_GOAL,
        )
    except Exception as exc:
        warnings.append(f"session memory start degraded: {exc}")
        return None, warnings

    try:
        store.update_job(
            job_id,
            {
                "memoryProject": project_key,
                "memorySessionId": session_id,
                "memoryFinalized": False,
                "memoryFinalizeState": "active",
            },
        )
    except Exception as exc:
        warnings.append(f"session memory metadata degraded: {exc}")

    prefix: str | None = None
    if bootstrap is not None:
        try:
            prefix = _format_context_prefix(bootstrap)
        except Exception as exc:
            warnings.append(f"session memory context formatting degraded: {exc}")

    return prefix, warnings


def finalize_dispatch_memory(job_id: str) -> list[str]:
    """Finalize dispatch memory exactly once; safe to call from cancel/supervise races."""
    store.job_paths(job_id)  # validate before deriving a lock name from caller input
    warnings: list[str] = []
    lock_name = f"dispatch-job-{job_id}"
    with file_lock(lock_name):
        meta = store.get_job(job_id)
        if meta is None or not meta.get("memorySessionId"):
            return warnings
        if meta.get("memoryFinalized"):
            return []

        session_id = str(meta["memorySessionId"])
        job_status = meta.get("status")
        timed_out = bool(meta.get("timedOut"))
        checkpoint_status = _checkpoint_status(
            str(job_status) if job_status is not None else None,
            timed_out,
        )
        end_status = _memory_end_status(
            str(job_status) if job_status is not None else None,
            timed_out,
        )

        finalize_state = "finalized"
        try:
            memory_checkpoint(
                session_id,
                status=checkpoint_status,
                files_changed=_bounded_files(meta.get("diff")),
                tests=_bounded_check_summaries(meta.get("checkResults")),
                durable_facts=[f"dispatch-job:{job_id}:{checkpoint_status}"],
            )
            session_end(session_id, status=end_status)
        except Exception as exc:
            finalize_state = "degraded"
            warnings.append(f"session memory finalization degraded: {exc}")

        merged_warnings = list(meta.get("warnings") or [])
        for item in warnings:
            if item not in merged_warnings:
                merged_warnings.append(item)

        updated = {
            **meta,
            "memoryFinalized": True,
            "memoryFinalizeState": finalize_state,
            "warnings": merged_warnings,
        }
        store._write_meta(job_id, updated)  # noqa: SLF001 — same lock domain as update_job
    return warnings


def append_warnings(job_id: str, new_warnings: list[str]) -> None:
    """Merge memory warnings into job metadata without failing the job."""
    if not new_warnings:
        return
    meta = store.get_job(job_id)
    if meta is None:
        return
    existing = list(meta.get("warnings") or [])
    merged = list(dict.fromkeys([*existing, *new_warnings]))
    if merged != existing:
        store.update_job(job_id, {"warnings": merged})
