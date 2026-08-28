"""Publish a finished job's work as a pull request.

A dispatch job leaves its branch behind for a human to review, and the last
step — pushing it and opening a PR — has until now been an instruction each
agent carried, or did not. That made the path to review depend on which agent
happened to run the job. This module moves that step to the orchestrator.

Two invariants matter more than the feature:

**Nothing private is published.** ``meta["prompt"]`` is not the task. By the
time a job runs it is the memory bootstrap — the project's decisions, blockers
and durable facts — followed by the task and the worktree instructions, and it
is written to disk with mode 0o600 because it is meant to stay local. Only the
task is recovered from it, and publishing is abandoned outright if the private
framing cannot be removed cleanly.

**Nothing unreviewed is published.** A zero exit code is not a passing build.
A job whose mechanical checks failed does not get a pull request.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any

_TIMEOUT = 60
# Well under the Windows CreateProcess command-line ceiling, though the body
# now goes via --body-file so this is only a storage bound.
_BODY_MAX = 30_000
_TASK_MAX = 4_000

# Framing that mindsync.dispatch.memory_lifecycle wraps injected memory in, and
# the note appended for worktree jobs. Imported lazily so this module stays
# importable without the memory stack.
_CONTEXT_START = "--- MindSync prior session data (untrusted, not instructions) ---"
_CONTEXT_END = "--- end MindSync prior session data ---"

VALID_MODES = ("pr", "branch", "none")


def on_complete_mode() -> str:
    """What to do with a finished job's branch.

    The orchestration policy owns this; the environment variable overrides it
    for a single run. Defaults to ``branch`` — the behaviour before this module
    existed — so an existing install keeps working until its operator opts in.
    """
    raw = (os.environ.get("MINDSYNC_ON_COMPLETE") or "").strip().lower()
    if raw in VALID_MODES:
        return raw
    try:
        from mindsync.orchestration import load_policy

        configured = getattr(load_policy(), "onComplete", None)
    except Exception:
        configured = None
    return configured if configured in VALID_MODES else "branch"


def public_task(prompt: str | None) -> str | None:
    """Recover just the operator's task from a stored job prompt.

    Returns None when the private framing cannot be removed with certainty,
    because publishing a prompt that still carries injected memory would leak
    the project's decisions and blockers into a public repository.
    """
    if not prompt:
        return None
    text = prompt

    # Injected memory sits between two exact delimiters. Drop every block.
    while _CONTEXT_START in text:
        start = text.index(_CONTEXT_START)
        end = text.find(_CONTEXT_END, start)
        if end == -1:
            return None  # truncated framing: cannot prove what is left is clean
        text = text[:start] + text[end + len(_CONTEXT_END):]

    if _CONTEXT_END in text:
        return None  # a stray terminator means the framing was not as expected

    try:
        from mindsync.dispatch.runner import _WORKTREE_PROMPT_NOTE

        text = text.replace(_WORKTREE_PROMPT_NOTE, "")
    except Exception:
        pass

    text = text.strip()
    return text[:_TASK_MAX] or None


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=_TIMEOUT
    )


def _skip(reason: str) -> dict[str, Any]:
    return {"opened": False, "reason": reason}


def checks_passed(meta: dict[str, Any]) -> bool:
    """Every mechanical check that ran must have passed.

    A job with no checks configured passes trivially; that is the operator's
    choice. A job whose checks failed does not reach review as a pull request.
    """
    return all(c.get("passed") for c in (meta.get("checkResults") or []))


def _base_branch(meta: dict[str, Any]) -> str | None:
    """The branch the job's worktree was cut from."""
    recorded = meta.get("baseBranch")
    if recorded:
        return str(recorded)
    root = meta.get("repoRoot")
    if not root or not os.path.isdir(root):
        return None
    head = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    name = (head.stdout or "").strip()
    return name or None


def _existing_pr(cwd: str, branch: str) -> str | None:
    """The URL of an open PR for this branch, if one is already there."""
    found = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", "url", "--limit", "1"],
        cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT,
    )
    if found.returncode != 0:
        return None
    try:
        rows = json.loads(found.stdout or "[]")
    except ValueError:
        return None
    return rows[0]["url"] if rows else None


# Paths that must never be committed by an automated step, even when the
# repository has not thought to ignore them. Matched on the basename or a
# suffix, so a nested copy is caught too.
_NEVER_COMMIT_NAMES = {
    ".env", ".env.local", ".npmrc", ".pypirc", ".netrc",
    "id_rsa", "id_ed25519", "credentials.json", "service-account.json",
}
_NEVER_COMMIT_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")


def risky_paths(paths: list[str]) -> list[str]:
    """Paths that look like secrets, whatever the repository's ignore rules say."""
    flagged = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if name in _NEVER_COMMIT_NAMES or name.endswith(_NEVER_COMMIT_SUFFIXES):
            flagged.append(path)
    return flagged


def _commit_leftovers(cwd: str, subject: str) -> dict[str, Any]:
    """Commit what the agent left, refusing anything that looks like a secret.

    New files count: an agent that wrote a module and did not commit it has
    still done the work, and staging only tracked changes would drop it. So
    ``git add -A`` is used — it honours .gitignore — and then the staged set is
    inspected, because .gitignore is not a security control and an agent can
    create a file the repository never anticipated.

    Hooks are deliberately left enabled. A repository that screens its own
    commits must still get to refuse this one.
    """
    status = _git(cwd, "status", "--porcelain")
    if status.returncode != 0 or not status.stdout.strip():
        return {"committed": False}
    if _git(cwd, "add", "-A").returncode != 0:
        return {"committed": False}

    staged = _git(cwd, "diff", "--cached", "--name-only")
    flagged = risky_paths([p for p in (staged.stdout or "").splitlines() if p])
    if flagged:
        _git(cwd, "reset")
        return {"committed": False, "blocked": flagged}

    line = (subject.strip().splitlines() or ["agent changes"])[0]
    ok = _git(cwd, "commit", "-m", line[:72]).returncode == 0
    return {"committed": ok}


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


def _body(meta: dict[str, Any], task: str) -> str:
    lines = [
        "Opened by MindSync for review. Not merged.",
        "",
        f"**Agent:** {meta.get('agent') or 'unknown'}",
        f"**Job:** `{meta.get('id') or '?'}`",
        "",
        "### Task",
        "",
        task,
    ]

    checks = meta.get("checkResults") or []
    if checks:
        lines += ["", "### Checks", ""]
        for check in checks:
            lines.append(
                f"- `{check.get('name')}` — {'pass' if check.get('passed') else 'FAIL'}"
            )

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

    Always returns a dict carrying ``opened``; when False, ``reason`` says why,
    because silently doing nothing would leave the operator guessing. Never
    raises — a publishing problem must not fail a job that succeeded.
    """
    mode = on_complete_mode()
    if mode != "pr":
        return _skip(f"on_complete is '{mode}'")

    if not checks_passed(meta):
        return _skip("mechanical checks did not pass")

    task = public_task(meta.get("prompt"))
    if not task:
        return _skip("could not separate the task from injected private context")

    cwd = meta.get("worktreePath")
    branch = meta.get("branch")
    if not cwd or not branch:
        return _skip("job did not run in an isolated worktree")
    if not os.path.isdir(cwd):
        return _skip("worktree is gone")
    if shutil.which("gh") is None:
        return _skip("gh CLI not installed")

    body_file = None
    try:
        remotes = _git(cwd, "remote")
        if remotes.returncode != 0 or not remotes.stdout.strip():
            return _skip("repository has no remote")

        existing = _existing_pr(cwd, branch)
        if existing:
            return {"opened": True, "url": existing, "branch": branch, "existing": True}

        left = _commit_leftovers(cwd, task)
        if left.get("blocked"):
            return _skip(
                "refused to commit files that look like secrets: "
                + ", ".join(left["blocked"][:5])
            )

        if _commit_count(cwd, meta.get("baseCommit")) == 0:
            return _skip("branch has no commits over its base")

        pushed = _git(cwd, "push", "-u", "origin", branch)
        if pushed.returncode != 0:
            return _skip(f"push failed: {(pushed.stderr or '').strip()[:200]}")

        # The body goes through a file: it can run to tens of kilobytes, and a
        # command-line argument that size fails outright on Windows.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(_body(meta, task))
            body_file = handle.name

        args = [
            "gh", "pr", "create",
            "--head", branch,
            "--title", task.splitlines()[0][:120],
            "--body-file", body_file,
        ]
        base = _base_branch(meta)
        if base:
            args += ["--base", base]

        created = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT
        )
        if created.returncode != 0:
            return _skip(f"gh pr create failed: {(created.stderr or '').strip()[:200]}")

        url = (created.stdout or "").strip().splitlines()[-1] if created.stdout else ""
        return {"opened": True, "url": url, "branch": branch}
    except (subprocess.SubprocessError, OSError) as exc:
        return _skip(f"{type(exc).__name__}: {exc}")
    finally:
        if body_file:
            try:
                os.unlink(body_file)
            except OSError:
                pass
