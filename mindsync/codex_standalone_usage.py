"""Codex bindings for the shared quota handoff core (see mindsync.quota_handoff)."""

from __future__ import annotations

import sys
from pathlib import Path

from mindsync import quota_handoff
from mindsync.dispatch.usage.types import UsageReadResult

_CLI = "codex"

usage_checks_enabled = quota_handoff.usage_checks_enabled


def _cache_path() -> Path:
    return quota_handoff.cache_path(_CLI)


def _warnings_path() -> Path:
    return quota_handoff.asked_path(_CLI)


def write_cache(result: UsageReadResult) -> None:
    quota_handoff.write_cache(result, cli=_CLI)


def read_cache(*, max_age_seconds: float = 600.0) -> UsageReadResult | None:
    return quota_handoff.read_cache(cli=_CLI, max_age_seconds=max_age_seconds)


def prefetch_usage(*, timeout_seconds: float) -> None:
    quota_handoff.prefetch_usage(cli=_CLI, timeout_seconds=timeout_seconds)


def _module_prefetch(*, cli: str, timeout_seconds: float) -> None:
    # Looked up at call time so a patched prefetch_usage on this module applies.
    del cli
    sys.modules[__name__].prefetch_usage(timeout_seconds=timeout_seconds)


def check_reserve_notice(
    session_id: str,
    *,
    memory_mode: str,
    timeout_seconds: float,
    checkpoint_saved: bool = True,
) -> quota_handoff.HandoffNotice | None:
    return quota_handoff.check_handoff(
        _CLI,
        session_id,
        memory_mode=memory_mode,
        timeout_seconds=timeout_seconds,
        checkpoint_saved=checkpoint_saved,
        prefetch=_module_prefetch,
    )


def maybe_append_reserve_warning(
    session_id: str,
    warnings: list[str],
    *,
    memory_mode: str,
    timeout_seconds: float,
    checkpoint_saved: bool = True,
) -> quota_handoff.HandoffNotice | None:
    """Append one stderr warning when a notice fires and return the notice."""
    notice = check_reserve_notice(
        session_id,
        memory_mode=memory_mode,
        timeout_seconds=timeout_seconds,
        checkpoint_saved=checkpoint_saved,
    )
    if notice is not None:
        warnings.append(notice.warning())
    return notice
