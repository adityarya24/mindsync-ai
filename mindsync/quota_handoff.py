"""Quota-aware handoff prompts for human-facing CLI Stop hooks.

A human-facing CLI (Codex, Claude Code, ...) cannot be swapped out from under the
user, so MindSync never auto-starts a successor here. What it can do is make the
running agent *say something*: when the CLI's own seat crosses the orchestrator
reserve, the Stop hook returns a block decision whose reason tells the agent to
report the usage to the user and ask whether to hand the rest of the task off.

Everything in this module is local and bounded so it fits a 3 second hook
timeout: one capped refresh of this CLI's usage, then a successor sort that reads
only cached usage files and never calls a provider.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mindsync.dispatch.usage.config import effective_reserve_percent, load_usage_config
from mindsync.dispatch.usage.evaluate import evaluate_threshold
from mindsync.dispatch.usage.readers.claude import ClaudeOAuthUsageReader
from mindsync.dispatch.usage.readers.codex import CodexOAuthUsageReader
from mindsync.dispatch.usage.types import ThresholdEvaluation, UsageReadResult, UsageWindow

_MAX_CACHE_AGE_SECONDS = 600.0
_MAX_PREFETCH_SECONDS = 0.8
_MAX_ASKED_SESSIONS = 200


@dataclass(frozen=True)
class CliProfile:
    """What the core needs to know about one human-facing CLI."""

    name: str
    display: str
    reader_factory: Callable[[float], Any]
    default_provider: str
    default_scope: str
    default_reader: str
    default_source: str


PROFILES: dict[str, CliProfile] = {
    "codex": CliProfile(
        name="codex",
        display="Codex",
        reader_factory=lambda timeout: CodexOAuthUsageReader(request_timeout_seconds=timeout),
        default_provider="codex",
        default_scope="openai:default",
        default_reader="codex-oauth",
        default_source="codex-oauth-wham-usage",
    ),
    "claude": CliProfile(
        name="claude",
        display="Claude Code",
        reader_factory=lambda timeout: ClaudeOAuthUsageReader(request_timeout_seconds=timeout),
        default_provider="claude",
        default_scope="anthropic:default",
        default_reader="claude-oauth",
        default_source="claude-oauth-usage",
    ),
}


def _profile(cli: str) -> CliProfile:
    try:
        return PROFILES[cli]
    except KeyError:
        raise ValueError(f"no quota handoff profile for CLI '{cli}'") from None


def _current_settings():
    from mindsync.config import settings

    return settings


def cache_path(cli: str) -> Path:
    return _current_settings().home / f"{cli}-standalone-usage-cache.json"


def asked_path(cli: str) -> Path:
    return _current_settings().home / f"{cli}-standalone-usage-warnings.json"


def usage_checks_enabled(*, memory_mode: str) -> bool:
    if memory_mode == "off":
        return False
    try:
        return bool(load_usage_config().enabled)
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Usage cache: one small JSON file per CLI, shared with the successor sort.
# --------------------------------------------------------------------------


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


def write_cache(result: UsageReadResult, *, cli: str) -> None:
    _current_settings().ensure_dirs()
    path = cache_path(cli)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": result.status,
        "provider": result.provider,
        "account_scope": result.account_scope,
        "reason": result.reason,
        "reader": result.reader,
        "source": result.source,
        "windows": [_serialize_window(window) for window in result.windows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def read_cache(
    *, cli: str, max_age_seconds: float = _MAX_CACHE_AGE_SECONDS
) -> UsageReadResult | None:
    profile = _profile(cli)
    path = cache_path(cli)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("status") != "available":
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
    windows = [
        window
        for item in raw.get("windows") or []
        if isinstance(item, dict) and (window := _deserialize_window(item)) is not None
    ]
    if not windows:
        return None
    return UsageReadResult.available(
        provider=str(raw.get("provider") or profile.default_provider),
        account_scope=str(raw.get("account_scope") or profile.default_scope),
        reader=str(raw.get("reader") or profile.default_reader),
        source=str(raw.get("source") or profile.default_source),
        windows=windows,
    )


def _bounded_prefetch_seconds(timeout_seconds: float) -> float:
    if timeout_seconds <= 0:
        return 0.0
    return max(0.05, min(timeout_seconds, _MAX_PREFETCH_SECONDS))


def prefetch_usage(*, cli: str, timeout_seconds: float) -> None:
    """Best-effort usage refresh with a strict deadline; never raises.

    The read runs on a daemon thread so a hung provider costs at most the bound,
    and a late answer is simply dropped instead of blocking hook exit.
    """
    profile = _profile(cli)
    bounded = _bounded_prefetch_seconds(timeout_seconds)
    if bounded <= 0:
        return

    result_box: list[UsageReadResult | None] = [None]
    finished = threading.Event()

    def _run() -> None:
        try:
            result_box[0] = profile.reader_factory(bounded).read()
        except Exception:
            result_box[0] = None
        finally:
            finished.set()

    worker = threading.Thread(target=_run, name=f"{cli}-usage-prefetch", daemon=True)
    worker.start()
    if not finished.wait(bounded):
        return
    result = result_box[0]
    if isinstance(result, UsageReadResult) and result.status == "available":
        write_cache(result, cli=cli)


# --------------------------------------------------------------------------
# Successor: a local sort over cached usage. No provider calls, no model.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Successor:
    agent: str
    used_percent: float | None


def _cached_evaluator(threshold_percent: float) -> Callable[..., ThresholdEvaluation]:
    """Evaluate an adapter from its cached usage file only.

    A CLI without a fresh cache is ranked neutrally rather than read live: a live
    read per installed agent is what pushed the old hook past its timeout.
    """

    def evaluate(adapter: Any, **_: Any) -> ThresholdEvaluation:
        observed = None
        if adapter.name in PROFILES:
            observed = read_cache(cli=adapter.name)
        if observed is None:
            observed = UsageReadResult.unavailable(
                provider=adapter.name,
                account_scope=adapter.quotaScope or f"agent:{adapter.name}",
                reason="no cached usage",
            )
        return evaluate_threshold(observed, threshold_percent=threshold_percent)

    return evaluate


def pick_successor(cli: str, threshold_percent: float) -> Successor | None:
    try:
        from mindsync.dispatch.routing import select_agent
        from mindsync.orchestration import effective_exclusions, load_policy

        decision = select_agent(
            "continue the current task",
            required_capabilities=["general"],
            exclude_agents=effective_exclusions([cli], load_policy(), cli),
            evaluator=_cached_evaluator(threshold_percent),
        )
    except Exception:
        return None
    agent = str(decision.get("agent") or "").strip()
    if not agent:
        return None
    used = decision.get("usedPercent")
    return Successor(agent=agent, used_percent=float(used) if used is not None else None)


# --------------------------------------------------------------------------
# Ask once per usage window per session.
# --------------------------------------------------------------------------


# Providers report the same reset a few seconds apart from one read to the next,
# so two resets this close together are one window.
_RESET_JITTER_SECONDS = 15 * 60


def _window_key(evaluation: ThresholdEvaluation) -> str:
    """Identify the usage window, not the reading.

    Used percent is deliberately left out: it moves on every turn, and keying on
    it re-asked the user at 91%, 92%, 93% ... of the same window.
    """
    window = evaluation.triggering_window
    if window is None:
        return "threshold|"
    reset = window.reset_at.astimezone(timezone.utc).isoformat() if window.reset_at else ""
    return f"{window.id}|{reset}"


def _same_window(stored: str | None, current: str) -> bool:
    if not stored:
        return False
    stored_id, _, stored_reset = stored.partition("|")
    current_id, _, current_reset = current.partition("|")
    if stored_id != current_id:
        return False
    if not stored_reset or not current_reset:
        return stored_reset == current_reset
    try:
        delta = datetime.fromisoformat(stored_reset) - datetime.fromisoformat(current_reset)
    except ValueError:
        return False
    return abs(delta.total_seconds()) <= _RESET_JITTER_SECONDS


def _load_asked(cli: str) -> dict[str, str]:
    path = asked_path(cli)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _save_asked(cli: str, state: dict[str, str]) -> None:
    _current_settings().ensure_dirs()
    if len(state) > _MAX_ASKED_SESSIONS:
        state = dict(list(state.items())[-_MAX_ASKED_SESSIONS:])
    asked_path(cli).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# The notice.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HandoffNotice:
    cli: str
    display: str
    window_label: str
    used_percent: float
    reset_at: datetime | None
    successor: Successor | None
    checkpoint_saved: bool

    def _reset_text(self) -> str:
        if self.reset_at is None:
            return ""
        local = self.reset_at.astimezone()
        if local.date() == datetime.now().astimezone().date():
            return f", resets at {local:%H:%M}"
        return f", resets {local:%a %d %b %H:%M}"

    def _successor_text(self) -> str:
        if self.successor is None:
            return "no other agent"
        if self.successor.used_percent is None:
            return self.successor.agent
        free = max(0.0, 100.0 - self.successor.used_percent)
        return f"{self.successor.agent} ({free:.0f}% free)"

    def warning(self) -> str:
        """One stderr line for the user's terminal."""
        checkpoint = " Checkpoint written." if self.checkpoint_saved else ""
        return (
            f"{self.display} seat nearing limit: {self.window_label} "
            f"{self.used_percent:.0f}% full{self._reset_text()}.{checkpoint} "
            f"Suggested successor: {self._successor_text()}. "
            "MindSync will not auto-start it."
        )

    def prompt(self) -> str:
        """The continuation reason the CLI hands back to the running agent."""
        head = (
            "[MindSync quota check: automatic message, not from the user] "
            f"Your {self.display} seat is at {self.used_percent:.0f}% of its "
            f"{self.window_label} usage window{self._reset_text()}."
        )
        if self.checkpoint_saved:
            head += " A MindSync checkpoint of this session was saved."
        if self.successor is None:
            return (
                f"{head} No other installed agent is free to take over. Tell the user "
                "this in one or two short lines and suggest wrapping up or pausing "
                "until the reset. Do not start new large work. MindSync will not "
                "remind you again in this window."
            )
        agent = self.successor.agent
        return (
            f"{head} Before anything else, tell the user this in one or two short "
            f"lines and ask: \"Hand off the rest of this task to {self._successor_text()}?\" "
            "Then stop and wait for the answer. "
            f"If they say yes, call the MindSync delegate_task tool with agent=\"{agent}\", "
            f"exclude_agents=[\"{self.cli}\"], and a self-contained prompt: the goal, what "
            "is done, what is left, the files involved and how to verify. Report the "
            "job id. If they say no, carry on with the task. Never hand off without "
            "the user's yes. MindSync will not ask again in this window."
        )


def check_handoff(
    cli: str,
    session_id: str,
    *,
    memory_mode: str,
    timeout_seconds: float,
    checkpoint_saved: bool,
    prefetch: Callable[..., None] | None = None,
) -> HandoffNotice | None:
    """Return a notice when this CLI's seat crossed the reserve for the first
    time in the current window of this session; otherwise None.

    The asked-state is saved *before* the successor sort, so a hook killed late
    still does not re-ask, and a failed save returns None so a block decision
    can never loop the agent.
    """
    profile = _profile(cli)
    if not usage_checks_enabled(memory_mode=memory_mode):
        return None
    (prefetch or prefetch_usage)(cli=cli, timeout_seconds=timeout_seconds)
    cached = read_cache(cli=cli)
    if cached is None:
        return None
    try:
        config = load_usage_config()
    except ValueError:
        return None
    threshold = effective_reserve_percent(usage_config=config)
    evaluation = evaluate_threshold(cached, threshold_percent=threshold)
    if evaluation.status != "at_threshold":
        return None

    key = _window_key(evaluation)
    state = _load_asked(cli)
    if _same_window(state.get(session_id), key):
        return None
    state.pop(session_id, None)
    state[session_id] = key
    try:
        _save_asked(cli, state)
    except OSError:
        return None

    window = evaluation.triggering_window
    return HandoffNotice(
        cli=cli,
        display=profile.display,
        window_label=(window.label or window.id) if window else "current",
        used_percent=window.used_percent if window else threshold,
        reset_at=window.reset_at if window else None,
        successor=pick_successor(cli, threshold),
        checkpoint_saved=checkpoint_saved,
    )


def stop_block_output(notice: HandoffNotice) -> str:
    """Stop-hook JSON shared by Codex and Claude Code: continue with a reason."""
    return json.dumps({"decision": "block", "reason": notice.prompt()}, ensure_ascii=False)
