"""Tests for mindsync dispatch worktree isolation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mindsync.dispatch import store
from mindsync.dispatch.runner import run_task
from mindsync.dispatch.worktree import (
    WorktreeError,
    create_worktree,
    has_changes,
    remove_worktree,
    repo_root,
)
from tests.test_dispatch import _isolate_dispatch


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _git_available(), reason="git is not on PATH")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    
    (repo / "file.txt").write_text("initial")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
    
    return repo


def test_repo_root(fake_repo: Path):
    assert Path(repo_root(str(fake_repo))).resolve() == fake_repo.resolve()
    
    subdir = fake_repo / "subdir"
    subdir.mkdir()
    assert Path(repo_root(str(subdir))).resolve() == fake_repo.resolve()


def test_repo_root_error(tmp_path: Path):
    with pytest.raises(WorktreeError, match="not inside a git repo"):
        repo_root(str(tmp_path))


def test_create_worktree(fake_repo: Path):
    job_id = "test-job-123"
    meta = create_worktree(str(fake_repo), job_id)
    
    wt_path = Path(meta["path"])
    assert wt_path.parent.name == ".mindsync-wt"
    assert wt_path.parent.parent.resolve() == fake_repo.parent.resolve()
    assert wt_path.name == job_id
    assert meta["branch"] == f"agent/{job_id}"
    assert "baseCommit" in meta
    
    # Check it exists and is a git repo
    assert wt_path.exists()
    assert (wt_path / "file.txt").read_text() == "initial"
    
    # Clean up
    remove_worktree(str(fake_repo), str(wt_path), meta["branch"])


def test_has_changes_clean(fake_repo: Path):
    job_id = "job-clean"
    meta = create_worktree(str(fake_repo), job_id)
    assert has_changes(meta["path"], meta["baseCommit"]) is False
    remove_worktree(str(fake_repo), meta["path"], meta["branch"])


def test_has_changes_untracked(fake_repo: Path):
    job_id = "job-untracked"
    meta = create_worktree(str(fake_repo), job_id)
    (Path(meta["path"]) / "new.txt").write_text("hello")
    assert has_changes(meta["path"], meta["baseCommit"]) is True
    remove_worktree(str(fake_repo), meta["path"], meta["branch"])


def test_has_changes_commit(fake_repo: Path):
    job_id = "job-commit"
    meta = create_worktree(str(fake_repo), job_id)
    wt = Path(meta["path"])
    (wt / "file.txt").write_text("modified")
    subprocess.run(["git", "-C", str(wt), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "mod"], check=True)
    assert has_changes(meta["path"], meta["baseCommit"]) is True
    remove_worktree(str(fake_repo), meta["path"], meta["branch"])


@pytest.mark.asyncio
async def test_clean_worktree_removed_after_job(fake_repo: Path, tmp_path: Path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    
    # Register an agent that does nothing and exits successfully
    from tests.test_dispatch import user_config_path
    import json
    import sys
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{"name": "noop", "bin": sys.executable, "input": "stdin", "runArgs": ["-c", "pass"]}]
    }), encoding="utf-8")

    res = await run_task(
        agent="noop", prompt="x", background=False, publisher_agent="t",
        cwd=str(fake_repo), worktree=True
    )
    job = res["job"]
    assert job["status"] == "done"
    assert job.get("worktreeKept") is False
    assert not Path(job["worktreePath"]).exists()


@pytest.mark.asyncio
async def test_dirty_worktree_survives_after_job(fake_repo: Path, tmp_path: Path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    
    # Register an agent that creates a file
    import json
    import sys
    from tests.test_dispatch import user_config_path
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{"name": "touch", "bin": sys.executable, "input": "stdin", "runArgs": ["-c", "open('dirty.txt', 'w').close()"]}]
    }), encoding="utf-8")

    res = await run_task(
        agent="touch", prompt="x", background=False, publisher_agent="t",
        cwd=str(fake_repo), worktree=True
    )
    job = res["job"]
    assert job["status"] == "done"
    assert job.get("worktreeKept") is True
    assert Path(job["worktreePath"]).exists()
    assert (Path(job["worktreePath"]) / "dirty.txt").exists()


def test_has_changes_without_base_commit_keeps(fake_repo: Path):
    """A missing base commit must never read as "no work" — that deletes the worktree."""
    meta = create_worktree(str(fake_repo), "job-nobase")
    assert has_changes(meta["path"], None) is True
    remove_worktree(str(fake_repo), meta["path"], meta["branch"])


def test_has_changes_on_unresolvable_base_keeps(fake_repo: Path):
    """git failing to answer is not the same as git answering "nothing"."""
    meta = create_worktree(str(fake_repo), "job-badbase")
    assert has_changes(meta["path"], "deadbeef" * 5) is True
    remove_worktree(str(fake_repo), meta["path"], meta["branch"])


def test_has_changes_missing_path(tmp_path: Path):
    assert has_changes(str(tmp_path / "never-created"), "abc123") is False


@pytest.mark.asyncio
async def test_committed_work_survives_after_job(fake_repo: Path, tmp_path: Path, monkeypatch):
    """The realistic case: the agent commits its work, leaving a clean tree behind."""
    _isolate_dispatch(tmp_path, monkeypatch)

    import json
    import sys

    from tests.test_dispatch import user_config_path

    script = (
        "import subprocess;"
        "open('feature.txt', 'w').write('agent work');"
        "subprocess.run(['git', 'add', '-A'], check=True);"
        "subprocess.run(['git', 'commit', '-m', 'agent work'], check=True)"
    )
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "committer",
            "bin": sys.executable,
            "input": "stdin",
            "runArgs": ["-c", script],
        }]
    }), encoding="utf-8")

    res = await run_task(
        agent="committer", prompt="x", background=False, publisher_agent="t",
        cwd=str(fake_repo), worktree=True
    )
    job = res["job"]
    assert job["status"] == "done"
    assert job.get("worktreeKept") is True
    assert Path(job["worktreePath"]).exists()
    assert (Path(job["worktreePath"]) / "feature.txt").exists()

    branches = subprocess.run(
        ["git", "-C", str(fake_repo), "branch", "--list", job["branch"]],
        capture_output=True, text=True,
    ).stdout
    assert job["branch"] in branches


@pytest.mark.asyncio
async def test_non_git_cwd_raises(tmp_path: Path):
    with pytest.raises(WorktreeError, match="not inside a git repo"):
        await run_task(
            agent="fake", prompt="x", background=False, publisher_agent="t",
            cwd=str(tmp_path), worktree=True
        )


@pytest.mark.asyncio
async def test_background_worktree_supervisor_cwd(fake_repo: Path, tmp_path: Path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    
    # We need to mock spawn_background to verify its cwd argument
    import mindsync.dispatch.runner as runner
    
    spawned_cwd = None
    
    def fake_spawn_background(py, args, cwd=None, stdout_path=None, stderr_path=None, env=None):
        nonlocal spawned_cwd
        spawned_cwd = cwd
        # Just pretend it spawned pid 999999
        return {"pid": 999999, "spawnedName": "python"}
        
    monkeypatch.setattr(runner, "spawn_background", fake_spawn_background)
    
    # Mock resolve_bin so it doesn't fail
    monkeypatch.setattr(runner, "resolve_bin", lambda x: "/fake/bin")
    
    # We need to register the agent
    import json
    from tests.test_dispatch import user_config_path
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{"name": "bg", "bin": "b", "input": "stdin", "runArgs": []}]
    }), encoding="utf-8")
    
    res = await run_task(
        agent="bg", prompt="x", background=True, publisher_agent="t",
        cwd=str(fake_repo), worktree=True
    )
    
    assert Path(spawned_cwd).resolve() == fake_repo.resolve()
    
    job_id = res["job"]["id"]
    meta = store.get_job(job_id)
    assert meta["worktreePath"] is not None
    assert Path(meta["cwd"]).resolve() == Path(meta["worktreePath"]).resolve()

