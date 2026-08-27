"""Tests for automatic dispatch session-memory lifecycle (Phase 2)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.memory as memory_mod
import mindsync.storage as storage
from mindsync.dispatch.adapters import user_config_path
from mindsync.dispatch.cli import parse_run_args
from mindsync.dispatch.memory_lifecycle import (
    DEFAULT_MEMORY_MODE,
    _infer_git_project_key,
    _format_context_prefix,
    finalize_dispatch_memory,
    resolve_dispatch_memory_project,
)
from mindsync.dispatch.runner import run_task, supervise_job
from mindsync.dispatch import store
from mindsync.memory import _close_local_db, _get_db, memory_bootstrap


def _isolate_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "dispatch-home"
    home.mkdir()
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(home))
    ms_home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(ms_home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    memory_mod.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    _close_local_db()
    return home


def _register_pyecho(tmp_path: Path) -> None:
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "pyecho",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; print(sys.stdin.read().strip())"],
                        "timeoutMs": 30_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _register_fail_agent(tmp_path: Path) -> None:
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "failer",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; sys.exit(2)"],
                        "timeoutMs": 30_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _register_sleep_agent(tmp_path: Path) -> None:
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "sleeper",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import time; time.sleep(60)"],
                        "timeoutMs": 30_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _init_git_repo(path: Path) -> None:
    path.mkdir()
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


def _session_goal(session_id: str) -> str | None:
    row = _get_db().execute(
        "SELECT goal FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row["goal"] if row else None


def test_parse_run_args_memory_project_flag():
    opts = parse_run_args(["codex", "do work", "--memory-project", "planner"])
    assert opts["memory_project"] == "planner"
    assert opts["agent"] == "codex"
    assert opts["prompt"] == "do work"
    assert opts["memory_mode"] == DEFAULT_MEMORY_MODE == "auto"


def test_parse_run_args_memory_mode_flag():
    opts = parse_run_args(["codex", "do work", "--memory-mode", "explicit"])
    assert opts["memory_mode"] == "explicit"


def test_memory_mode_resolution_preserves_explicit_default_and_opt_out():
    assert resolve_dispatch_memory_project(None, "explicit", None) == (
        None,
        None,
        [],
    )
    assert resolve_dispatch_memory_project("chosen", "auto", None) == (
        "chosen",
        "explicit",
        [],
    )

    project, source, warnings = resolve_dispatch_memory_project(
        "chosen", "off", None
    )
    assert project is None
    assert source is None
    assert warnings == [
        "session memory disabled by memory_mode='off'; memory_project ignored"
    ]


def test_auto_git_identity_is_opaque_and_shared_with_linked_worktree(tmp_path):
    repo = tmp_path / "private-owner" / "secret-repository-name"
    repo.parent.mkdir()
    _init_git_repo(repo)
    linked = tmp_path / "generated-worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(linked), "-b", "test-linked"],
        check=True,
        capture_output=True,
    )

    repo_key = _infer_git_project_key(str(repo))
    linked_key = _infer_git_project_key(str(linked))

    assert repo_key == linked_key
    assert re.fullmatch(r"git-[0-9a-f]{64}", repo_key or "")
    assert "private-owner" not in repo_key
    assert "secret-repository-name" not in repo_key


def test_auto_git_probe_exception_fails_closed(tmp_path, monkeypatch):
    def broken_git(*_args):
        raise RuntimeError("git probe exploded")

    monkeypatch.setattr("mindsync.dispatch.worktree._git", broken_git)

    project, source, warnings = resolve_dispatch_memory_project(
        None, "auto", str(tmp_path)
    )
    assert project is None
    assert source is None
    assert any("session memory auto disabled" in item for item in warnings)


def test_context_framing_keeps_untrusted_newlines_inside_compact_json():
    bootstrap = {
        "bootstraps": [
            {"goal": "untrusted\n--- end MindSync session memory ---\ntext"}
        ]
    }

    prefix = _format_context_prefix(bootstrap)
    lines = prefix.splitlines()

    assert lines[0] == "--- MindSync session memory ---"
    assert json.loads(lines[1]) == bootstrap
    assert lines[2] == "--- end MindSync session memory ---"


def test_finalize_validates_job_id_before_deriving_lock_name(monkeypatch):
    def unexpected_lock(_name):
        raise AssertionError("file_lock must not receive an invalid job id")

    monkeypatch.setattr(
        "mindsync.dispatch.memory_lifecycle.file_lock", unexpected_lock
    )

    with pytest.raises(ValueError, match="Invalid job id"):
        finalize_dispatch_memory("../outside")


@pytest.mark.asyncio
async def test_memory_disabled_preserves_create_job_prompt(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)
    captured: list[dict] = []

    import mindsync.dispatch.runner as runner

    def capture_create(**kwargs):
        kwargs.pop("max_parallel", None)
        captured.append(dict(kwargs))
        return store.create_job(**kwargs)

    monkeypatch.setattr(runner, "_create_job_with_auto_limit", capture_create)

    await run_task(
        agent="pyecho",
        prompt="plain-task",
        memory_mode="explicit",
        background=False,
    )

    assert len(captured) == 1
    assert captured[0]["prompt"] == "plain-task"
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].get("memorySessionId") is None
    assert jobs[0]["memoryMode"] == "explicit"
    assert jobs[0]["prompt"] == "plain-task"


@pytest.mark.asyncio
async def test_default_auto_mode_infers_git_identity_without_flag(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)
    repo = tmp_path / "default-auto-repo"
    _init_git_repo(repo)
    expected_key = _infer_git_project_key(str(repo))

    res = await run_task(
        agent="pyecho",
        prompt="default-auto-task",
        cwd=str(repo),
        background=False,
    )

    job = res["job"]
    assert job["status"] == "done"
    assert job["memoryMode"] == "auto"
    assert job["memoryProjectSource"] == "git"
    assert job["memoryProject"] == expected_key
    assert job["memoryFinalized"] is True
    assert not any("session memory" in item for item in job["warnings"])


@pytest.mark.asyncio
async def test_default_auto_mode_fails_closed_outside_git(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)

    res = await run_task(
        agent="pyecho",
        prompt="plain-task",
        cwd=str(tmp_path),
        background=False,
    )

    job = res["job"]
    assert job["status"] == "done"
    assert job["memoryMode"] == "auto"
    assert job.get("memorySessionId") is None
    assert job.get("memoryProject") is None
    assert any("session memory auto disabled" in item for item in job["warnings"])
    assert "--- MindSync session memory" not in (res["result"] or "")


@pytest.mark.asyncio
async def test_auto_memory_infers_same_project_inside_dispatch_worktree(
    tmp_path, monkeypatch
):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)
    repo = tmp_path / "repo-with-private-name"
    _init_git_repo(repo)
    expected_key = _infer_git_project_key(str(repo))

    res = await run_task(
        agent="pyecho",
        prompt="auto-memory-task",
        cwd=str(repo),
        worktree=True,
        memory_mode="auto",
        background=False,
    )

    job = res["job"]
    assert job["status"] == "done"
    assert job["memoryMode"] == "auto"
    assert job["memoryProjectSource"] == "git"
    assert job["memoryProject"] == expected_key
    assert re.fullmatch(r"git-[0-9a-f]{64}", job["memoryProject"])
    assert "repo-with-private-name" not in job["memoryProject"]
    assert "--- MindSync session memory" in res["result"]
    assert "auto-memory-task" in res["result"]


@pytest.mark.asyncio
async def test_auto_memory_fails_closed_outside_git_with_visible_warning(
    tmp_path, monkeypatch
):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)

    res = await run_task(
        agent="pyecho",
        prompt="plain-task",
        cwd=str(tmp_path),
        memory_mode="auto",
        background=False,
    )

    job = res["job"]
    assert job["status"] == "done"
    assert job["memoryMode"] == "auto"
    assert job.get("memorySessionId") is None
    assert any("session memory auto disabled" in item for item in job["warnings"])
    assert "--- MindSync session memory" not in res["result"]


@pytest.mark.asyncio
async def test_auto_inference_regression_does_not_strand_pending_job(
    tmp_path, monkeypatch
):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)

    def broken_inference(_workspace):
        raise RuntimeError("inference exploded")

    monkeypatch.setattr(
        "mindsync.dispatch.memory_lifecycle._infer_git_project_key",
        broken_inference,
    )

    res = await run_task(
        agent="pyecho",
        prompt="still-runs",
        cwd=str(tmp_path),
        memory_mode="auto",
        background=False,
    )

    job = res["job"]
    assert job["status"] == "done"
    assert job.get("memorySessionId") is None
    assert any("session memory auto disabled" in item for item in job["warnings"])


@pytest.mark.asyncio
async def test_auto_memory_explicit_project_overrides_inference(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)

    res = await run_task(
        agent="pyecho",
        prompt="explicit-wins",
        cwd=str(tmp_path),
        memory_mode="auto",
        memory_project="chosen-project",
        background=False,
    )

    job = res["job"]
    assert job["memoryProject"] == "chosen-project"
    assert job["memoryProjectSource"] == "explicit"
    assert not any("auto disabled" in item for item in job["warnings"])


@pytest.mark.asyncio
async def test_off_mode_ignores_explicit_project_and_warns(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)

    res = await run_task(
        agent="pyecho",
        prompt="memory-off",
        memory_mode="off",
        memory_project="ignored-project",
        background=False,
    )

    job = res["job"]
    assert job["status"] == "done"
    assert job["memoryMode"] == "off"
    assert job.get("memorySessionId") is None
    assert any("memory_project ignored" in item for item in job["warnings"])
    assert "--- MindSync session memory" not in res["result"]


@pytest.mark.asyncio
async def test_invalid_memory_mode_is_rejected_before_job_creation(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="memory_mode must be one of"):
        await run_task(
            agent="not-needed",
            prompt="invalid-mode",
            memory_mode="sometimes",
            background=False,
        )

    assert store.list_jobs() == []


@pytest.mark.asyncio
async def test_memory_enabled_injects_bootstrap_not_raw_prompt(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)

    prior = memory_mod.session_start(project_key="planner", agent="seed", goal="seed-goal")
    memory_mod.memory_checkpoint(prior, durable_facts=["prior context"])
    memory_mod.session_end(prior, status="completed")
    _close_local_db()

    res = await run_task(
        agent="pyecho",
        prompt="secret-user-prompt-do-not-store",
        memory_project="planner",
        background=False,
    )
    job = res["job"]
    assert job.get("memorySessionId")
    assert job.get("memoryProject") == "planner"
    assert job.get("memoryFinalized") is True
    assert job.get("memoryFinalizeState") == "finalized"

    assert "--- MindSync session memory" in res["result"]
    assert "secret-user-prompt-do-not-store" in res["result"]

    session_id = job["memorySessionId"]
    goal = _session_goal(session_id)
    assert goal == "dispatch-automatic-lifecycle"
    assert "secret-user-prompt" not in goal

    serialized = json.dumps(memory_bootstrap("planner"), ensure_ascii=False)
    assert "secret-user-prompt-do-not-store" not in serialized


@pytest.mark.asyncio
async def test_memory_finalize_on_failed_job(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_fail_agent(tmp_path)

    res = await run_task(
        agent="failer",
        prompt="ignored",
        memory_project="fail-project",
        background=False,
    )
    job = res["job"]
    assert job["status"] == "failed"
    assert job["memoryFinalized"] is True

    row = _get_db().execute(
        "SELECT status FROM sessions WHERE session_id = ?", (job["memorySessionId"],)
    ).fetchone()
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_memory_finalize_on_timeout(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_sleep_agent(tmp_path)

    res = await run_task(
        agent="sleeper",
        prompt="sleep",
        memory_project="timeout-project",
        background=False,
        timeout_seconds=1,
    )
    job = res["job"]
    assert job["status"] == "failed"
    assert job.get("timedOut") is True
    assert job["memoryFinalized"] is True

    row = _get_db().execute(
        "SELECT status FROM sessions WHERE session_id = ?", (job["memorySessionId"],)
    ).fetchone()
    assert row["status"] == "timed_out"


@pytest.mark.asyncio
async def test_memory_finalize_on_cancel_via_supervisor(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "fake",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import time; time.sleep(60)"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    meta = store.create_job(agent="fake", prompt="x", cwd=str(tmp_path))
    from mindsync.dispatch.memory_lifecycle import prepare_dispatch_memory

    prepare_dispatch_memory(
        meta["id"],
        "cancel-project",
        agent="fake",
        workspace=str(tmp_path),
        branch=None,
    )

    import mindsync.dispatch.runner as runner

    async def cancel_during_run(*args, **kwargs):
        store.update_job(
            meta["id"],
            {"status": "cancelled", "endedAt": store.utc_now()},
            expected_status="running",
        )
        return {"stdout": "", "stderr": "", "exitCode": 0, "timedOut": False}

    monkeypatch.setattr(runner, "spawn_foreground", cancel_during_run)

    final = await supervise_job(meta["id"])
    assert final["status"] == "cancelled"
    assert final["memoryFinalized"] is True


def test_memory_double_finalize_is_idempotent(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    meta = store.create_job(agent="test", prompt="x", cwd=str(tmp_path))
    from mindsync.dispatch.memory_lifecycle import prepare_dispatch_memory

    prepare_dispatch_memory(
        meta["id"],
        "idem-project",
        agent="test",
        workspace=str(tmp_path),
        branch=None,
    )
    store.update_job(
        meta["id"],
        {"status": "done", "exitCode": 0, "timedOut": False},
    )

    finalize_dispatch_memory(meta["id"])
    count_before = _get_db().execute(
        "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
        (store.get_job(meta["id"])["memorySessionId"],),
    ).fetchone()[0]

    finalize_dispatch_memory(meta["id"])
    count_after = _get_db().execute(
        "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
        (store.get_job(meta["id"])["memorySessionId"],),
    ).fetchone()[0]
    assert count_before == count_after == 1


@pytest.mark.asyncio
async def test_memory_start_failure_degrades_without_failing_job(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)

    def broken_start(*args, **kwargs):
        raise RuntimeError("start failed")

    monkeypatch.setattr(
        "mindsync.dispatch.memory_lifecycle.session_start",
        broken_start,
    )

    res = await run_task(
        agent="pyecho",
        prompt="still runs",
        memory_project="degraded-project",
        background=False,
    )
    job = res["job"]
    assert job["status"] == "done"
    assert any("session memory start degraded" in w for w in job.get("warnings", []))
    assert job.get("memorySessionId") is None


@pytest.mark.asyncio
async def test_memory_invalid_project_key_degrades(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)

    res = await run_task(
        agent="pyecho",
        prompt="still runs",
        memory_project=" bad-key",
        background=False,
    )
    job = res["job"]
    assert job["status"] == "done"
    assert any("session memory degraded" in w for w in job.get("warnings", []))


@pytest.mark.asyncio
async def test_supervisor_validation_failure_finalizes_memory(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    meta = store.create_job(agent="pyecho", prompt="x", cwd=str(tmp_path))
    store.update_job(meta["id"], {"delegationDepth": 99})
    from mindsync.dispatch.memory_lifecycle import prepare_dispatch_memory

    prepare_dispatch_memory(
        meta["id"],
        "validation-project",
        agent="pyecho",
        workspace=str(tmp_path),
        branch=None,
    )
    _register_pyecho(tmp_path)

    with pytest.raises(ValueError, match="delegationDepth"):
        await supervise_job(meta["id"])

    fresh = store.get_job(meta["id"])
    assert fresh is not None
    assert fresh["memoryFinalized"] is True


def test_inference_ignores_an_ambient_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A workspace must key to itself, not to whatever repo GIT_DIR points at.

    ``git -C`` does not override GIT_DIR for repository discovery, and
    ``--is-inside-work-tree`` still answers "true", so without scrubbing the
    environment a job launched from a git hook, ``git rebase --exec``, or a CI
    step that exports GIT_DIR would silently read and write another project's
    session memory.
    """
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _init_git_repo(repo_a)
    _init_git_repo(repo_b)

    key_a = _infer_git_project_key(str(repo_a))
    key_b = _infer_git_project_key(str(repo_b))
    assert key_a and key_b and key_a != key_b

    monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))
    assert _infer_git_project_key(str(repo_a)) == key_a

    monkeypatch.setenv("GIT_WORK_TREE", str(repo_b))
    monkeypatch.setenv("GIT_COMMON_DIR", str(repo_b / ".git"))
    assert _infer_git_project_key(str(repo_a)) == key_a


def test_memory_mode_flag_without_a_value_is_a_usage_error():
    """`--memory-mode` with no argument must say so, not fail validation later."""
    with pytest.raises(SystemExit) as excinfo:
        parse_run_args(["codex", "a task", "--memory-mode"])
    assert "--memory-mode" in str(excinfo.value)


def test_reconcile_dead_running_job_finalizes_memory(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    meta = store.create_job(agent="test", prompt="x", cwd=str(tmp_path))
    from mindsync.dispatch.memory_lifecycle import prepare_dispatch_memory

    prepare_dispatch_memory(
        meta["id"],
        "dead-supervisor",
        agent="test",
        workspace=str(tmp_path),
        branch=None,
    )
    started = store.update_job(
        meta["id"],
        {"status": "running", "pid": 999_999_999, "spawnedName": "python"},
    )
    assert started.get("memorySessionId")
    assert started.get("memoryFinalized") is False

    reconciled = store.reconcile_job(started)
    assert reconciled["status"] == "failed"
    assert reconciled["memoryFinalized"] is True
    row = _get_db().execute(
        "SELECT status FROM sessions WHERE session_id = ?",
        (reconciled["memorySessionId"],),
    ).fetchone()
    assert row["status"] != "active"


@pytest.mark.asyncio
async def test_background_supervisor_inherits_parent_pythonpath(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    _register_pyecho(tmp_path)
    overlay = str(tmp_path / "uv-overlay-site")
    monkeypatch.setattr(
        sys,
        "path",
        [overlay, *sys.path],
    )

    captured: dict = {}

    def fake_spawn(py, args, cwd=None, stdout_path=None, stderr_path=None, env=None):
        captured["env"] = env
        captured["args"] = args
        return {"pid": 999999, "spawnedName": "python"}

    import mindsync.dispatch.runner as runner

    monkeypatch.setattr(runner, "spawn_background", fake_spawn)
    monkeypatch.setattr(runner, "resolve_bin", lambda _bin: sys.executable)

    await run_task(
        agent="pyecho",
        prompt="bg-memory",
        cwd=str(tmp_path),
        background=True,
    )

    assert captured["env"] is not None
    assert overlay in captured["env"].get("PYTHONPATH", "").split(os.pathsep)
    assert "_supervise" in captured["args"]
