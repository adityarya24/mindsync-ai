"""Tests for mechanical review gate in mindsync dispatch."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import mindsync.config as config_mod
from mindsync.dispatch.adapters import user_config_path
from mindsync.dispatch.review import (
    CheckResult,
    diff_summary,
    format_review,
    run_checks,
    verdict,
)
from mindsync.dispatch.runner import run_task
import mindsync.storage as storage


def _isolate_dispatch(tmp_path: Path, monkeypatch):
    home = tmp_path / "dispatch-home"
    home.mkdir()
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(home))
    ms_home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(ms_home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    return home


def _register_python_agent(tmp_path: Path, monkeypatch, script: str) -> None:
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "pyagent",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", script],
                        "timeoutMs": 30_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_passing_and_failing_checks(tmp_path):
    cmd_pass = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    cmd_fail = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
    results = run_checks(str(tmp_path), [cmd_pass, cmd_fail])
    assert len(results) == 2
    assert results[0].passed is True
    assert results[0].exitCode == 0
    assert results[1].passed is False
    assert results[1].exitCode == 1


def test_check_timeout(tmp_path):
    cmd_timeout = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    results = run_checks(str(tmp_path), [cmd_timeout], timeout_ms=200)
    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].exitCode is None


def test_check_output_truncated_to_tail(tmp_path):
    cmd_long = f'"{sys.executable}" -c "print(\'A\' * 3000 + \'END_MARKER\')"'
    results = run_checks(str(tmp_path), [cmd_long])
    assert len(results) == 1
    output = results[0].output
    assert len(output) <= 2000
    assert output.endswith("END_MARKER\n") or output.endswith("END_MARKER\r\n") or output.endswith("END_MARKER")


def test_verdict_fails_when_job_failed():
    meta = {
        "status": "failed",
        "exitCode": 1,
        "write": False,
        "checkResults": [
            {
                "name": "c1",
                "passed": True,
                "exitCode": 0,
                "output": "",
                "durationMs": 10,
            }
        ],
    }
    v = verdict(meta)
    assert v["passed"] is False
    assert any("failed" in r for r in v["reasons"])


def test_verdict_fails_when_write_job_changed_nothing():
    meta = {
        "status": "done",
        "exitCode": 0,
        "write": True,
        "checkResults": [
            {
                "name": "c1",
                "passed": True,
                "exitCode": 0,
                "output": "",
                "durationMs": 10,
            }
        ],
        "diff": {
            "filesChanged": 0,
            "insertions": 0,
            "deletions": 0,
            "files": [],
            "commits": 0,
        },
    }
    v = verdict(meta)
    assert v["passed"] is False
    assert "no files changed by a write job" in v["reasons"]


def test_verdict_passes_for_successful_write_job_with_diff():
    meta = {
        "status": "done",
        "exitCode": 0,
        "write": True,
        "checkResults": [
            {
                "name": "c1",
                "passed": True,
                "exitCode": 0,
                "output": "",
                "durationMs": 10,
            }
        ],
        "diff": {
            "filesChanged": 1,
            "insertions": 5,
            "deletions": 0,
            "files": ["a.txt"],
            "commits": 1,
        },
    }
    v = verdict(meta)
    assert v["passed"] is True
    assert v["reasons"] == []


def test_diff_summary_non_git_directory(tmp_path):
    res1 = diff_summary(str(tmp_path), base_commit="abcdef123456")
    assert res1 == {
        "filesChanged": 0,
        "insertions": 0,
        "deletions": 0,
        "files": [],
        "commits": 0,
    }
    res2 = diff_summary(str(tmp_path), base_commit=None)
    assert res2 == {
        "filesChanged": 0,
        "insertions": 0,
        "deletions": 0,
        "files": [],
        "commits": 0,
    }


@pytest.mark.asyncio
async def test_check_crash_does_not_change_job_status(tmp_path, monkeypatch):
    _register_python_agent(tmp_path, monkeypatch, "print('ok')")

    def crashing_run_checks(cwd, checks, timeout_ms=600_000):
        raise RuntimeError("Crash inside check runner")

    monkeypatch.setattr(
        "mindsync.dispatch.runner.run_checks", crashing_run_checks
    )

    res = await run_task(
        agent="pyagent", prompt="hi", checks=[f'"{sys.executable}" -c "exit(0)"']
    )
    job = res["job"]
    assert job["status"] == "done"
    assert len(job["checkResults"]) == 1
    assert job["checkResults"][0]["passed"] is False


@pytest.mark.asyncio
async def test_checks_run_before_worktree_cleanup(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    (repo_dir / "init.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    _register_python_agent(
        tmp_path,
        monkeypatch,
        "import pathlib; pathlib.Path('agent_created.txt').write_text('hello')",
    )

    check_cmd = f'"{sys.executable}" -c "import pathlib, sys; sys.exit(0 if pathlib.Path(\'agent_created.txt\').exists() else 1)"'

    res = await run_task(
        agent="pyagent",
        prompt="do work",
        cwd=str(repo_dir),
        worktree=True,
        checks=[check_cmd],
        write=True,
    )
    job = res["job"]
    assert job["status"] == "done"
    assert len(job["checkResults"]) == 1
    assert job["checkResults"][0]["passed"] is True


@pytest.mark.asyncio
async def test_job_with_no_checks_behaves_as_before(tmp_path, monkeypatch):
    _register_python_agent(tmp_path, monkeypatch, "print('no checks')")
    res = await run_task(agent="pyagent", prompt="task without checks")
    job = res["job"]
    assert job["status"] == "done"
    assert job.get("checks") == []
    assert job.get("checkResults") == []


def test_format_review_rendering():
    meta = {
        "id": "20260101-123456",
        "agent": "codex",
        "status": "done",
        "exitCode": 0,
        "write": True,
        "checkResults": [
            CheckResult(
                name="pytest -q",
                passed=False,
                exitCode=1,
                output="1 failed",
                durationMs=1500,
            )
        ],
        "diff": {
            "filesChanged": 1,
            "insertions": 10,
            "deletions": 2,
            "files": ["test.py"],
            "commits": 1,
        },
    }
    rendered = format_review(meta)
    assert "Job 20260101-123456" in rendered
    assert "VERDICT: FAIL" in rendered
    assert "[FAIL] pytest -q" in rendered
    assert "diff vs base: 1 file, +10 -2, 1 commit" in rendered
