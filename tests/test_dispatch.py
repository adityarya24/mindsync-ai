"""Tests for mindsync.dispatch (agent-dispatch Python port)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mindsync.bus import EventType, poll_events
from mindsync.dispatch.adapters import (
    UnknownAgentError,
    build_invocation,
    load_adapters,
    resolve_adapter,
    user_config_path,
)
from mindsync.dispatch.proc import names_match, resolve_bin, spawn_foreground
from mindsync.dispatch.runner import (
    assert_arg_mode_spawn_safe,
    job_result,
    run_task,
)
from mindsync.dispatch import store
import mindsync.config as config_mod
import mindsync.storage as storage


def _isolate_dispatch(tmp_path: Path, monkeypatch):
    home = tmp_path / "dispatch-home"
    home.mkdir()
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(home))
    # Isolate event bus / mindsync home too
    ms_home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(ms_home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    return home


def test_names_match_rules():
    assert names_match("node.exe", "node.exe") is True
    assert names_match("verylongprocess", "verylongprocessname.bin") is True
    assert names_match(None, "x") is False
    assert names_match("x", None) is False
    assert names_match("other", "node") is False
    assert names_match("node", "nodemon") is False
    assert names_match("node-mainthread", "node") is True
    assert names_match("nodemon", "node") is False


def test_presets_load():
    adapters = load_adapters()
    for name in ("codex", "claude", "gemini", "cursor", "aider", "grok"):
        assert name in adapters
    assert adapters["codex"].input == "stdin"
    assert adapters["grok"].input == "arg"
    assert any("{prompt}" in t for t in adapters["grok"].runArgs)


def test_user_config_merge(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "myagent",
                        "bin": "mybin",
                        "input": "arg",
                        "runArgs": ["go", "{prompt}"],
                    },
                    {"name": "codex", "timeoutMs": 123},
                ]
            }
        ),
        encoding="utf-8",
    )
    adapters = load_adapters()
    assert adapters["myagent"].bin == "mybin"
    assert adapters["codex"].timeoutMs == 123
    assert adapters["codex"].bin == "codex"


def test_unknown_agent(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    with pytest.raises(UnknownAgentError, match="codex"):
        resolve_adapter("ghost")


def test_build_invocation_stdin_and_model(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "a1",
                        "bin": "b",
                        "input": "stdin",
                        "runArgs": ["exec"],
                        "writeArgs": ["--w"],
                        "modelArgs": ["-m", "{model}"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    inv = build_invocation(
        resolve_adapter("a1"),
        prompt='p "quoted" & stuff',
        model="org/model-x.1:latest",
        write=True,
    )
    assert inv["args"] == ["exec", "--w", "-m", "org/model-x.1:latest"]
    assert inv["input"] == 'p "quoted" & stuff'


def test_build_invocation_rejects_bad_model(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "a3",
                        "bin": "b",
                        "input": "stdin",
                        "runArgs": ["exec"],
                        "modelArgs": ["-m", "{model}"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid model"):
        build_invocation(resolve_adapter("a3"), prompt="p", model="gpt-5 & calc.exe")


def test_arg_mode_requires_placeholder(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "bad", "bin": "b", "input": "arg", "runArgs": ["run"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match=r"\{prompt\}"):
        resolve_adapter("bad")


def test_assert_arg_mode_spawn_safe_blocks_cmd_shim():
    class A:
        name = "grok"
        input = "arg"

    with pytest.raises(RuntimeError, match="unsafe"):
        assert_arg_mode_spawn_safe(A(), r"C:\Tools\grok.cmd", platform="win32")
    # stdin adapters are fine
    class B:
        name = "codex"
        input = "stdin"

    assert_arg_mode_spawn_safe(B(), r"C:\Tools\codex.cmd", platform="win32")


@pytest.mark.asyncio
async def test_foreground_lifecycle_with_python_echo(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    # Register a fake agent that runs the current Python interpreter.
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "pyecho",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": ["-c", "import sys; print(sys.stdin.read().strip())"],
                        "timeoutMs": 30_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    res = await run_task(
        agent="pyecho",
        prompt="hello-from-dispatch",
        background=False,
        publisher_agent="test-agent",
    )
    assert res["job"]["status"] == "done"
    assert res["result"] is not None
    assert "hello-from-dispatch" in res["result"]

    # Event bus received started + completed
    events = poll_events(since_seq=0, limit=50)
    types = [e.event_type for e in events]
    assert EventType.JOB_STARTED.value in types or "job.started" in types
    assert EventType.JOB_COMPLETED.value in types or "job.completed" in types

    # job_result API
    data = job_result(res["job"]["id"])
    assert data["result"] is not None
    assert "hello-from-dispatch" in data["result"]


@pytest.mark.asyncio
async def test_spawn_foreground_timeout(tmp_path):
    # Short timeout against a sleeping python
    r = await spawn_foreground(
        sys.executable,
        ["-c", "import time; time.sleep(30)"],
        timeout_ms=200,
        cwd=str(tmp_path),
    )
    assert r["timedOut"] is True


def test_resolve_bin_python():
    found = resolve_bin(sys.executable)
    assert found is not None
    assert Path(found).exists()


def test_job_id_validation(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Invalid job id"):
        store.job_paths("../evil")


def _register_failing_agent(tmp_path, monkeypatch, *, args: list[str]) -> None:
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "pyfail",
                        "bin": sys.executable,
                        "input": "stdin",
                        "runArgs": args,
                        "timeoutMs": 30_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_failed_job_result_includes_stderr(tmp_path, monkeypatch):
    """A failed job must explain itself: stderr is the only diagnostic there is."""
    _register_failing_agent(
        tmp_path,
        monkeypatch,
        args=["-c", "import sys; print('TRUST_GATE_BLOCKED', file=sys.stderr); sys.exit(3)"],
    )

    res = await run_task(
        agent="pyfail", prompt="x", background=False, publisher_agent="test-agent"
    )
    assert res["job"]["status"] == "failed"
    assert res["job"]["exitCode"] == 3
    assert res["result"], "failed job returned an empty result"
    assert "TRUST_GATE_BLOCKED" in res["result"]

    # Same content must reach anyone polling after the fact.
    assert "TRUST_GATE_BLOCKED" in job_result(res["job"]["id"])["result"]


@pytest.mark.asyncio
async def test_failed_job_keeps_partial_stdout_alongside_stderr(tmp_path, monkeypatch):
    """Partial output stays first; the diagnostic is appended, not substituted."""
    _register_failing_agent(
        tmp_path,
        monkeypatch,
        args=[
            "-c",
            "import sys; print('PARTIAL_WORK'); print('BOOM', file=sys.stderr); sys.exit(1)",
        ],
    )

    res = await run_task(
        agent="pyfail", prompt="x", background=False, publisher_agent="test-agent"
    )
    result = res["result"]
    assert "PARTIAL_WORK" in result
    assert "BOOM" in result
    assert result.index("PARTIAL_WORK") < result.index("BOOM")


@pytest.mark.asyncio
async def test_successful_job_result_has_no_diagnostic_block(tmp_path, monkeypatch):
    """Stderr noise on a passing run must not pollute the result."""
    _register_failing_agent(
        tmp_path,
        monkeypatch,
        args=["-c", "import sys; print('OK'); print('just a warning', file=sys.stderr)"],
    )

    res = await run_task(
        agent="pyfail", prompt="x", background=False, publisher_agent="test-agent"
    )
    assert res["job"]["status"] == "done"
    assert res["result"].strip() == "OK"


def test_gemini_preset_runs_headless():
    """Gemini CLI aborts in an untrusted directory unless trust is waived."""
    gemini = load_adapters()["gemini"]
    assert "--skip-trust" in gemini.runArgs
