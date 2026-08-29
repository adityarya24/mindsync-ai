"""Per-bundle MCP action validation. Not a shared dispatcher."""

from __future__ import annotations

from typing import Any

JOB_ACTIONS = frozenset({"status", "wait", "result", "review", "cancel"})
JOB_FIELDS = {
    "status": frozenset({"job_id"}),
    "wait": frozenset({"job_id", "timeout_seconds", "poll_interval_seconds"}),
    "result": frozenset({"job_id"}),
    "review": frozenset({"job_id"}),
    "cancel": frozenset({"job_id"}),
}
JOB_REQUIRED = {action: frozenset({"job_id"}) for action in JOB_ACTIONS}

EVENT_ACTIONS = frozenset({"publish", "poll", "subscribe"})
EVENT_FIELDS = {
    "publish": frozenset({"event_type", "payload", "correlation_id"}),
    "poll": frozenset({"since_seq", "limit"}),
    "subscribe": frozenset({"event_types"}),
}
EVENT_REQUIRED = {
    "publish": frozenset({"event_type", "payload"}),
    "poll": frozenset(),
    "subscribe": frozenset(),
}

SESSION_ACTIONS = frozenset({"start", "checkpoint", "end"})
SESSION_FIELDS = {
    "start": frozenset({"project_key", "workspace", "branch", "goal"}),
    "checkpoint": frozenset(
        {
            "session_id",
            "status",
            "decisions",
            "files_changed",
            "tests",
            "pending",
            "blockers",
            "durable_facts",
        }
    ),
    "end": frozenset({"session_id", "status"}),
}
SESSION_REQUIRED = {
    "start": frozenset({"project_key"}),
    "checkpoint": frozenset({"session_id"}),
    "end": frozenset({"session_id"}),
}

CONSOLIDATION_ACTIONS = frozenset({"preview", "apply", "undo", "list"})
CONSOLIDATION_FIELDS = {
    "preview": frozenset(
        {
            "project_key",
            "limit",
            "min_similarity",
            "embedding_model",
            "consolidation_model",
        }
    ),
    "apply": frozenset({"proposal_id"}),
    "undo": frozenset({"fact_id"}),
    "list": frozenset({"project_key", "status", "limit"}),
}
CONSOLIDATION_REQUIRED = {
    "preview": frozenset({"project_key"}),
    "apply": frozenset({"proposal_id"}),
    "undo": frozenset({"fact_id"}),
    "list": frozenset(),
}

LIST_KINDS = frozenset({"agents", "models", "roles"})
LIST_FIELDS = {
    "agents": frozenset(),
    "models": frozenset({"agent"}),
    "roles": frozenset(),
}


def extra_fields(
    action: str,
    fields_by_action: dict[str, frozenset[str]],
    provided: dict[str, Any],
) -> list[str]:
    allowed = fields_by_action[action]
    return sorted(
        name
        for name, value in provided.items()
        if value is not None and name not in allowed
    )


def missing_fields(
    action: str,
    required_by_action: dict[str, frozenset[str]],
    provided: dict[str, Any],
) -> list[str]:
    required = required_by_action[action]
    return sorted(name for name in required if provided.get(name) in (None, ""))


def action_error(tool: str, action: str, allowed: frozenset[str]) -> str:
    names = ", ".join(sorted(allowed))
    return f"Unknown {tool} action {action!r}. Use one of: {names}."


def extras_error(tool: str, action: str, extras: list[str]) -> str:
    joined = ", ".join(extras)
    return f"{joined} not valid for {tool} action {action!r}."


def missing_error(tool: str, action: str, missing: list[str]) -> str:
    joined = ", ".join(missing)
    return f"{tool} action {action!r} requires {joined}."
