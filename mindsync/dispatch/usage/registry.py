"""Runtime-pluggable usage reader registry."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from time import monotonic
from typing import Any

from mindsync.dispatch.adapters import AdapterConfig
from mindsync.dispatch.usage.config import (
    UsageConfig,
    effective_threshold_percent,
    load_usage_config,
    reader_disabled_reason,
    reader_is_enabled,
    validate_reader_name,
)
from mindsync.dispatch.usage.evaluate import evaluate_threshold
from mindsync.dispatch.usage.readers.antigravity import (
    AntigravityOAuthUsageReader,
    READER_NAME as ANTIGRAVITY_OAUTH_READER,
)
from mindsync.dispatch.usage.readers.claude import ClaudeOAuthUsageReader, READER_NAME as CLAUDE_OAUTH_READER
from mindsync.dispatch.usage.readers.codex import CodexOAuthUsageReader, READER_NAME as CODEX_OAUTH_READER
from mindsync.dispatch.usage.readers.cursor import CursorOAuthUsageReader, READER_NAME as CURSOR_OAUTH_READER
from mindsync.dispatch.usage.readers.grok import GrokOAuthUsageReader, READER_NAME as GROK_OAUTH_READER
from mindsync.dispatch.usage.readers.opencode_go import (
    OpenCodeGoUsageReader,
    READER_NAME as OPENCODE_GO_READER,
)
from mindsync.dispatch.usage.types import ThresholdEvaluation, UsageReadResult

ReaderFactory = Callable[..., Any]

_READERS: dict[str, ReaderFactory] = {
    CODEX_OAUTH_READER: lambda **kwargs: CodexOAuthUsageReader(**kwargs),
    CLAUDE_OAUTH_READER: lambda **kwargs: ClaudeOAuthUsageReader(**kwargs),
    GROK_OAUTH_READER: lambda **kwargs: GrokOAuthUsageReader(**kwargs),
    ANTIGRAVITY_OAUTH_READER: lambda **kwargs: AntigravityOAuthUsageReader(**kwargs),
    CURSOR_OAUTH_READER: lambda **kwargs: CursorOAuthUsageReader(**kwargs),
    OPENCODE_GO_READER: lambda **kwargs: OpenCodeGoUsageReader(**kwargs),
}

_UsageCacheKey = tuple[str, str, bool]
_USAGE_CACHE: dict[_UsageCacheKey, tuple[float, UsageReadResult]] = {}
_USAGE_READ_LOCKS: dict[_UsageCacheKey, Lock] = {}
_USAGE_CACHE_LOCK = Lock()


def _clear_usage_cache() -> None:
    """Reset process-local usage snapshots (primarily for isolated tests)."""
    with _USAGE_CACHE_LOCK:
        _USAGE_CACHE.clear()


def _cached_usage_result(
    key: _UsageCacheKey,
    *,
    max_age_seconds: float,
) -> UsageReadResult | None:
    now = monotonic()
    with _USAGE_CACHE_LOCK:
        cached = _USAGE_CACHE.get(key)
        if cached is None:
            return None
        observed_at, result = cached
        if now - observed_at >= max_age_seconds:
            _USAGE_CACHE.pop(key, None)
            return None
        return result.model_copy(deep=True)


def _usage_read_lock(key: _UsageCacheKey) -> Lock:
    with _USAGE_CACHE_LOCK:
        return _USAGE_READ_LOCKS.setdefault(key, Lock())


def _store_usage_result(key: _UsageCacheKey, result: UsageReadResult) -> None:
    with _USAGE_CACHE_LOCK:
        _USAGE_CACHE[key] = (monotonic(), result.model_copy(deep=True))


def register_reader(name: str, factory: ReaderFactory) -> None:
    cleaned = validate_reader_name(name)
    if cleaned is None:
        raise ValueError("reader name must not be empty")
    _READERS[cleaned] = factory
    _clear_usage_cache()


def known_readers() -> list[str]:
    return sorted(_READERS)


def safe_read(reader_name: str, **kwargs: Any) -> UsageReadResult:
    """Read usage and always return a safe result; never raise to callers."""
    provider = str(kwargs.get("provider") or "unknown")
    account_scope = str(kwargs.get("account_scope") or "unknown")
    usage_config = kwargs.pop("usage_config", None)
    try:
        cleaned = validate_reader_name(reader_name)
        if cleaned is None:
            return UsageReadResult.unavailable(
                provider=provider,
                account_scope=account_scope,
                reason="usage reader not configured",
            )
        config = usage_config if isinstance(usage_config, UsageConfig) else load_usage_config()
        if not reader_is_enabled(cleaned, config):
            return UsageReadResult.unavailable(
                provider=provider,
                account_scope=account_scope,
                reason=reader_disabled_reason(cleaned) or "usage reader not enabled",
                reader=cleaned,
            )
        factory = _READERS.get(cleaned)
        if factory is None:
            return UsageReadResult.unavailable(
                provider=provider,
                account_scope=account_scope,
                reason="usage reader unavailable",
                reader=cleaned,
            )
        reader = factory(**kwargs)
        result = reader.read()
        if not isinstance(result, UsageReadResult):
            return UsageReadResult.unavailable(
                provider=provider,
                account_scope=account_scope,
                reason="usage reader returned invalid result",
                reader=cleaned,
            )
        return result
    except ValueError:
        return UsageReadResult.unavailable(
            provider=provider,
            account_scope=account_scope,
            reason="usage reader configuration invalid",
            reader=reader_name,
        )
    except Exception:
        return UsageReadResult.unavailable(
            provider=provider,
            account_scope=account_scope,
            reason="usage reader failed",
            reader=reader_name,
        )


def read_usage_for_adapter(
    adapter: AdapterConfig,
    *,
    usage_config: UsageConfig | None = None,
) -> UsageReadResult:
    """Resolve an adapter's configured reader without affecting reactive dispatch."""
    if not adapter.usageReader:
        return UsageReadResult.unavailable(
            provider=adapter.name,
            account_scope=adapter.quotaScope or f"agent:{adapter.name}",
            reason="usage reader not configured",
        )
    scope = adapter.quotaScope or f"agent:{adapter.name}"
    config = usage_config or load_usage_config()
    reader_name = adapter.usageReader.strip().lower()
    key = (reader_name, scope, reader_is_enabled(reader_name, config))
    max_age_seconds = float(config.pollingIntervalSeconds)
    cached = _cached_usage_result(key, max_age_seconds=max_age_seconds)
    if cached is not None:
        return cached

    # A route preview, preflight, and job start may converge on the same account.
    # Serialize only that reader/scope so one provider request refreshes the snapshot.
    with _usage_read_lock(key):
        cached = _cached_usage_result(key, max_age_seconds=max_age_seconds)
        if cached is not None:
            return cached
        result = safe_read(
            reader_name,
            account_scope=scope,
            usage_config=config,
        )
        _store_usage_result(key, result)
        return result


def evaluate_adapter_threshold(
    adapter: AdapterConfig,
    *,
    usage_config: UsageConfig | None = None,
    result: UsageReadResult | None = None,
) -> ThresholdEvaluation:
    config = usage_config or load_usage_config()
    observation = result or read_usage_for_adapter(adapter, usage_config=config)
    threshold = effective_threshold_percent(
        usage_config=config,
        adapter_threshold=adapter.usageThresholdPercent,
    )
    return evaluate_threshold(observation, threshold_percent=threshold)
