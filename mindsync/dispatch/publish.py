"""Publish a finished job's work as a pull request.

A dispatch job leaves its branch behind for a human to review, and the last
step — pushing it and opening a PR — has until now been an instruction each
agent carried, or did not. That made the path to review depend on which agent
happened to run the job.

This module moves that step to the orchestrator, so it behaves the same way
whichever agent ran the work. It never merges: the PR is the handoff to a
human, and merging stays a human decision.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

_TIMEOUT = 60
_BODY_MAX = 60_000

VALID_MODES = ("pr", "branch", "none")


def on_complete_mode() -> str:
    """What to do with a finished job's branch.

    Defaults to ``branch`` — the behaviour before this module existed — so an
    existing install keeps working until its operator opts in.
    """
    raw = (os.environ.get("MINDSYNC_ON_COMPLETE") or "branch").strip().lower()
    return raw if raw in VALID_MODES else "branch"


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )


def _skip(reason: str) -> dict[str, Any]:
    return {"opened": False, "reason": reason}


def _has_remote(cwd: str) -> bool:
    result = _git(cwd, "remote")
    return result.returncode == 0 and bool(result.stdout.strip())


def _commit_leftovers(cwd: str, task: str) -> bool:
    """Commit whatever the agent left uncommitted, on its own branch.

    An agent that edited files without committing has still done the work, and
    that work should not be the reason no PR appears. This only ever touches
    the job's own branch inside the job's own worktree.
    """
    status = _git(cwd, "status", "--porcelain")
    if status.returncode != 0 or not status.stdout.strip():
        return False
    if _git(cwd, "add", "-A").returncode != 0:
        return False
    subject = task.strip().splitlines()[0] if task.strip() else "agent changes"
    return _git(cwd, "commit", "-m", subject[:72], "--no-verify").returncode == 0


def _commit_count(cwd: str, base_commit: str | None) -> int:
    if not base_commit:
        return 0
    result = _git(cwd, "rev-list", "--count", f"{base_commit}..HEAD")
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


def _body(meta: dict[str, Any]) -> str:
    lines = [
        "Opened by MindSync for review. Not merged.",
        "",
        f"**Agent:** {meta.get('agent') or 'unknown'}",
        f"**Job:** `{meta.get('id') or meta.get('jobId') or '?'}`",
        "",
        "### Task",
        "",
        (meta.get("prompt") or "").strip() or "_(no prompt recorded)_",
    ]

    checks = meta.get("checkResults") or []
    if checks:
        lines += ["", "### Checks", ""]
        for check in checks:
            mark = "pass" if check.get("passed") else "FAIL"
            lines.append(f"- `{check.get('name')}` — {mark}")

    diff = meta.get("diff") or {}
    if diff.get("filesChanged"):
        lines += [
            "",
            "### Diff",
            "",
            f"{diff.get('filesChanged')} files, "
            f"+{diff.get('insertions', 0)} / -{diff.get('deletions', 0)}",
        ]

    return "\n".join(lines)[:_BODY_MAX]


def open_pull_request(meta: dict[str, Any]) -> dict[str, Any]:
    """Push the job branch and open a PR for it.

    Returns a dict that always carries ``opened``; when False, ``reason`` says
    why, because silently doing nothing would leave the operator guessing.
    Never raises — a publishing problem must not fail a job that succeeded.
    """
    mode = on_complete_mode()
    if mode != "pr":
        return _skip(f"on_complete is '{mode}'")

    cwd = meta.get("worktreePath")
    branch = meta.get("branch")
    if not cwd or not branch:
        return _skip("job did not run in an isolated worktree")
    if not os.path.isdir(cwd):
        return _skip("worktree is gone")
    if shutil.which("gh") is None:
        return _skip("gh CLI not installed")

    try:
        if not _has_remote(cwd):
            return _skip("repository has no remote")

        _commit_leftovers(cwd, meta.get("prompt") or "")

        if _commit_count(cwd, meta.get("baseCommit")) == 0:
            return _skip("branch has no commits over its base")

        pushed = _git(cwd, "push", "-u", "origin", branch)
        if pushed.returncode != 0:
            return _skip(f"push failed: {(pushed.stderr or '').strip()[:200]}")

        title = (meta.get("prompt") or "Agent changes").strip().splitlines()[0]
        created = subprocess.run(
            [
                "gh", "pr", "create",
                "--head", branch,
                "--title", title[:120],
                "--body", _body(meta),
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        if created.returncode != 0:
            return _skip(f"gh pr create failed: {(created.stderr or '').strip()[:200]}")

        url = (created.stdout or "").strip().splitlines()[-1] if created.stdout else ""
        return {"opened": True, "url": url, "branch": branch}
    except (subprocess.SubprocessError, OSError) as exc:
        return _skip(f"{type(exc).__name__}: {exc}")
