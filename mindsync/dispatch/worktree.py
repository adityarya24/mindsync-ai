"""Per-job git worktree isolation, so parallel agents never share a working tree."""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


def _git(cwd: str, *args: str) -> str | None:
    """Run a git command, returning stdout, or None when it could not run.

    Callers must distinguish "git said no" from "git said nothing" — an empty
    string is a real answer, None is a failure.
    """
    try:
        res = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return res.stdout if res.returncode == 0 else None


def repo_root(path: str) -> str:
    """Find the root of the git repository containing the given path."""
    out = _git(path, "rev-parse", "--show-toplevel")
    if out is None:
        raise WorktreeError(
            f"Path '{path}' is not inside a git repo (or git is not available)"
        )
    return out.strip()


def head_commit(path: str) -> str | None:
    """Resolve HEAD for a directory, or None when it is not a usable git repo."""
    out = _git(path, "rev-parse", "HEAD")
    return out.strip() if out else None


def create_worktree(root: str, job_id: str) -> dict[str, str]:
    """Create a git worktree for a specific job.

    The worktree is placed in a sibling directory of the repository root, so the
    repository's own status and tooling never see it.
    """
    repo = Path(root).resolve()
    wt_path = repo.parent / ".mindsync-wt" / job_id
    branch = f"agent/{job_id}"

    base_commit = _git(root, "rev-parse", "HEAD")
    if base_commit is None:
        raise WorktreeError(f"Could not resolve HEAD in '{root}'")

    try:
        subprocess.run(
            ["git", "-C", root, "worktree", "add", str(wt_path), "-b", branch],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise WorktreeError(f"Failed to create worktree: {detail}") from exc

    return {
        "path": str(wt_path),
        "branch": branch,
        "baseCommit": base_commit.strip(),
    }


def has_changes(path: str, base_commit: str | None) -> bool:
    """Report whether the agent left any work behind in the worktree.

    Counts both uncommitted files and commits made on the job's branch.

    Fails safe towards keeping: the caller deletes the worktree and force-deletes
    its branch when this returns False, so a wrong False destroys the agent's
    output while a wrong True only leaves a directory for a human to look at.
    Anything we cannot determine therefore counts as "has work".
    """
    if not Path(path).is_dir():
        # Already gone — there is nothing left to preserve.
        return False
    if not base_commit:
        return True

    status = _git(path, "status", "--porcelain")
    if status is None or status.strip():
        return True

    revs = _git(path, "rev-list", f"{base_commit}..HEAD")
    return revs is None or bool(revs.strip())


def remove_worktree(root: str, path: str, branch: str) -> bool:
    """Remove a worktree and its branch. Never raises.

    Success is judged by the filesystem, not by exit codes: whether the branch
    delete or the prune worked is no reason to tell the caller the agent's
    directory is still sitting there.
    """
    _git(root, "worktree", "remove", "--force", path)
    removed = not Path(path).exists()
    if removed:
        _git(root, "branch", "-D", branch)
        _git(root, "worktree", "prune")
    return removed
