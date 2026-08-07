"""Unit tests for MindSync remote queue and worker claim logic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

import mindsync.config as config_mod
import mindsync.remote_queue as remote_queue
import mindsync.storage as storage
from mindsync.dispatch.adapters import user_config_path
from mindsync.remote_queue import (
    RemoteQueue,
    run_worker_once,
    validate_repo_path,
)


@pytest.fixture
def local_remote_root(tmp_path):
    root = tmp_path / "remote_root"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


@pytest.fixture
def local_repo(tmp_path):
    repo = tmp_path / "test_repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    dummy_file = repo / "README.md"
    dummy_file.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    return str(repo)


def _configure_fake_dispatch(local_repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    base = Path(local_repo).parent
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(base / "dispatch-home"))
    monkeypatch.setenv("MINDSYNC_HOME", str(base / "mindsync-home"))
    configured = config_mod.Settings()
    monkeypatch.setattr(config_mod, "settings", configured)
    monkeypatch.setattr(storage, "settings", configured)
    configured.ensure_dirs()
    config_path = user_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {"agents": [{"name": "fake", "bin": sys.executable, "input": "stdin", "runArgs": []}]}
        ),
        encoding="utf-8",
    )


def _git_output(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_validate_repo_path(tmp_path):
    allowed1 = tmp_path / "allowed1"
    allowed2 = tmp_path / "allowed2"
    allowed1.mkdir()
    allowed2.mkdir()
    sub1 = allowed1 / "subfolder"
    sub1.mkdir()
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()

    allowed_list = [str(allowed1), str(allowed2)]

    assert validate_repo_path(str(allowed1), allowed_list) is True
    assert validate_repo_path(str(sub1), allowed_list) is True
    assert validate_repo_path(str(forbidden), allowed_list) is False
    assert validate_repo_path("", allowed_list) is False
    assert validate_repo_path(str(allowed1), []) is False


def test_claim_atomicity(local_remote_root):
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    queue.ensure_remote_dirs()

    job_id = queue.submit_job(
        repo_path="/some/repo",
        prompt="Test prompt",
        agent="auto",
    )

    barrier = threading.Barrier(2)

    def claim(worker_id):
        barrier.wait()
        return queue.claim_job(job_id, worker_id=worker_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim1, claim2 = list(pool.map(claim, ("worker-1", "worker-2")))

    # Exactly one worker wins
    assert (claim1 is not None and claim2 is None) or (claim1 is None and claim2 is not None)
    winning_claim = claim1 or claim2
    winning_worker = "worker-1" if claim1 is not None else "worker-2"

    assert winning_claim["worker_id"] == winning_worker
    assert "claimed_at" in winning_claim

    # Verify remote directory state
    pending_file = Path(local_remote_root) / "queue" / "pending" / f"{job_id}.json"
    claimed_file = Path(local_remote_root) / "queue" / "claimed" / f"{job_id}.json"

    assert not pending_file.exists()
    assert claimed_file.exists()

    # Check 0600 file permissions on POSIX
    if os.name == "posix":
        mode = claimed_file.stat().st_mode & 0o777
        assert mode == 0o600


def test_stale_claim_requeue(local_remote_root):
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    queue.ensure_remote_dirs()

    job_id = queue.submit_job(
        repo_path="/some/repo",
        prompt="Stale test prompt",
    )

    # Worker 1 claims job
    claimed_data = queue.claim_job(job_id, worker_id="worker-dead")
    assert claimed_data is not None

    # Simulate a worker dying after the atomic move but before claim metadata was written.
    claimed_file = Path(local_remote_root) / "queue" / "claimed" / f"{job_id}.json"
    claimed_data.pop("claimed_at")
    claimed_data.pop("worker_id")
    claimed_file.write_text(json.dumps(claimed_data, indent=2), encoding="utf-8")
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=600)).timestamp()
    os.utime(claimed_file, (stale_time, stale_time))

    # Requeue stale claims (stale window = 300s)
    requeued = queue.requeue_stale_claims(stale_seconds=300)
    assert requeued == 1

    # Verify job moved back to pending
    pending_file = Path(local_remote_root) / "queue" / "pending" / f"{job_id}.json"
    assert pending_file.exists()
    assert not claimed_file.exists()

    pending_data = json.loads(pending_file.read_text(encoding="utf-8"))
    assert "claimed_at" not in pending_data
    assert "worker_id" not in pending_data


def test_allow_list_rejection(local_remote_root, tmp_path):
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    queue.ensure_remote_dirs()

    allowed_dir = tmp_path / "allowed_repo"
    allowed_dir.mkdir()
    forbidden_dir = tmp_path / "forbidden_repo"
    forbidden_dir.mkdir()

    job_id = queue.submit_job(
        repo_path=str(forbidden_dir),
        prompt="Execute prohibited path",
    )

    res = run_worker_once(
        queue,
        worker_id="worker-strict",
        allowed_repos=[str(allowed_dir)],
    )

    assert res is not None
    assert res["job_id"] == job_id
    assert res["status"] == "failed"
    assert "allow-list" in res["error"]

    status_info = queue.get_status(job_id)
    assert status_info is not None
    assert status_info["state"] == "failed"
    assert "allow-list" in status_info["job"]["result"]
    assert str(forbidden_dir) not in status_info["job"]["result"]


def test_requested_branch_must_match_worker_checkout(local_remote_root, local_repo):
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    job_id = queue.submit_job(
        repo_path=local_repo,
        prompt="Do not run on the wrong branch",
        branch="definitely-not-current",
    )

    result = run_worker_once(queue, "worker-branch", [local_repo])

    assert result["status"] == "failed"
    assert "Branch mismatch" in result["error"]
    assert queue.get_status(job_id)["state"] == "failed"


@pytest.mark.parametrize("timeout_seconds", [0, 3600.01, float("inf"), float("nan")])
def test_submit_rejects_timeout_outside_bounds(
    local_remote_root, local_repo, timeout_seconds
):
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    with pytest.raises(
        ValueError, match="timeout_seconds must be greater than 0 and at most 3600"
    ):
        queue.submit_job(
            repo_path=local_repo,
            prompt="bounded",
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize("timeout_seconds", [0.001, 3600])
def test_submit_accepts_timeout_at_valid_bounds(local_remote_root, local_repo, timeout_seconds):
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    job_id = queue.submit_job(
        repo_path=local_repo,
        prompt="bounded",
        timeout_seconds=timeout_seconds,
    )
    assert queue.get_status(job_id)["job"]["timeout_seconds"] == timeout_seconds


def test_timeout_reaches_agent_process(local_remote_root, local_repo, monkeypatch):
    _configure_fake_dispatch(local_repo, monkeypatch)
    seen_timeout_ms = []

    async def fake_spawn(*args, **kwargs):
        seen_timeout_ms.append(kwargs["timeout_ms"])
        return {"stdout": "ok", "stderr": "", "exitCode": 0, "timedOut": False}

    import mindsync.dispatch.runner as runner

    monkeypatch.setattr(runner, "spawn_foreground", fake_spawn)
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    queue.submit_job(
        repo_path=local_repo,
        prompt="use custom timeout",
        agent="fake",
        timeout_seconds=123.5,
    )

    result = run_worker_once(queue, "worker-timeout", [local_repo])

    assert result and result["status"] == "done"
    assert seen_timeout_ms == [123500]


def test_commit_absent_leaves_git_history_and_index_untouched(
    local_remote_root, local_repo, monkeypatch
):
    _configure_fake_dispatch(local_repo, monkeypatch)

    async def fake_spawn(*args, **kwargs):
        (Path(kwargs["cwd"]) / "agent.txt").write_text("agent change\n", encoding="utf-8")
        return {"stdout": "ok", "stderr": "", "exitCode": 0, "timedOut": False}

    import mindsync.dispatch.runner as runner

    monkeypatch.setattr(runner, "spawn_foreground", fake_spawn)
    monkeypatch.setattr(
        remote_queue,
        "_commit_git_handoff",
        lambda *args: pytest.fail("commit handoff ran without --commit"),
    )
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    queue.submit_job(repo_path=local_repo, prompt="edit only", agent="fake")

    result = run_worker_once(queue, "worker-no-commit", [local_repo])

    assert result and result["status"] == "done"
    assert _git_output(local_repo, "rev-list", "--count", "HEAD") == "1"
    assert subprocess.run(
        ["git", "-C", local_repo, "diff", "--cached", "--quiet"], check=False
    ).returncode == 0


def test_commit_handoff_creates_branch_and_records_sha(
    local_remote_root, local_repo, monkeypatch
):
    _configure_fake_dispatch(local_repo, monkeypatch)

    async def fake_spawn(*args, **kwargs):
        (Path(kwargs["cwd"]) / "agent.txt").write_text("agent change\n", encoding="utf-8")
        return {"stdout": "ok", "stderr": "", "exitCode": 0, "timedOut": False}

    import mindsync.dispatch.runner as runner

    monkeypatch.setattr(runner, "spawn_foreground", fake_spawn)
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    job_id = queue.submit_job(
        repo_path=local_repo,
        prompt="edit and commit",
        agent="fake",
        branch="feat/worker-handoff",
        commit=True,
    )

    result = run_worker_once(queue, "worker-commit", [local_repo])
    status = queue.get_status(job_id)

    assert result and result["status"] == "done"
    assert _git_output(local_repo, "rev-list", "--count", "HEAD") == "2"
    assert _git_output(local_repo, "branch", "--show-current") == "feat/worker-handoff"
    assert status["job"]["commit_sha"] == _git_output(local_repo, "rev-parse", "HEAD")
    assert status["job"]["branch"] == "feat/worker-handoff"


@pytest.mark.parametrize(
    ("exit_code", "timed_out"),
    [(1, False), (-1, True)],
)
def test_failed_or_timed_out_agent_is_never_committed(
    local_remote_root, local_repo, monkeypatch, exit_code, timed_out
):
    _configure_fake_dispatch(local_repo, monkeypatch)

    async def fake_spawn(*args, **kwargs):
        (Path(kwargs["cwd"]) / "partial.txt").write_text("partial\n", encoding="utf-8")
        return {
            "stdout": "partial",
            "stderr": "failed",
            "exitCode": exit_code,
            "timedOut": timed_out,
        }

    import mindsync.dispatch.runner as runner

    monkeypatch.setattr(runner, "spawn_foreground", fake_spawn)
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    job_id = queue.submit_job(
        repo_path=local_repo,
        prompt="do not commit failure",
        agent="fake",
        commit=True,
    )

    result = run_worker_once(queue, "worker-failure", [local_repo])
    status = queue.get_status(job_id)

    assert result and result["status"] == "failed"
    assert _git_output(local_repo, "rev-list", "--count", "HEAD") == "1"
    assert "commit_sha" not in status["job"]


def test_commit_handoff_refuses_dirty_tree_before_agent_run(
    local_remote_root, local_repo, monkeypatch
):
    _configure_fake_dispatch(local_repo, monkeypatch)
    (Path(local_repo) / "leftover.txt").write_text("previous run\n", encoding="utf-8")
    agent_ran = False

    async def fake_spawn(*args, **kwargs):
        nonlocal agent_ran
        agent_ran = True
        return {"stdout": "", "stderr": "", "exitCode": 0, "timedOut": False}

    import mindsync.dispatch.runner as runner

    monkeypatch.setattr(runner, "spawn_foreground", fake_spawn)
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    job_id = queue.submit_job(
        repo_path=local_repo,
        prompt="refuse leftovers",
        agent="fake",
        branch="feat/unsafe",
        commit=True,
    )

    result = run_worker_once(queue, "worker-dirty", [local_repo])
    status = queue.get_status(job_id)

    assert result and result["status"] == "failed"
    assert "working tree must be clean" in status["job"]["result"]
    assert agent_ran is False
    assert _git_output(local_repo, "branch", "--show-current") != "feat/unsafe"
    assert _git_output(local_repo, "rev-list", "--count", "HEAD") == "1"


def test_ssh_transport_quotes_configured_root_and_keeps_files_private(monkeypatch):
    import mindsync.remote_queue as remote_queue

    scripts = []

    def fake_ssh(script, *, timeout):
        scripts.append(script)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(remote_queue.settings, "ssh_host", "vps-alias")
    monkeypatch.setattr(remote_queue, "_ssh_script", fake_ssh)
    queue = RemoteQueue(remote_root="/srv/mind sync", ssh_host="vps-alias")

    queue.ensure_remote_dirs()
    queue.submit_job(repo_path="C:/work/repo", prompt="safe payload")

    combined = "\n".join(scripts)
    assert "'/srv/mind sync/queue/pending'" in combined
    assert "chmod 600" in combined
    assert "TMP_FILE" in combined


def test_full_round_trip(local_remote_root, local_repo, monkeypatch):
    dispatch_home = Path(local_repo).parent / "dispatch-home"
    mindsync_home = Path(local_repo).parent / "mindsync-home"
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(dispatch_home))
    monkeypatch.setenv("MINDSYNC_HOME", str(mindsync_home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    config_path = user_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "fake", "bin": sys.executable, "input": "stdin", "runArgs": []}
                ]
            }
        ),
        encoding="utf-8",
    )

    import mindsync.dispatch.runner as runner

    async def sandbox_enabled_spawn(*args, **kwargs):
        return {
            "stdout": "sandbox-enabled-ok",
            "stderr": "",
            "exitCode": 0,
            "timedOut": False,
        }

    monkeypatch.setattr(runner, "spawn_foreground", sandbox_enabled_spawn)
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    queue.ensure_remote_dirs()

    job_id = queue.submit_job(
        repo_path=local_repo,
        prompt="Check repository state",
        agent="fake",
    )

    status_initial = queue.get_status(job_id)
    assert status_initial["state"] == "pending"

    res = run_worker_once(
        queue,
        worker_id="worker-local",
        allowed_repos=[local_repo],
    )

    assert res is not None
    assert res["job_id"] == job_id
    assert res["status"] == "done"
    assert "sandbox-enabled-ok" in res["result"]

    status_final = queue.get_status(job_id)
    assert status_final["state"] == "done"
    assert status_final["job"]["worker_id"] == "worker-local"
    assert status_final["job"]["stdout"] == "sandbox-enabled-ok"

    local_jobs = list((dispatch_home / "jobs").glob("*/meta.json"))
    assert len(local_jobs) == 1
    local_meta = json.loads(local_jobs[0].read_text(encoding="utf-8"))
    assert local_meta["cwd"] == local_repo
    assert local_meta["status"] == "done"

    done_file = Path(local_remote_root) / "queue" / "done" / f"{job_id}.json"
    assert done_file.exists()
    if os.name == "posix":
        assert (done_file.stat().st_mode & 0o777) == 0o600
