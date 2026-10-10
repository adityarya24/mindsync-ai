"""Shared quota handoff core plus the Claude Code Stop hook adapter."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from mindsync import claude_hook, quota_handoff
from mindsync.dispatch.adapters import AdapterConfig, user_config_path
from mindsync.dispatch.usage.types import UsageReadResult, UsageWindow
from mindsync.onboarding import _write_claude_hooks, claude_settings_path
from tests.isolation_helpers import isolate_mindsync_home


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    user_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_config_path().write_text(
        json.dumps({"usage": {"enabled": True, "defaultThresholdPercent": 90}}),
        encoding="utf-8",
    )
    # Never touch a real provider from these tests.
    monkeypatch.setattr(quota_handoff, "prefetch_usage", lambda **kwargs: None)


def _cache(cli: str, used: float, reset_at: datetime | None = None) -> None:
    quota_handoff.write_cache(
        UsageReadResult.available(
            provider=cli,
            account_scope=f"{cli}:test",
            reader=f"{cli}-oauth",
            source="test",
            windows=[
                UsageWindow(id="primary", label="5h", used_percent=used, reset_at=reset_at)
            ],
        ),
        cli=cli,
    )


def _check(cli: str = "codex", session: str = "s1"):
    return quota_handoff.check_handoff(
        cli,
        session,
        memory_mode="auto",
        timeout_seconds=0.2,
        checkpoint_saved=True,
        prefetch=lambda **kwargs: None,
    )


def _fake_fleet(monkeypatch, names=("codex", "claude", "grok")):
    adapters = {name: AdapterConfig(name=name, bin=name, capabilities=["general"]) for name in names}
    monkeypatch.setattr("mindsync.dispatch.routing.load_adapters", lambda: adapters)
    monkeypatch.setattr("mindsync.dispatch.routing.resolve_bin", lambda value: value)


def test_asks_once_per_window_even_as_usage_climbs(monkeypatch):
    _fake_fleet(monkeypatch)
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    _cache("codex", 91.0, reset)
    assert _check() is not None

    _cache("codex", 94.0, reset)
    assert _check() is None

    # Providers jitter the reported reset by seconds between reads.
    _cache("codex", 95.0, reset - timedelta(seconds=40))
    assert _check() is None

    # A new window (new reset) is a new question.
    _cache("codex", 92.0, reset + timedelta(hours=5))
    assert _check() is not None


def test_below_reserve_never_asks(monkeypatch):
    _fake_fleet(monkeypatch)
    _cache("codex", 50.0)
    assert _check() is None


def test_usage_disabled_never_asks(monkeypatch):
    _fake_fleet(monkeypatch)
    user_config_path().write_text(json.dumps({"usage": {"enabled": False}}), encoding="utf-8")
    _cache("codex", 99.0)
    assert _check() is None


def test_successor_sort_reads_cache_never_live(monkeypatch):
    _fake_fleet(monkeypatch)

    def live_read(*args, **kwargs):
        raise AssertionError("successor sort must not read usage live")

    monkeypatch.setattr("mindsync.dispatch.usage.registry.evaluate_adapter_threshold", live_read)
    monkeypatch.setattr("mindsync.dispatch.usage.registry.read_usage_for_adapter", live_read)
    _cache("codex", 95.0)
    _cache("claude", 30.0)

    notice = _check()

    assert notice is not None
    assert notice.successor is not None
    # claude has 70% headroom in its cache; grok is unknown, so ranked neutral.
    assert notice.successor.agent == "claude"
    assert notice.successor.used_percent == pytest.approx(30.0)
    assert "claude (70% free)" in notice.prompt()


def test_failed_state_save_does_not_ask(monkeypatch):
    _fake_fleet(monkeypatch)
    _cache("codex", 95.0)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(quota_handoff, "_save_asked", boom)
    assert _check() is None


def test_no_successor_says_so(monkeypatch):
    _fake_fleet(monkeypatch, names=("codex",))
    _cache("codex", 95.0)
    notice = _check()
    assert notice is not None
    assert notice.successor is None
    assert "No other installed agent" in notice.prompt()
    assert "delegate_task" not in notice.prompt()


def test_prompt_requires_user_yes(monkeypatch):
    _fake_fleet(monkeypatch)
    _cache("codex", 95.0)
    prompt = _check().prompt()
    assert "not from the user" in prompt
    assert "Never hand off without the user's yes" in prompt
    assert 'exclude_agents=["codex"]' in prompt
    block = json.loads(quota_handoff.stop_block_output(_notice_for_output()))
    assert block["decision"] == "block"


def _notice_for_output():
    return quota_handoff.HandoffNotice(
        cli="codex",
        display="Codex",
        window_label="5h",
        used_percent=95.0,
        reset_at=None,
        successor=quota_handoff.Successor(agent="grok", used_percent=None),
        checkpoint_saved=False,
    )


# --------------------------------------------------------------------------
# Claude Code adapter
# --------------------------------------------------------------------------


def _run_claude(monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    code = claude_hook.main()
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err


def test_claude_stop_asks_then_lets_claude_stop(monkeypatch, capsys):
    _fake_fleet(monkeypatch)
    _cache("claude", 93.0, datetime.now(timezone.utc) + timedelta(hours=1))
    payload = {
        "session_id": "c1",
        "hook_event_name": "Stop",
        "transcript_path": "/secret/transcript.jsonl",
        "stop_hook_active": False,
    }

    code, out, err = _run_claude(monkeypatch, capsys, payload)

    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "Claude Code seat is at 93%" in decision["reason"]
    assert 'exclude_agents=["claude"]' in decision["reason"]
    assert "/secret/" not in out + err
    assert "Claude Code seat nearing limit" in err

    code2, out2, _ = _run_claude(monkeypatch, capsys, {**payload, "stop_hook_active": True})
    assert code2 == 0
    assert out2 == ""


def test_claude_hook_ignores_other_events_and_bad_input(monkeypatch, capsys):
    code, out, err = _run_claude(monkeypatch, capsys, {"session_id": "c1", "hook_event_name": "PreToolUse"})
    assert (code, out) == (0, "")
    assert "unsupported hook event" in err

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert claude_hook.main() == 0
    assert capsys.readouterr().out == ""


def test_claude_hook_respects_memory_mode_off(monkeypatch, capsys):
    _fake_fleet(monkeypatch)
    monkeypatch.setenv("MINDSYNC_STANDALONE_MEMORY_MODE", "off")
    _cache("claude", 99.0)
    code, out, _ = _run_claude(monkeypatch, capsys, {"session_id": "c1", "hook_event_name": "Stop"})
    assert (code, out) == (0, "")


# --------------------------------------------------------------------------
# Claude Code installer
# --------------------------------------------------------------------------


def test_claude_installer_merges_and_is_idempotent(tmp_path):
    home = tmp_path / "user"
    path = claude_settings_path(home)
    path.parent.mkdir(parents=True)
    existing = {
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-hook"}]}]},
    }
    path.write_text(json.dumps(existing), encoding="utf-8")

    first = _write_claude_hooks(user_home=home, force=False, dry_run=False)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert first["action"] == "configured"
    assert first["backup"]
    assert data["model"] == "opus"
    commands = [hook["command"] for block in data["hooks"]["Stop"] for hook in block["hooks"]]
    assert commands == ["other-hook", "mindsync-claude-hook"]

    again = _write_claude_hooks(user_home=home, force=True, dry_run=False)
    assert again["action"] == "already_configured"
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_claude_installer_dry_run_and_bad_json(tmp_path):
    home = tmp_path / "user"
    assert _write_claude_hooks(user_home=home, force=False, dry_run=True)["action"] == "would_configure"
    assert not claude_settings_path(home).exists()

    claude_settings_path(home).parent.mkdir(parents=True)
    claude_settings_path(home).write_text("{broken", encoding="utf-8")
    assert _write_claude_hooks(user_home=home, force=False, dry_run=False)["action"] == "error"
