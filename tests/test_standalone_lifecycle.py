"""Tests for generic standalone session-memory lifecycle (Phase 3B)."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.memory as memory_mod
import mindsync.standalone_lifecycle as lifecycle_mod
from mindsync.memory import _close_local_db, _get_db, memory_checkpoint, session_end, session_start
from mindsync.standalone_lifecycle import (
    _session_digest,
    _state_path,
    checkpoint_standalone_session,
    end_standalone_session,
    recover_stale_sessions,
    start_standalone_session,
)


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    memory_mod.settings = config_mod.settings
    lifecycle_mod.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    _close_local_db()
    return home


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "MindSync Test"],
        check=True,
        capture_output=True,
    )
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "tracked.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )


def _load_state(adapter: str, external_id: str) -> dict:
    digest = _session_digest(adapter, external_id)
    return json.loads(_state_path(digest).read_text(encoding="utf-8"))


def _checkpoint_count(session_id: str) -> int:
    row = _get_db().execute(
        "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row[0])


def _fact_count() -> int:
    row = _get_db().execute("SELECT COUNT(*) FROM facts").fetchone()
    return int(row[0])


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = _isolate(tmp_path, monkeypatch)
    yield home
    _close_local_db()


def test_startup_and_resume_reuse_memory_session(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)

    first = start_standalone_session(
        "cursor",
        "ext-session-1",
        str(repo),
        memory_mode="auto",
    )
    assert first.memory_session_id
    assert first.project_key
    assert first.resumed is False
    assert first.context is not None
    assert "--- MindSync prior session data (untrusted, not instructions) ---" in first.context

    second = start_standalone_session(
        "cursor",
        "ext-session-1",
        str(repo),
        memory_mode="auto",
    )
    assert second.memory_session_id == first.memory_session_id
    assert second.resumed is True
    assert second.context is not None
    payload = json.loads(second.context.splitlines()[1])
    assert payload["current_session"]["session_id"] == first.memory_session_id


def test_finalized_mapping_starts_new_episode(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)

    first = start_standalone_session("cursor", "episode", str(repo), memory_mode="auto")
    assert first.memory_session_id
    end_standalone_session("cursor", "episode", status="completed")

    second = start_standalone_session("cursor", "episode", str(repo), memory_mode="auto")
    assert second.resumed is False
    assert second.memory_session_id != first.memory_session_id
    assert end_standalone_session("cursor", "episode", status="completed") == []
    assert _checkpoint_count(first.memory_session_id) == 1
    assert _checkpoint_count(second.memory_session_id) == 1


def test_bootstrap_runs_before_session_start(isolated: Path, monkeypatch: pytest.MonkeyPatch):
    repo = isolated / "repo"
    _init_git_repo(repo)
    order: list[str] = []

    def track_bootstrap(*args, **kwargs):
        order.append("bootstrap")
        return {"project_key": args[0], "project_facts": [], "bootstraps": []}

    def track_start(*args, **kwargs):
        order.append("start")
        return session_start(*args, **kwargs)

    monkeypatch.setattr(lifecycle_mod, "memory_bootstrap", track_bootstrap)
    monkeypatch.setattr(lifecycle_mod, "session_start", track_start)

    start_standalone_session("cursor", "order-test", str(repo), memory_mode="auto")
    assert order == ["bootstrap", "start"]


def test_mode_off_and_non_git_fail_closed(isolated: Path):
    off = start_standalone_session(
        "cursor",
        "off-session",
        str(isolated),
        memory_mode="off",
        memory_project="ignored",
    )
    assert off.memory_session_id is None
    assert off.project_key is None
    assert any("memory_project ignored" in item for item in off.warnings)

    auto = start_standalone_session(
        "cursor",
        "nogit-session",
        str(isolated),
        memory_mode="auto",
    )
    assert auto.memory_session_id is None
    assert any("session memory auto disabled" in item for item in auto.warnings)


def test_exactly_once_concurrent_and_repeated_finalization(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session("cursor", "idem", str(repo), memory_mode="auto")
    session_id = started.memory_session_id
    assert session_id

    barrier = threading.Barrier(2)

    def finalize_once():
        barrier.wait()
        end_standalone_session("cursor", "idem", status="completed")

    threads = [threading.Thread(target=finalize_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert _checkpoint_count(session_id) == 1
    end_standalone_session("cursor", "idem", status="completed")
    assert _checkpoint_count(session_id) == 1

    state = _load_state("cursor", "idem")
    assert state["lifecycle_state"] == "finalized"


def test_crash_retry_uses_deterministic_terminal_checkpoint(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session("cursor", "crash", str(repo), memory_mode="auto")
    session_id = started.memory_session_id
    assert session_id
    digest = _session_digest("cursor", "crash")

    state = _load_state("cursor", "crash")
    state["lifecycle_state"] = "finalizing"
    _state_path(digest).write_text(json.dumps(state), encoding="utf-8")

    warnings = end_standalone_session("cursor", "crash", status="completed")
    assert warnings == []
    assert _checkpoint_count(session_id) == 1

    warnings = end_standalone_session("cursor", "crash", status="completed")
    assert warnings == []
    assert _checkpoint_count(session_id) == 1


def test_retry_after_session_end_failure_keeps_one_terminal_checkpoint(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session(
        "cursor", "end-retry", str(repo), memory_mode="auto"
    )
    session_id = started.memory_session_id
    assert session_id

    real_session_end = lifecycle_mod.session_end
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated shutdown between checkpoint and session end")
        return real_session_end(*args, **kwargs)

    monkeypatch.setattr(lifecycle_mod, "session_end", fail_once)

    warnings = end_standalone_session("cursor", "end-retry", status="completed")
    assert any("session end degraded" in item for item in warnings)
    assert _load_state("cursor", "end-retry")["lifecycle_state"] == "finalizing"
    assert _checkpoint_count(session_id) == 1

    assert end_standalone_session("cursor", "end-retry", status="completed") == []
    assert _load_state("cursor", "end-retry")["lifecycle_state"] == "finalized"
    assert _checkpoint_count(session_id) == 1


def test_memory_checkpoint_idempotent_for_same_session(isolated: Path):
    session_id = session_start(project_key="idem", agent="test", goal="goal")
    fixed_id = "a" * 32
    first = memory_checkpoint(session_id, status="done", checkpoint_id=fixed_id)
    second = memory_checkpoint(
        session_id,
        status="different",
        decisions=["should not write"],
        checkpoint_id=fixed_id,
    )
    assert first == second == fixed_id
    assert _checkpoint_count(session_id) == 1

    other = session_start(project_key="idem", agent="test", goal="goal")
    with pytest.raises(ValueError, match="another session"):
        memory_checkpoint(other, status="done", checkpoint_id=fixed_id)


def test_memory_checkpoint_idempotent_under_concurrent_retry(isolated: Path):
    session_id = session_start(project_key="idem-race", agent="test", goal="goal")
    fixed_id = "b" * 32
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[Exception] = []

    def write_same_checkpoint():
        try:
            barrier.wait()
            results.append(
                memory_checkpoint(
                    session_id, status="done", checkpoint_id=fixed_id
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            _close_local_db()

    threads = [threading.Thread(target=write_same_checkpoint) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results == [fixed_id, fixed_id]
    assert _checkpoint_count(session_id) == 1


def test_two_external_sessions_share_project_but_isolate_memory(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)

    first = start_standalone_session("cursor", "session-a", str(repo), memory_mode="auto")
    second = start_standalone_session("cursor", "session-b", str(repo), memory_mode="auto")

    assert first.project_key == second.project_key
    assert first.memory_session_id != second.memory_session_id


def test_stale_recovery_respects_limit_and_exclusion(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)

    current = start_standalone_session("cursor", "current", str(repo), memory_mode="auto")
    stale_ids = [f"stale-{index}" for index in range(3)]
    for external_id in stale_ids:
        start_standalone_session("cursor", external_id, str(repo), memory_mode="auto")

    old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    for index, external_id in enumerate(stale_ids):
        digest = _session_digest("cursor", external_id)
        state = _load_state("cursor", external_id)
        state["last_activity_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=30 + index)
        ).isoformat()
        _state_path(digest).write_text(json.dumps(state), encoding="utf-8")

    digest_current = _session_digest("cursor", "current")
    current_state = _load_state("cursor", "current")
    current_state["last_activity_at"] = old_time
    _state_path(digest_current).write_text(
        json.dumps(current_state),
        encoding="utf-8",
    )

    recover_stale_sessions(
        "cursor",
        exclude_external_session_id="current",
        stale_after_seconds=86_400,
        limit=2,
    )

    finalized = [
        external_id
        for external_id in stale_ids
        if _load_state("cursor", external_id)["lifecycle_state"] == "finalized"
    ]
    assert len(finalized) == 2
    assert _load_state("cursor", stale_ids[0])["lifecycle_state"] == "active"
    assert _load_state("cursor", "current")["lifecycle_state"] == "active"
    assert current.memory_session_id


def test_backend_failure_degrades_without_raising(isolated: Path, monkeypatch: pytest.MonkeyPatch):
    repo = isolated / "repo"
    _init_git_repo(repo)

    def broken_start(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(lifecycle_mod, "session_start", broken_start)

    result = start_standalone_session("cursor", "fail", str(repo), memory_mode="auto")
    assert result.memory_session_id is None
    assert any("session start degraded" in item for item in result.warnings)


def test_session_start_reaps_old_sessions_but_excludes_current(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    stale = start_standalone_session("cursor", "old", str(repo), memory_mode="auto")
    assert stale.memory_session_id
    stale_state = _load_state("cursor", "old")
    stale_state["last_activity_at"] = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).isoformat()
    _state_path(_session_digest("cursor", "old")).write_text(
        json.dumps(stale_state), encoding="utf-8"
    )

    current = start_standalone_session(
        "cursor", "current-start", str(repo), memory_mode="auto"
    )

    assert current.memory_session_id
    assert _load_state("cursor", "old")["lifecycle_state"] == "finalized"
    assert _load_state("cursor", "old")["terminal_status"] == "stale"
    assert _load_state("cursor", "current-start")["lifecycle_state"] == "active"


def test_reaper_completes_interrupted_finalizing_state(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session(
        "cursor", "half-finalized", str(repo), memory_mode="auto"
    )
    assert started.memory_session_id
    state = _load_state("cursor", "half-finalized")
    state["lifecycle_state"] = "finalizing"
    state["terminal_status"] = "completed"
    state["last_activity_at"] = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).isoformat()
    _state_path(_session_digest("cursor", "half-finalized")).write_text(
        json.dumps(state), encoding="utf-8"
    )

    recover_stale_sessions("cursor", stale_after_seconds=86_400)

    recovered = _load_state("cursor", "half-finalized")
    assert recovered["lifecycle_state"] == "finalized"
    assert recovered["terminal_status"] == "completed"
    assert _checkpoint_count(started.memory_session_id) == 1


def test_reaper_rechecks_freshness_under_session_lock(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = isolated / "repo"
    _init_git_repo(repo)
    start_standalone_session("cursor", "refresh-race", str(repo), memory_mode="auto")
    target_path = _state_path(_session_digest("cursor", "refresh-race"))
    state = _load_state("cursor", "refresh-race")
    state["last_activity_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=30)
    ).isoformat()
    target_path.write_text(json.dumps(state), encoding="utf-8")

    original_load = lifecycle_mod._load_state
    target_reads = 0

    def refresh_before_locked_recheck(path: Path):
        nonlocal target_reads
        current = original_load(path)
        if path == target_path and current is not None:
            target_reads += 1
            if target_reads == 2:
                current["last_activity_at"] = datetime.now(timezone.utc).isoformat()
                lifecycle_mod._save_state(path, current)
        return current

    monkeypatch.setattr(lifecycle_mod, "_load_state", refresh_before_locked_recheck)

    assert recover_stale_sessions(
        "cursor", stale_after_seconds=86_400, limit=1
    ) == []
    assert _load_state("cursor", "refresh-race")["lifecycle_state"] == "active"


def test_terminal_db_contention_stays_within_hook_budget(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session(
        "cursor", "db-contention", str(repo), memory_mode="auto"
    )
    assert started.memory_session_id

    blocker = sqlite3.connect(
        config_mod.settings.memory_db_file, isolation_level=None, timeout=1.0
    )
    blocker.execute("BEGIN IMMEDIATE")
    try:
        began = time.monotonic()
        warnings = end_standalone_session(
            "cursor", "db-contention", status="completed"
        )
        elapsed = time.monotonic() - began
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert elapsed < 2.5
    assert any("database is locked" in item for item in warnings)
    assert _load_state("cursor", "db-contention")["lifecycle_state"] == "finalizing"
    assert end_standalone_session(
        "cursor", "db-contention", status="completed"
    ) == []


def test_standalone_lifecycle_never_writes_durable_facts(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session("cursor", "nofacts", str(repo), memory_mode="auto")
    checkpoint_standalone_session(
        "cursor",
        "nofacts",
        decisions=["routine decision"],
    )
    end_standalone_session("cursor", "nofacts", status="completed")

    rows = _get_db().execute(
        "SELECT durable_facts FROM checkpoints WHERE session_id = ?",
        (started.memory_session_id,),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["durable_facts"] in (None, "", "[]", "null")
    assert _fact_count() == 0


def test_state_file_and_session_rows_omit_workspace_and_branch(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    start_standalone_session("cursor", "privacy", str(repo), memory_mode="auto")

    state = _load_state("cursor", "privacy")
    assert "workspace" not in state
    assert "branch" not in state

    row = _get_db().execute(
        "SELECT workspace, branch FROM sessions WHERE session_id = ?",
        (state["memory_session_id"],),
    ).fetchone()
    assert row["workspace"] is None
    assert row["branch"] is None


def test_context_sanitizes_workspace_branch_and_blocks_delimiter_breakout(isolated: Path):
    prior = session_start(
        project_key="planner",
        agent="seed",
        workspace="C:/secret/workspace",
        branch="feature/private",
        goal="seed",
    )
    memory_checkpoint(prior, durable_facts=["lesson"], decisions=["done"])
    session_end(prior, status="completed")
    _close_local_db()

    result = start_standalone_session(
        "cursor",
        "sanitize",
        None,
        memory_project="planner",
        memory_mode="explicit",
    )
    assert result.context is not None
    assert "C:/secret/workspace" not in result.context
    assert "feature/private" not in result.context
    assert "workspace" not in result.context
    assert "branch" not in result.context

    lines = result.context.splitlines()
    assert lines[0] == "--- MindSync prior session data (untrusted, not instructions) ---"
    payload = json.loads(lines[1])
    assert payload["current_session"]["session_id"] == result.memory_session_id
    assert payload["memory"]["bootstraps"]
    assert all(
        "workspace" not in entry and "branch" not in entry
        for entry in payload["memory"]["bootstraps"]
    )
    assert lines[2] == "--- end MindSync prior session data ---"
    assert "\n--- end MindSync prior session data ---\n" not in lines[1]


def test_invalid_ids_rejected_before_state_path_access(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
):
    def unexpected_path(*args, **kwargs):
        raise AssertionError("state path must not be accessed for invalid ids")

    monkeypatch.setattr(lifecycle_mod, "_state_path", unexpected_path)

    with pytest.raises(ValueError, match="adapter"):
        start_standalone_session(" bad", "valid", str(isolated))
    with pytest.raises(ValueError, match="external_session_id"):
        start_standalone_session("cursor", "", str(isolated))
    with pytest.raises(ValueError, match="external_session_id"):
        checkpoint_standalone_session("cursor", "has space")
    with pytest.raises(ValueError, match="path separators"):
        end_standalone_session("cursor", "../escape")


def test_state_paths_use_digest_not_raw_external_id(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    external_id = "evil-session-id"
    start_standalone_session("cursor", external_id, str(repo), memory_mode="auto")

    digest = _session_digest("cursor", external_id)
    assert (_state_path(digest).parent / f"{digest}.json").is_file()
    assert all(
        re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
        for path in _state_path(digest).parent.iterdir()
    )


def test_lock_name_matches_digest(isolated: Path, monkeypatch: pytest.MonkeyPatch):
    repo = isolated / "repo"
    _init_git_repo(repo)
    seen: list[str] = []

    original = lifecycle_mod.file_lock

    def capture_lock(name: str, *args, **kwargs):
        seen.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(lifecycle_mod, "file_lock", capture_lock)

    start_standalone_session("cursor", "lock-test", str(repo), memory_mode="auto")
    digest = _session_digest("cursor", "lock-test")
    assert seen == [f"standalone-session-{digest}"]


def test_checkpoint_updates_active_session(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session("cursor", "active", str(repo), memory_mode="auto")
    warnings = checkpoint_standalone_session(
        "cursor",
        "active",
        status="working",
        decisions=["step done"],
    )
    assert warnings == []
    row = _get_db().execute(
        "SELECT status FROM sessions WHERE session_id = ?",
        (started.memory_session_id,),
    ).fetchone()
    assert row["status"] == "working"


def test_checkpoint_zero_budget_stops_before_database(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = isolated / "repo"
    _init_git_repo(repo)
    start_standalone_session("cursor", "deadline", str(repo), memory_mode="auto")

    def unexpected_checkpoint(*args, **kwargs):
        raise AssertionError("database checkpoint must not start after the deadline")

    monkeypatch.setattr(lifecycle_mod, "memory_checkpoint", unexpected_checkpoint)
    # The database must not be touched after the deadline — but the caller gets
    # a warning, not an exception. Every other failure in this module degrades,
    # the return type is unchanged, and only the Codex hook happens to wrap
    # these calls; anything else would get an exception where it used to get a
    # value.
    warnings = checkpoint_standalone_session(
        "cursor",
        "deadline",
        decisions=["too late"],
        timeout_seconds=0,
    )
    assert any("deadline exhausted" in item for item in warnings)


def test_checkpoint_skips_empty_and_duplicate_heartbeats(isolated: Path):
    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session(
        "cursor", "dedupe-heartbeat", str(repo), memory_mode="auto"
    )

    assert checkpoint_standalone_session(
        "cursor", "dedupe-heartbeat", files_changed=["mindsync/memory.py"]
    ) == []
    assert checkpoint_standalone_session(
        "cursor", "dedupe-heartbeat", files_changed=["mindsync/memory.py"]
    ) == []
    assert checkpoint_standalone_session("cursor", "dedupe-heartbeat") == []

    count = _get_db().execute(
        "SELECT COUNT(*) AS n FROM checkpoints WHERE session_id = ?",
        (started.memory_session_id,),
    ).fetchone()["n"]
    assert count == 1


def test_auto_project_key_matches_git_pattern(isolated: Path):
    repo = isolated / "private-repo-name"
    _init_git_repo(repo)
    result = start_standalone_session("cursor", "git", str(repo), memory_mode="auto")
    assert result.project_key is not None
    assert re.fullmatch(r"git-[0-9a-f]{64}", result.project_key)
    assert "private-repo-name" not in result.project_key


def test_git_probes_are_bounded_to_the_hook_budget(monkeypatch, tmp_path):
    """Codex allows the hook 3s; the dispatch default of 15s per probe does not fit.

    Two probes at 15s can outlive the hook by 10x, and a killed hook can strand a
    session row whose state file was never written.
    """
    from mindsync.dispatch import memory_lifecycle as ml
    from mindsync import standalone_lifecycle as core

    seen: list[float | None] = []

    def fake_git(cwd, *args, ignore_ambient_repo=False, timeout=15.0):
        seen.append(timeout)
        return None

    monkeypatch.setattr("mindsync.dispatch.worktree._git", fake_git)
    # A real directory, not "/tmp": the resolver returns before probing when the
    # workspace does not exist, and "/tmp" does not exist on Windows.
    ml._infer_git_project_key(str(tmp_path), git_timeout=core._GIT_TIMEOUT_SECONDS)

    assert seen, "no git probe ran"
    assert all(value <= 3.0 for value in seen), seen
    assert seen == sorted(seen, reverse=True), seen


def test_stale_recovery_failure_does_not_deny_this_session(monkeypatch, tmp_path):
    """Reaping someone else's abandoned session is a courtesy, not a precondition."""
    from mindsync import standalone_lifecycle as core

    def boom(*args, **kwargs):
        raise TimeoutError("another process is finalizing that mapping")

    monkeypatch.setattr(core, "recover_stale_sessions", boom)
    result = core.start_standalone_session(
        "codex", "sess-recovery", str(tmp_path), memory_mode="off"
    )
    # Reached the end rather than propagating the TimeoutError.
    assert result.memory_session_id is None
    assert isinstance(result.warnings, list)


def test_mode_off_touches_nothing(monkeypatch, tmp_path):
    """`off` is documented as an opt-out, so it must not reap, bootstrap or write."""
    from mindsync import standalone_lifecycle as core

    called: list[str] = []
    monkeypatch.setattr(
        core, "recover_stale_sessions", lambda *a, **k: called.append("reaper") or []
    )
    monkeypatch.setattr(
        core, "session_start", lambda **k: called.append("session_start") or "x"
    )

    result = core.start_standalone_session(
        "codex", "sess-off", str(tmp_path), memory_mode="off"
    )
    assert result.memory_session_id is None
    assert called == [], f"off still did work: {called}"


def test_mode_off_still_reports_an_ignored_project():
    from mindsync import standalone_lifecycle as core

    result = core.start_standalone_session(
        "codex", "sess-off-2", None, memory_mode="off", memory_project="planner"
    )
    assert any("memory_project ignored" in item for item in result.warnings)


def test_exhausted_budget_does_not_strand_a_healthy_session(isolated: Path):
    """Finalize must check the budget before it mutates anything.

    Flipping lifecycle_state to "finalizing" and only then discovering there is
    no time left leaves the state file saying finalizing while the DB row is
    still active with no ended_at. "finalizing" is not resumable, so the session
    is unusable until the 24h stale reaper reaches it.
    """
    import time

    import mindsync.memory as memory_mod

    repo = isolated / "repo"
    _init_git_repo(repo)
    started = start_standalone_session(
        "cursor", "strand", str(repo), memory_mode="auto"
    )
    assert started.memory_session_id

    digest = lifecycle_mod._session_digest("cursor", "strand")
    state = lifecycle_mod._load_state(lifecycle_mod._state_path(digest))
    warnings: list[str] = []
    lifecycle_mod._finalize_state(
        state, digest, "completed", warnings, deadline=time.monotonic() - 1
    )

    after = lifecycle_mod._load_state(lifecycle_mod._state_path(digest))
    assert after["lifecycle_state"] == "active", after["lifecycle_state"]
    assert after["lifecycle_state"] in lifecycle_mod._RESUMABLE_STATES
    row = memory_mod._get_db().execute(
        "SELECT status, ended_at FROM sessions WHERE session_id = ?",
        (started.memory_session_id,),
    ).fetchone()
    assert row["status"] == "active" and row["ended_at"] is None


def test_public_entry_points_degrade_rather_than_raise(isolated: Path):
    """Memory is optional here; a spent budget is a warning, not an exception."""
    repo = isolated / "repo"
    _init_git_repo(repo)

    started = start_standalone_session(
        "cursor", "degrade", str(repo), memory_mode="auto", timeout_seconds=0
    )
    assert started.memory_session_id is None
    assert any("skipped" in item for item in started.warnings)

    assert any(
        "skipped" in item
        for item in checkpoint_standalone_session(
            "cursor", "degrade", status="active", timeout_seconds=0
        )
    )
    assert any(
        "skipped" in item
        for item in end_standalone_session(
            "cursor", "degrade", status="completed", timeout_seconds=0
        )
    )
