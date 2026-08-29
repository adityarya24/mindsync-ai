"""Regression tests for Codex standalone usage cache deadlines."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone

import pytest

from mindsync.codex_standalone_usage import (
    _bounded_prefetch_seconds,
    _cache_path,
    maybe_append_reserve_warning,
    prefetch_usage,
)
from mindsync.dispatch.adapters import user_config_path
from mindsync.dispatch.usage.readers.codex import CodexOAuthUsageReader
from mindsync.dispatch.usage.types import UsageReadResult, UsageWindow
from tests.isolation_helpers import isolate_mindsync_home


def _enable_usage(tmp_path, monkeypatch) -> None:
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text(
        json.dumps({"usage": {"enabled": True, "defaultThresholdPercent": 90}}),
        encoding="utf-8",
    )


def _prefetch_duration_seconds(*, timeout_seconds: float, read_seconds: float) -> float:
    bounded = _bounded_prefetch_seconds(timeout_seconds)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-usage-prefetch-test")
    future = pool.submit(time.sleep, read_seconds)
    started = time.perf_counter()
    try:
        future.result(timeout=bounded)
    except FuturesTimeoutError:
        pool.shutdown(wait=False, cancel_futures=True)
        return time.perf_counter() - started
    pool.shutdown(wait=False, cancel_futures=True)
    return time.perf_counter() - started


def test_prefetch_returns_before_slow_reader_finishes(tmp_path, monkeypatch):
    def slow_read(self):
        time.sleep(2.0)
        return UsageReadResult.unavailable(
            provider="codex",
            account_scope="openai:test",
            reason="slow",
            reader="codex-oauth",
        )

    monkeypatch.setattr(CodexOAuthUsageReader, "read", slow_read)
    started = time.perf_counter()
    prefetch_usage(timeout_seconds=0.05)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25


def test_executor_timeout_does_not_wait_for_slow_work():
    elapsed = _prefetch_duration_seconds(timeout_seconds=0.05, read_seconds=2.0)
    assert elapsed < 0.25


def test_prefetch_uses_bounded_reader_timeout(tmp_path, monkeypatch):
    captured: dict[str, float | None] = {}

    class TrackingReader(CodexOAuthUsageReader):
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs.get("request_timeout_seconds")
            super().__init__(**kwargs)

        def read(self):
            return UsageReadResult.unavailable(
                provider="codex",
                account_scope="openai:test",
                reason="fast",
                reader="codex-oauth",
            )

    monkeypatch.setattr(
        "mindsync.codex_standalone_usage.CodexOAuthUsageReader",
        TrackingReader,
    )
    prefetch_usage(timeout_seconds=0.2)

    assert captured["timeout"] == pytest.approx(0.2)


def test_stop_warning_refreshes_stale_cache(tmp_path, monkeypatch):
    _enable_usage(tmp_path, monkeypatch)
    stale = datetime.now(timezone.utc) - timedelta(minutes=20)
    _cache_path().write_text(
        json.dumps(
            {
                "fetched_at": stale.isoformat(),
                "status": "available",
                "provider": "codex",
                "account_scope": "openai:test",
                "reader": "codex-oauth",
                "source": "test",
                "windows": [
                    {
                        "id": "primary",
                        "label": "Primary",
                        "used_percent": 50.0,
                        "reset_at": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fresh_read(self):
        return UsageReadResult.available(
            provider="codex",
            account_scope="openai:test",
            reader="codex-oauth",
            source="test",
            windows=[
                UsageWindow(id="primary", label="Primary", used_percent=95.0),
            ],
        )

    monkeypatch.setattr(CodexOAuthUsageReader, "read", fresh_read)
    warnings: list[str] = []
    maybe_append_reserve_warning(
        "session-1",
        warnings,
        memory_mode="auto",
        timeout_seconds=0.2,
    )

    assert warnings
    assert "Codex seat nearing limit" in warnings[0]
