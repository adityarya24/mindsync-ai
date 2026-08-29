"""Regression tests for Codex standalone usage cache deadlines."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone

import pytest

from mindsync.codex_standalone_usage import (
    _cache_path,
    maybe_append_reserve_warning,
    prefetch_usage,
    write_cache,
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


def _prefetch_subprocess_elapsed(
    *,
    home: str,
    slow_seconds: float,
    timeout_seconds: float,
) -> float:
    script = textwrap.dedent(
        f"""
        import os
        import sys
        import time

        os.environ["MINDSYNC_HOME"] = {home!r}
        from mindsync.codex_standalone_usage import prefetch_usage
        from mindsync.dispatch.usage.readers.codex import CodexOAuthUsageReader
        from mindsync.dispatch.usage.types import UsageReadResult

        def slow_read(self):
            time.sleep({slow_seconds})
            return UsageReadResult.unavailable(
                provider="codex",
                account_scope="openai:test",
                reason="slow",
                reader="codex-oauth",
            )

        CodexOAuthUsageReader.read = slow_read
        prefetch_usage(timeout_seconds={timeout_seconds})
        """
    )
    env = os.environ.copy()
    env["MINDSYNC_HOME"] = home
    start = time.perf_counter()
    subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        check=True,
        timeout=10,
    )
    return time.perf_counter() - start


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


def test_prefetch_subprocess_exits_without_waiting_for_slow_reader(
    tmp_path, monkeypatch
):
    home = str(isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=False))
    baseline = _prefetch_subprocess_elapsed(
        home=home,
        slow_seconds=0.0,
        timeout_seconds=0.05,
    )
    slow_exit = _prefetch_subprocess_elapsed(
        home=home,
        slow_seconds=2.0,
        timeout_seconds=0.05,
    )

    assert baseline < 0.6
    assert slow_exit < max(0.6, baseline + 0.25)


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

    def refresh_cache(**kwargs):
        write_cache(
            UsageReadResult.available(
                provider="codex",
                account_scope="openai:test",
                reader="codex-oauth",
                source="test",
                windows=[
                    UsageWindow(id="primary", label="Primary", used_percent=95.0),
                ],
            )
        )

    monkeypatch.setattr(
        "mindsync.codex_standalone_usage.prefetch_usage",
        refresh_cache,
    )
    warnings: list[str] = []
    maybe_append_reserve_warning(
        f"session-{tmp_path.name}",
        warnings,
        memory_mode="auto",
        timeout_seconds=0.2,
    )

    assert warnings
    assert "Codex seat nearing limit" in warnings[0]


def test_stop_warning_uses_orchestrator_reserve_not_worker_threshold(
    tmp_path, monkeypatch
):
    _enable_usage(tmp_path, monkeypatch)
    user_config_path().write_text(
        json.dumps(
            {
                "usage": {
                    "enabled": True,
                    "defaultThresholdPercent": 90,
                    "orchestratorReservePercent": 80,
                }
            }
        ),
        encoding="utf-8",
    )
    write_cache(
        UsageReadResult.available(
            provider="codex",
            account_scope="openai:test",
            reader="codex-oauth",
            source="test",
            windows=[UsageWindow(id="primary", label="Primary", used_percent=85.0)],
        )
    )
    monkeypatch.setattr(
        "mindsync.codex_standalone_usage.prefetch_usage",
        lambda **kwargs: None,
    )
    warnings: list[str] = []
    maybe_append_reserve_warning(
        f"reserve-{tmp_path.name}",
        warnings,
        memory_mode="auto",
        timeout_seconds=0.2,
    )

    assert warnings
    assert "85" in warnings[0]


def test_stop_warning_below_orchestrator_reserve(tmp_path, monkeypatch):
    _enable_usage(tmp_path, monkeypatch)
    user_config_path().write_text(
        json.dumps(
            {
                "usage": {
                    "enabled": True,
                    "defaultThresholdPercent": 90,
                    "orchestratorReservePercent": 80,
                }
            }
        ),
        encoding="utf-8",
    )
    write_cache(
        UsageReadResult.available(
            provider="codex",
            account_scope="openai:test",
            reader="codex-oauth",
            source="test",
            windows=[UsageWindow(id="primary", label="Primary", used_percent=70.0)],
        )
    )
    monkeypatch.setattr(
        "mindsync.codex_standalone_usage.prefetch_usage",
        lambda **kwargs: None,
    )
    warnings: list[str] = []
    maybe_append_reserve_warning(
        f"below-{tmp_path.name}",
        warnings,
        memory_mode="auto",
        timeout_seconds=0.2,
    )

    assert warnings == []
