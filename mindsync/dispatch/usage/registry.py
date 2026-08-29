"""Runtime-pluggable usage reader registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mindsync.dispatch.adapters import AdapterConfig
from mindsync.dispatch.usage.config import (
    UsageConfig,
    effective_threshold_percent,
    load_usage_config,
    validate_reader_name,
)
from mindsync.dispatch.usage.evaluate import evaluate_threshold
from mindsync.dispatch.usage.readers.codex import CodexOAuthUsageReader, READER_NAME as CODEX_OAUTH_READER
from mindsync.dispatch.usage.types import ThresholdEvaluation, UsageReadResult

ReaderFactory = Callable[..., Any]

_READERS: dict[str, ReaderFactory] = {
    CODEX_OAUTH_READER: lambda **kwargs: CodexOAuthUsageReader(**kwargs),
}


def register_reader(name: str, factory: ReaderFactory) -> None:
    cleaned = validate_reader_name(name)
    if cleaned is None:
        raise ValueError("reader name must not be empty")
    _READERS[cleaned] = factory


def known_readers() -> list[str]:
    return sorted(_READERS)


def safe_read(reader_name: str, **kwargs: Any) -> UsageReadResult:
    """Read usage and always return a safe result; never raise to callers."""
    provider = str(kwargs.get("provider") or "unknown")
    account_scope = str(kwargs.get("account_scope") or "unknown")
    try:
        cleaned = validate_reader_name(reader_name)
        if cleaned is None:
            return UsageReadResult.unavailable(
                provider=provider,
                account_scope=account_scope,
                reason="usage reader not configured",
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
    return safe_read(
        adapter.usageReader,
        account_scope=scope,
    )


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
