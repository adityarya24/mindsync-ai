"""Tests for dispatch roles functionality."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mindsync.dispatch.adapters import (
    UnknownRoleError,
    build_invocation,
    load_adapters,
    load_roles,
    resolve_adapter,
    resolve_role,
    user_config_path,
)
from mindsync.dispatch.cli import parse_run_args
from mindsync.dispatch.runner import run_task
from mindsync.dispatch import store
from mindsync.server import delegate_task, list_roles
import mindsync.config as config_mod
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


def _write_role_config(tmp_path: Path, monkeypatch, role: dict) -> None:
    """Register one echo-style agent plus a single role pointing at it."""
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "custom-runner",
            "bin": sys.executable,
            "input": "stdin",
            "runArgs": ["-c", "pass"],
            "modelArgs": ["--model", "{model}"],
            "efforts": ["low", "high"],
            "effortArgs": ["--effort", "{effort}"],
        }],
        "roles": {"heavy-task": role},
    }), encoding="utf-8")


@pytest.mark.asyncio
async def test_role_defaults_reach_the_job_without_explicit_model_or_effort(
    tmp_path, monkeypatch
):
    """The documented path: `run --role X "task"` with model and effort only in config.

    Every other role test either builds the invocation by hand or passes an explicit
    model, so dropping the role fallback in run_task would leave the suite green.
    """
    _write_role_config(tmp_path, monkeypatch, {
        "agent": "custom-runner", "model": "model-v1", "effort": "high",
    })

    res = await run_task(role="heavy-task", prompt="do work")
    job = res["job"]

    assert job["agent"] == "custom-runner"
    assert job["role"] == "heavy-task"
    assert job["model"] == "model-v1"
    assert job["effort"] == "high"

    # And they must survive into the argv the agent is actually spawned with.
    inv = build_invocation(
        resolve_adapter(job["agent"]),
        prompt=job["prompt"],
        model=job["model"],
        effort=job["effort"],
    )
    assert "--model" in inv["args"] and "model-v1" in inv["args"]
    assert "--effort" in inv["args"] and "high" in inv["args"]


@pytest.mark.asyncio
async def test_background_role_job_records_resolution(tmp_path, monkeypatch):
    """Background is how work is really detached, and it takes a different code path."""
    _write_role_config(tmp_path, monkeypatch, {
        "agent": "custom-runner", "model": "model-v1", "effort": "low",
    })

    res = await run_task(role="heavy-task", prompt="do work", background=True)
    meta = store.get_job(res["job"]["id"])
    assert meta["agent"] == "custom-runner"
    assert meta["model"] == "model-v1"
    assert meta["effort"] == "low"


def test_role_prompt_may_mention_an_agent_by_name(tmp_path, monkeypatch):
    """Prompts talk about agents ("codex is hanging"); that must not look like an agent arg."""
    _write_role_config(tmp_path, monkeypatch, {"agent": "custom-runner"})

    opts = parse_run_args(["--role", "heavy-task", "codex", "is", "hanging", "on", "Windows"])
    assert opts["role"] == "heavy-task"
    assert opts["agent"] is None
    assert opts["prompt"] == "codex is hanging on Windows"


def test_role_with_model_the_agent_cannot_pass_fails_at_config_load(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{"name": "plain", "bin": sys.executable, "input": "stdin", "runArgs": []}],
        "roles": {"r": {"agent": "plain", "model": "some-model"}},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="no modelArgs"):
        load_roles()


def test_non_object_role_entry_is_rejected(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{"name": "plain", "bin": sys.executable, "input": "stdin", "runArgs": []}],
        "roles": {"r": "plain"},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="must be an object"):
        load_roles()


@pytest.mark.asyncio
async def test_empty_prompt_is_rejected(tmp_path, monkeypatch):
    _write_role_config(tmp_path, monkeypatch, {"agent": "custom-runner"})
    with pytest.raises(ValueError, match="prompt must not be empty"):
        await run_task(role="heavy-task", prompt="   ")


def test_role_resolves_to_agent_model_effort_and_build_invocation(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "custom-runner",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["exec"],
                        "modelArgs": ["--model", "{model}"],
                        "efforts": ["low", "high"],
                        "effortArgs": ["--effort", "{effort}"],
                    }
                ],
                "roles": {
                    "heavy-task": {
                        "agent": "custom-runner",
                        "model": "model-v1",
                        "effort": "high",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    role_cfg = resolve_role("heavy-task")
    assert role_cfg.name == "heavy-task"
    assert role_cfg.agent == "custom-runner"
    assert role_cfg.model == "model-v1"
    assert role_cfg.effort == "high"

    adapter = resolve_adapter(role_cfg.agent)
    inv = build_invocation(
        adapter,
        prompt="do work",
        model=role_cfg.model,
        effort=role_cfg.effort,
    )
    assert inv["args"] == ["exec", "--model", "model-v1", "--effort", "high"]


@pytest.mark.asyncio
async def test_explicit_model_effort_override_role(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "dummy-agent",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; print(sys.stdin.read().strip())"],
                        "modelArgs": ["--model", "{model}"],
                        "efforts": ["low", "high"],
                        "effortArgs": ["--effort", "{effort}"],
                    }
                ],
                "roles": {
                    "fast-role": {
                        "agent": "dummy-agent",
                        "model": "fast-model",
                        "effort": "low",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    res = await run_task(
        role="fast-role",
        prompt="hello",
        model="custom-model",
        effort="high",
        background=False,
    )
    assert res["job"]["status"] == "done"
    assert res["job"]["role"] == "fast-role"
    assert res["job"]["agent"] == "dummy-agent"
    assert res["job"]["model"] == "custom-model"
    assert res["job"]["effort"] == "high"


def test_role_naming_unknown_agent_fails_at_config_load(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "roles": {
                    "invalid-role": {
                        "agent": "nonexistent-agent",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid-role"):
        load_roles()


def test_role_unsupported_effort_fails_at_config_load(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "limited-agent",
                        "bin": "echo",
                        "efforts": ["low"],
                        "effortArgs": ["--effort", "{effort}"],
                    }
                ],
                "roles": {
                    "bad-effort-role": {
                        "agent": "limited-agent",
                        "effort": "high",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad-effort-role"):
        load_roles()


@pytest.mark.asyncio
async def test_agent_and_role_both_or_neither_raises(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="[Ee]xactly one"):
        await run_task(agent="codex", role="bulk", prompt="hello")

    with pytest.raises(ValueError, match="[Ee]xactly one"):
        await run_task(prompt="hello")


@pytest.mark.asyncio
async def test_run_task_records_role_in_job_meta(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "mock-agent",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "print('ok')"],
                    }
                ],
                "roles": {
                    "simple-role": {
                        "agent": "mock-agent",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    res = await run_task(role="simple-role", prompt="run this")
    job_id = res["job"]["id"]
    job_data = store.get_job(job_id)
    assert job_data is not None
    assert job_data["role"] == "simple-role"
    assert job_data["agent"] == "mock-agent"


def test_resolve_role_empty_config(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    with pytest.raises(UnknownRoleError) as exc_info:
        resolve_role("nonexistent-role")

    err_msg = str(exc_info.value.args[0])
    assert "no roles are configured" in err_msg.lower() or "no roles configured" in err_msg.lower()
    assert str(user_config_path()) in err_msg


def test_config_without_roles_key_loads_fine(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "test-agent",
                        "bin": "echo",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    adapters = load_adapters()
    assert "test-agent" in adapters
    roles = load_roles()
    assert roles == {}


def test_cli_parse_run_args_role_validation():
    # Role set, prompt provided
    opts = parse_run_args(["--role", "bulk", "my task prompt"])
    assert opts["role"] == "bulk"
    assert opts["agent"] is None
    assert opts["prompt"] == "my task prompt"

    # --role takes no positional agent, so leading words are prompt, not an agent.
    # Guessing by "is this word a known agent" was tried and rejected: it breaks the
    # common valid case of a prompt that mentions an agent by name.
    opts = parse_run_args(["codex", "my task prompt", "--role", "bulk"])
    assert opts["role"] == "bulk"
    assert opts["agent"] is None
    assert opts["prompt"] == "codex my task prompt"

    # Neither agent nor role given -> SystemExit
    with pytest.raises(SystemExit):
        parse_run_args(["my task prompt"])


@pytest.mark.asyncio
async def test_mcp_delegate_task_and_list_roles(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "mcp-agent",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "print('mcp ok')"],
                    }
                ],
                "roles": {
                    "mcp-role": {
                        "agent": "mcp-agent",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    roles_text = list_roles()
    assert "mcp-role" in roles_text
    assert "mcp-agent" in roles_text

    res = await delegate_task(role="mcp-role", prompt="mcp work")
    assert "mcp ok" in res

    err_res = await delegate_task(agent="mcp-agent", role="mcp-role", prompt="mcp work")
    assert "Error:" in err_res
