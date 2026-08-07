"""Focused tests for remote execution boundaries and orchestration opt-in."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.orchestration as orchestration
import mindsync.remote_queue as remote_queue
import mindsync.storage as storage
from mindsync.dispatch.adapters import user_config_path
from mindsync.manage import build_parser, main as manage_main
from mindsync.remote_queue import RemoteQueue, run_worker_once


@pytest.fixture
def local_repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True
    )
    return str(repo)


@pytest.fixture
def configured_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "mindsync-home"))
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(tmp_path / "dispatch-home"))
    settings = config_mod.Settings()
    config_mod.settings = settings
    storage.settings = settings
    orchestration.settings = settings
    settings.ensure_dirs()
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "fake",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; sys.stdin.read()"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def queue_root(tmp_path: Path) -> str:
    return str(tmp_path / "remote-root")


def _queue(root: str) -> RemoteQueue:
    queue = RemoteQueue(remote_root=root, ssh_host="")
    queue.ensure_remote_dirs()
    return queue


def test_submit_execution_mode_defaults_and_validates(queue_root: str, local_repo: str) -> None:
    queue = _queue(queue_root)
    worker_id = queue.submit_job(repo_path=local_repo, prompt="legacy", agent="fake")
    worker_payload = json.loads(
        (Path(queue_root) / "queue" / "pending" / f"{worker_id}.json").read_text()
    )
    assert worker_payload["execution_mode"] == "worker"
    assert worker_payload["delegation_depth"] == 0

    orchestrator_id = queue.submit_job(
        repo_path=local_repo,
        prompt="orchestrate",
        agent="fake",
        execution_mode="orchestrator",
    )
    orchestrator_payload = json.loads(
        (Path(queue_root) / "queue" / "pending" / f"{orchestrator_id}.json").read_text()
    )
    assert orchestrator_payload["execution_mode"] == "orchestrator"
    assert orchestrator_payload["delegation_depth"] == 0

    with pytest.raises(ValueError, match="execution_mode"):
        queue.submit_job(repo_path=local_repo, prompt="bad", execution_mode="nested")
    with pytest.raises(ValueError, match="delegation_depth"):
        queue.submit_job(repo_path=local_repo, prompt="bad", delegation_depth=1)


def test_legacy_payload_defaults_to_non_recursive_worker(
    queue_root: str,
    local_repo: str,
    configured_dispatch: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _queue(queue_root)
    job_id = queue.submit_job(repo_path=local_repo, prompt="legacy", agent="fake")
    pending = Path(queue_root) / "queue" / "pending" / f"{job_id}.json"
    payload = json.loads(pending.read_text())
    payload.pop("execution_mode")
    payload.pop("delegation_depth")
    pending.write_text(json.dumps(payload), encoding="utf-8")

    seen_env: list[dict[str, str]] = []

    async def fake_spawn(*args, **kwargs):
        seen_env.append(kwargs["env"])
        return {"stdout": "worker-ok", "stderr": "", "exitCode": 0, "timedOut": False}

    import mindsync.dispatch.runner as runner

    monkeypatch.setattr(runner, "spawn_foreground", fake_spawn)
    result = run_worker_once(queue, "worker", [local_repo])
    assert result and result["status"] == "done"
    assert seen_env and seen_env[0]["MINDSYNC_WORKER"] == "1"
    local_meta = next(
        json.loads(path.read_text())
        for path in (Path(user_config_path()).parent.parent / "dispatch-home" / "jobs").glob(
            "*/meta.json"
        )
    )
    assert local_meta["executionMode"] == "worker"
    assert local_meta["delegationDepth"] == 1


def test_orchestrator_requires_local_opt_in(
    queue_root: str, local_repo: str, configured_dispatch: None
) -> None:
    queue = _queue(queue_root)
    job_id = queue.submit_job(
        repo_path=local_repo,
        prompt="orchestrate",
        agent="fake",
        execution_mode="orchestrator",
    )
    result = run_worker_once(queue, "worker", [local_repo], allow_orchestrator=False)
    assert result and result["status"] == "failed"
    status = queue.get_status(job_id)
    assert status and "orchestrator execution is disabled" in status["job"]["result"]


def test_orchestrator_parent_is_not_marked_worker(
    queue_root: str,
    local_repo: str,
    configured_dispatch: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _queue(queue_root)
    queue.submit_job(
        repo_path=local_repo,
        prompt="orchestrate",
        agent="fake",
        execution_mode="orchestrator",
    )
    monkeypatch.setenv("MINDSYNC_WORKER", "1")
    seen_env: list[dict[str, str]] = []

    async def fake_spawn(*args, **kwargs):
        seen_env.append(kwargs["env"])
        return {"stdout": "orchestrator-ok", "stderr": "", "exitCode": 0, "timedOut": False}

    import mindsync.dispatch.runner as runner

    monkeypatch.setattr(runner, "spawn_foreground", fake_spawn)
    result = run_worker_once(queue, "worker", [local_repo], allow_orchestrator=True)
    assert result and result["status"] == "done"
    assert seen_env and "MINDSYNC_WORKER" not in seen_env[0]
    local_meta = next(
        json.loads(path.read_text())
        for path in (Path(user_config_path()).parent.parent / "dispatch-home" / "jobs").glob(
            "*/meta.json"
        )
    )
    assert local_meta["executionMode"] == "orchestrator"
    assert local_meta["delegationDepth"] == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("execution_mode", ["orchestrator"], "execution_mode"),
        ("delegation_depth", 1, "delegation_depth"),
    ],
)
def test_invalid_remote_metadata_fails_closed(
    queue_root: str,
    local_repo: str,
    field: str,
    value: object,
    message: str,
) -> None:
    queue = _queue(queue_root)
    job_id = queue.submit_job(repo_path=local_repo, prompt="bad metadata", agent="fake")
    pending = Path(queue_root) / "queue" / "pending" / f"{job_id}.json"
    payload = json.loads(pending.read_text())
    payload[field] = value
    pending.write_text(json.dumps(payload), encoding="utf-8")

    result = run_worker_once(queue, "worker", [local_repo])
    assert result and result["status"] == "failed"
    status = queue.get_status(job_id)
    assert status and message in status["job"]["result"]


def test_non_object_remote_payload_fails_closed(queue_root: str, local_repo: str) -> None:
    queue = _queue(queue_root)
    job_id = queue.submit_job(repo_path=local_repo, prompt="bad shape", agent="fake")
    pending = Path(queue_root) / "queue" / "pending" / f"{job_id}.json"
    pending.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    result = run_worker_once(queue, "worker", [local_repo])
    assert result and result["status"] == "failed"
    status = queue.get_status(job_id)
    assert status and "payload must be a JSON object" in status["job"]["result"]


def test_malformed_remote_payload_fails_closed(queue_root: str, local_repo: str) -> None:
    queue = _queue(queue_root)
    job_id = queue.submit_job(repo_path=local_repo, prompt="bad json", agent="fake")
    pending = Path(queue_root) / "queue" / "pending" / f"{job_id}.json"
    pending.write_text("not-json", encoding="utf-8")

    result = run_worker_once(queue, "worker", [local_repo])
    assert result and result["status"] == "failed"
    status = queue.get_status(job_id)
    assert status and "payload must be a JSON object" in status["job"]["result"]


def test_submit_and_status_cli_expose_execution_mode(
    queue_root: str, local_repo: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed = build_parser().parse_args(
        [
            "submit",
            "--repo",
            local_repo,
            "--prompt",
            "orchestrate",
            "--execution-mode",
            "orchestrator",
        ]
    )
    assert parsed.execution_mode == "orchestrator"

    queue = _queue(queue_root)
    job_id = queue.submit_job(
        repo_path=local_repo, prompt="orchestrate", execution_mode="orchestrator"
    )
    monkeypatch.setattr(remote_queue.settings, "remote_root", queue_root)
    monkeypatch.setattr(remote_queue.settings, "ssh_host", "")
    assert manage_main(["status", job_id]) == 0
    output = capsys.readouterr().out
    assert "execution_mode: orchestrator" in output
    assert "delegation_depth: 0" in output
