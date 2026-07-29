from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


def repo_root(path: str) -> str:
    """Find the root of the git repository containing the given path."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        raise WorktreeError(f"Path '{path}' is not inside a git repo")


def create_worktree(root: str, job_id: str) -> dict:
    """
    Create a git worktree for a specific job.

    The worktree is placed in a sibling directory of the repository root,
    so that the repository's own status/tooling never sees it.
    """
    repo = Path(root).resolve()
    wt_path = repo.parent / ".mindsync-wt" / job_id
    branch = f"agent/{job_id}"

    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        base_commit = result.stdout.strip()
    except subprocess.CalledProcessError:
        raise WorktreeError(f"Could not resolve HEAD in '{root}'")

    # Create the worktree
    try:
        subprocess.run(
            ["git", "-C", root, "worktree", "add", str(wt_path), "-b", branch],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise WorktreeError(f"Failed to create worktree: {e.stderr}")

    return {
        "path": str(wt_path),
        "branch": branch,
        "baseCommit": base_commit,
    }


def has_changes(path: str, base_commit: str) -> bool:
    """Check if the worktree has any uncommitted or committed changes."""
    # Check for uncommitted/untracked files
    res_status = subprocess.run(
        ["git", "-C", path, "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_status.stdout.strip():
        return True

    # Check for commits
    res_revs = subprocess.run(
        ["git", "-C", path, "rev-list", f"{base_commit}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_revs.stdout.strip():
        return True

    return False


def remove_worktree(root: str, path: str, branch: str) -> bool:
    """
    Remove a worktree and its branch.

    This must never raise an exception. It returns True if successful,
    or False if any step fails.
    """
    try:
        subprocess.run(
            ["git", "-C", root, "worktree", "remove", "--force", path],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", root, "branch", "-D", branch],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", root, "worktree", "prune"],
            capture_output=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
