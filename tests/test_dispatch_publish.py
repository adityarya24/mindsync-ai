"""Tests for publishing a finished job as a pull request.

Every test drives a real git repository in a tmp dir and fakes only the two
things that would reach the network: the remote, and `gh`.
"""
from __future__ import annotations

import subprocess

import pytest

from mindsync.dispatch import publish


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture()
def repo(tmp_path):
    """A repo with one base commit and a job branch checked out."""
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "seed.txt").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "checkout", "-b", "agent/job-1")
    return {"path": str(root), "base": base}


def _meta(repo, **over):
    meta = {
        "id": "job-1",
        "status": "done",
        "agent": "codex",
        "prompt": "Add a greeting module",
        "worktreePath": repo["path"],
        "branch": "agent/job-1",
        "baseCommit": repo["base"],
    }
    meta.update(over)
    return meta


def test_skipped_unless_the_operator_asked_for_a_pr(repo, monkeypatch):
    """Default stays the old behaviour, so existing installs do not change."""
    monkeypatch.delenv("MINDSYNC_ON_COMPLETE", raising=False)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert "branch" in outcome["reason"]


def test_reports_why_when_the_repo_has_no_remote(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert outcome["reason"] == "repository has no remote"


def test_reports_why_when_gh_is_missing(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: None)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert outcome["reason"] == "gh CLI not installed"


def test_refuses_a_branch_that_added_nothing(repo, monkeypatch):
    """No commits over base means there is nothing to review."""
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert outcome["reason"] == "branch has no commits over its base"


def test_uncommitted_work_still_becomes_a_pull_request(repo, monkeypatch):
    """An agent that edited without committing has still done the work.

    That must not be the reason no PR appears, so the leftovers are committed
    onto the job's own branch first.
    """
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")
    (tmp := __import__("pathlib").Path(repo["path"]) / "greeting.py").write_text("hi\n")
    assert tmp.exists()

    calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/o/r/pull/7\n", stderr=""
            )
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is True
    assert outcome["url"] == "https://github.com/o/r/pull/7"
    assert any(c[:2] == ["gh", "pr"] for c in calls)
    # the leftover file was committed rather than abandoned
    tracked = _git(repo["path"], "ls-tree", "-r", "HEAD", "--name-only").stdout
    assert "greeting.py" in tracked


def test_a_failed_push_is_reported_not_raised(repo, monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "pr")
    monkeypatch.setattr(publish.shutil, "which", lambda _name: "/usr/bin/gh")
    _git(repo["path"], "remote", "add", "origin", "https://example.invalid/x.git")
    (__import__("pathlib").Path(repo["path"]) / "a.txt").write_text("a\n")

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="denied")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", fake_run)

    outcome = publish.open_pull_request(_meta(repo))

    assert outcome["opened"] is False
    assert "push failed" in outcome["reason"]
    assert "denied" in outcome["reason"]


def test_body_carries_the_task_and_check_results(repo):
    body = publish._body(
        _meta(
            repo,
            checkResults=[
                {"name": "pytest", "passed": True},
                {"name": "ruff", "passed": False},
            ],
            diff={"filesChanged": 2, "insertions": 10, "deletions": 3},
        )
    )

    assert "Add a greeting module" in body
    assert "`pytest` — pass" in body
    assert "`ruff` — FAIL" in body
    assert "2 files, +10 / -3" in body
    assert "Not merged" in body


def test_an_unknown_mode_falls_back_to_the_safe_one(monkeypatch):
    monkeypatch.setenv("MINDSYNC_ON_COMPLETE", "nonsense")
    assert publish.on_complete_mode() == "branch"
