"""Bounded Codex usage cache and reserve warnings for standalone hooks."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mindsync.config import settings
from mindsync.dispatch.usage.config import load_usage_config
from mindsync.dispatch.usage.evaluate import evaluate_threshold
from mindsync.dispatch.usage.readers.codex import CodexOAuthUsageReader
from mindsync.dispatch.usage.types import ThresholdEvaluation, UsageReadResult, UsageWindow

_CACHE_NAME = "codex-standalone-usage-cache.json"
_WARNINGS_NAME = "codex-standalone-usage-warnings.json"
_MAX_CACHE_AGE_SECONDS = 600.0
_MAX_PREFETCH_SECONDS = 0.8


def _cache_path() -> Path:
    return settings.home / _CACHE_NAME


def _warnings_path() -> Path:
    return settings.home / _WARNINGS_NAME


def usage_checks_enabled(*, memory_mode: str) -> bool:
    if memory_mode == "off":
        return False
    try:
        return bool(load_usage_config().enabled)
    except ValueError:
        return False


def _serialize_window(window: UsageWindow) -> dict[str, Any]:
    payload = window.model_dump()
    reset_at = payload.get("reset_at")
    if isinstance(reset_at, datetime):
        payload["reset_at"] = reset_at.astimezone(timezone.utc).isoformat()
    return payload


def _deserialize_window(raw: dict[str, Any]) -> UsageWindow | None:
    try:
        reset_at = raw.get("reset_at")
        if isinstance(reset_at, str) and reset_at.strip():
            raw = dict(raw)
            raw["reset_at"] = datetime.fromisoformat(reset_at)
        return UsageWindow.model_validate(raw)
    except (TypeError, ValueError):
        return None


def _serialize_result(result: UsageReadResult) -> dict[str, Any]:
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": result.status,
        "provider": result.provider,
        "account_scope": result.account_scope,
        "reason": result.reason,
        "reader": result.reader,
        "source": result.source,
        "windows": [_serialize_window(window) for window in result.windows],
    }


def _deserialize_result(raw: dict[str, Any]) -> UsageReadResult | None:
    if raw.get("status") != "available":
        return None
    windows: list[UsageWindow] = []
    for item in raw.get("windows") or []:
        if isinstance(item, dict):
            window = _deserialize_window(item)
            if window is not None:
                windows.append(window)
    if not windows:
        return None
    return UsageReadResult.available(
        provider=str(raw.get("provider") or "codex"),
        account_scope=str(raw.get("account_scope") or "openai:default"),
        reader=str(raw.get("reader") or "codex-oauth"),
        source=str(raw.get("source") or "codex-oauth-wham-usage"),
        windows=windows,
    )


def write_cache(result: UsageReadResult) -> None:
    settings.ensure_dirs()
    atomic_path = _cache_path()
    atomic_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_path.write_text(
        json.dumps(_serialize_result(result), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_cache(*, max_age_seconds: float = _MAX_CACHE_AGE_SECONDS) -> UsageReadResult | None:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    fetched_at = raw.get("fetched_at")
    if not isinstance(fetched_at, str):
        return None
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age > max_age_seconds:
        return None
    return _deserialize_result(raw)


def _bounded_prefetch_seconds(timeout_seconds: float) -> float:
    if timeout_seconds <= 0:
        return 0.0
    return max(0.05, min(timeout_seconds, _MAX_PREFETCH_SECONDS))


def prefetch_usage(*, timeout_seconds: float) -> None:
    """Best-effort usage refresh with a strict deadline; never raises."""
    bounded = _bounded_prefetch_seconds(timeout_seconds)
    if bounded <= 0:
        return
    reader = CodexOAuthUsageReader(request_timeout_seconds=bounded)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-usage-prefetch")
    future = pool.submit(reader.read)
    try:
        result = future.result(timeout=bounded)
    except FuturesTimeoutError:
        pool.shutdown(wait=False, cancel_futures=True)
        return
    except Exception:
        pool.shutdown(wait=False, cancel_futures=True)
        return
    pool.shutdown(wait=False, cancel_futures=True)
    if isinstance(result, UsageReadResult) and result.status == "available":
        write_cache(result)


def _warning_fingerprint(evaluation: ThresholdEvaluation) -> str:
    window = evaluation.triggering_window
    if window is None:
        return "threshold"
    reset = window.reset_at.isoformat() if window.reset_at else "unknown"
    return f"{window.id}:{window.used_percent:.1f}:{reset}"


def _load_warning_state() -> dict[str, str]:
    path = _warnings_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_warning_state(state: dict[str, str]) -> None:
    settings.ensure_dirs()
    path = _warnings_path()
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _format_warning(evaluation: ThresholdEvaluation) -> str:
    window = evaluation.triggering_window
    if window is None:
        return "Codex seat nearing configured usage threshold"
    reset_bit = ""
    if window.reset_at is not None:
        reset_bit = f"; resets {window.reset_at.astimezone(timezone.utc).isoformat()}"
    label = window.label or window.id
    return (
        f"Codex seat nearing limit: {label} "
        f"{window.used_percent:.0f}% full{reset_bit}"
    )


def maybe_append_reserve_warning(
    session_id: str,
    warnings: list[str],
    *,
    memory_mode: str,
    timeout_seconds: float,
) -> None:
    """Refresh usage when budget allows, then append one deduplicated warning."""
    if not usage_checks_enabled(memory_mode=memory_mode):
        return
    prefetch_usage(timeout_seconds=timeout_seconds)
    cached = read_cache()
    if cached is None:
        return
    try:
        config = load_usage_config()
    except ValueError:
        return
    evaluation = evaluate_threshold(
        cached,
        threshold_percent=config.defaultThresholdPercent,
    )
    if evaluation.status != "at_threshold":
        return
    fingerprint = _warning_fingerprint(evaluation)
    state = _load_warning_state()
    if state.get(session_id) == fingerprint:
        return
    warnings.append(_format_warning(evaluation))
    state[session_id] = fingerprint
    _save_warning_state(state)
