"""Checkpoint-gated pre-emptive usage helpers for dispatch handoff."""

from __future__ import annotations

from typing import Any

from mindsync.dispatch.adapters import AdapterConfig
from mindsync.dispatch.limits import cooldown_reason
from mindsync.dispatch.usage.config import UsageConfig, load_usage_config
from mindsync.dispatch.usage.registry import evaluate_adapter_threshold
from mindsync.dispatch.usage.types import ThresholdEvaluation, UsageReadResult

_CHECKPOINT_KEYS = (
    "status",
    "decisions",
    "files_changed",
    "tests",
    "pending",
    "blockers",
    "durable_facts",
)


def preemptive_usage_active(
    *,
    usage_config: UsageConfig | None = None,
    on_limit: str | None,
) -> bool:
    config = usage_config or load_usage_config()
    return bool(config.enabled and on_limit == "handoff")


def has_usable_checkpoint(meta: dict[str, Any]) -> tuple[bool, str | None]:
    """Return whether a privacy-safe checkpoint can gate a pre-emptive transfer."""
    session_id = meta.get("memorySessionId")
    if not session_id:
        return False, "no memory session checkpoint available"
    try:
        from mindsync.memory import memory_show

        shown = memory_show(str(session_id))
        rows = shown.get("checkpoints") or []
        if not rows or not isinstance(rows[-1], dict):
            return False, "no checkpoint recorded for this attempt"
        allowed = {
            key: rows[-1][key] for key in _CHECKPOINT_KEYS if key in rows[-1]
        }
        if not allowed:
            return False, "checkpoint has no actionable fields"
        if all(not value for value in allowed.values()):
            return False, "checkpoint is empty"
        return True, None
    except Exception:
        return False, "checkpoint unavailable"


def preflight_skip_reason(
    adapter: AdapterConfig,
    *,
    usage_config: UsageConfig | None = None,
    evaluation: ThresholdEvaluation | None = None,
    result: UsageReadResult | None = None,
) -> str | None:
    """Return a visible skip reason when an agent must not start yet."""
    cooling = cooldown_reason(adapter)
    if cooling:
        return cooling
    if evaluation is None:
        evaluation = evaluate_adapter_threshold(
            adapter,
            usage_config=usage_config,
            result=result,
        )
    if evaluation.status == "at_threshold":
        return _threshold_skip_reason(evaluation)
    return None


def _threshold_skip_reason(evaluation: ThresholdEvaluation) -> str:
    window = evaluation.triggering_window
    if window is not None:
        return (
            f"usage at threshold ({window.used_percent:.1f}% on {window.label})"
        )
    return "usage at threshold"


def public_usage_status(evaluation: ThresholdEvaluation) -> dict[str, Any]:
    """Serialize only privacy-safe usage fields for job metadata."""
    return evaluation.to_public_dict()
