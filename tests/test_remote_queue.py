"""Unit tests for MindSync remote queue and worker claim logic."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

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

    # Attempt to claim concurrently/sequentially by Worker 1 and Worker 2
    claim1 = queue.claim_job(job_id, worker_id="worker-1")
    claim2 = queue.claim_job(job_id, worker_id="worker-2")

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

    # Manually set claimed_at timestamp to 10 minutes ago
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    claimed_file = Path(local_remote_root) / "queue" / "claimed" / f"{job_id}.json"
    claimed_data["claimed_at"] = stale_time
    claimed_file.write_text(json.dumps(claimed_data, indent=2), encoding="utf-8")

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


def test_full_round_trip(local_remote_root, local_repo, monkeypatch):
    monkeypatch.delenv("MINDSYNC_WORKER", raising=False)
    queue = RemoteQueue(remote_root=local_remote_root, ssh_host="")
    queue.ensure_remote_dirs()

    job_id = queue.submit_job(
        repo_path=local_repo,
        prompt="Check repository state",
        agent="auto",
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
    assert res["status"] in ("done", "failed")

    status_final = queue.get_status(job_id)
    assert status_final["state"] in ("done", "failed")
    assert status_final["job"]["worker_id"] == "worker-local"

    done_file = Path(local_remote_root) / "queue" / "done" / f"{job_id}.json"
    assert done_file.exists()
    if os.name == "posix":
        assert (done_file.stat().st_mode & 0o777) == 0o600
