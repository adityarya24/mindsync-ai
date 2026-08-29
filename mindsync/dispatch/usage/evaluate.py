"""Threshold evaluation across provider usage windows."""

from __future__ import annotations

from datetime import datetime

from mindsync.dispatch.usage.types import ThresholdEvaluation, UsageReadResult, UsageWindow


def evaluate_threshold(
    result: UsageReadResult,
    *,
    threshold_percent: float,
) -> ThresholdEvaluation:
    """Return a safe threshold decision; unavailable readers never raise."""
    base = {
        "threshold_percent": threshold_percent,
        "provider": result.provider,
        "account_scope": result.account_scope,
        "windows": list(result.windows),
    }
    if result.status != "available":
        return ThresholdEvaluation(
            status="unavailable",
            reason=result.reason or "usage unavailable",
            earliest_reset_at=_earliest_reset(result.windows),
            **base,
        )

    at_threshold = [window for window in result.windows if window.used_percent >= threshold_percent]
    earliest_reset = _earliest_reset(result.windows)
    if not at_threshold:
        return ThresholdEvaluation(
            status="below_threshold",
            earliest_reset_at=earliest_reset,
            **base,
        )

    triggering = max(at_threshold, key=lambda window: window.used_percent)
    earliest_trigger_reset = _earliest_reset(at_threshold) or earliest_reset
    return ThresholdEvaluation(
        status="at_threshold",
        triggering_window=triggering,
        earliest_reset_at=earliest_trigger_reset,
        **base,
    )


def _earliest_reset(windows: list[UsageWindow]) -> datetime | None:
    resets = [window.reset_at for window in windows if window.reset_at is not None]
    if not resets:
        return None
    return min(resets)
