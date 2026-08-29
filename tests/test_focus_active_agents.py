"""Tests for focus staleness in health reporting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mindsync.storage as storage
from mindsync.conflict import list_active_agents
from mindsync.server import health
from tests.isolation_helpers import isolate_mindsync_home


def test_list_active_agents_omits_stale_and_future_entries(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=False)
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(seconds=30)).isoformat()
    stale = (now - timedelta(hours=3)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()
    agents_focus = {
        "fresh-agent": {"timestamp": fresh, "focus": "working"},
        "stale-agent": {"timestamp": stale, "focus": "old work"},
        "future-agent": {"timestamp": future, "focus": "clock skew"},
        "broken-agent": {"timestamp": "not-a-date", "focus": "bad"},
    }

    assert list_active_agents(agents_focus, stale_seconds=7200, now=now) == [
        "fresh-agent"
    ]


def test_health_active_agents_respects_focus_stale_seconds(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=False)
    now = datetime.now(timezone.utc)
    with storage.locked_state() as state:
        state["agents_focus"] = {
            "live": {
                "timestamp": (now - timedelta(minutes=5)).isoformat(),
                "focus": "current task",
            },
            "ghost": {
                "timestamp": (now - timedelta(hours=5)).isoformat(),
                "focus": " stale task",
            },
        }
        storage.save_state(state)

    report = health()

    assert report["active_agents"] == ["live"]
