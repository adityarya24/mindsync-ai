"""Tests for model and reasoning-effort selection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mindsync.dispatch.adapters import (
    AdapterConfig,
    build_invocation,
    list_models,
    load_adapters,
    resolve_adapter,
    user_config_path,
)
from mindsync.dispatch.runner import run_task

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

def test_default_model_applied(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b",
            "defaultModel": "def-model",
            "modelArgs": ["-m", "{model}"]
        }]
    }))
    inv = build_invocation(resolve_adapter("a1"), prompt="hi")
    assert "-m" in inv["args"]
    assert "def-model" in inv["args"]

def test_caller_supplied_model_overrides_default(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b",
            "defaultModel": "def-model",
            "modelArgs": ["-m", "{model}"]
        }]
    }))
    inv = build_invocation(resolve_adapter("a1"), prompt="hi", model="caller-model")
    assert "caller-model" in inv["args"]
    assert "def-model" not in inv["args"]

def test_passing_model_with_empty_args_raises(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b",
            "modelArgs": []
        }]
    }))
    with pytest.raises(ValueError, match="a1"):
        build_invocation(resolve_adapter("a1"), prompt="hi", model="caller-model")

def test_passing_effort_without_support_falls_back_with_warning(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b"
        }]
    }))
    inv = build_invocation(resolve_adapter("a1"), prompt="hi", effort="high")
    assert inv["effectiveEffort"] is None
    assert inv["requestedEffort"] == "high"
    assert "does not support reasoning effort" in inv["warnings"][0]

def test_effort_outside_supported_values_falls_back_with_warning(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b",
            "efforts": ["low", "high"],
            "effortArgs": ["-e", "{effort}"]
        }]
    }))
    inv = build_invocation(resolve_adapter("a1"), prompt="hi", effort="medium")
    assert inv["effectiveEffort"] is None
    assert "supports only: low, high" in inv["warnings"][0]
    assert "-e" not in inv["args"]


def test_malformed_effort_is_still_rejected(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"agents": [{"name": "a1", "bin": "b"}]}))

    with pytest.raises(ValueError, match="Invalid effort"):
        build_invocation(resolve_adapter("a1"), prompt="hi", effort="high;unsafe")

def test_effort_substitution_both_styles(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b",
            "efforts": ["high"],
            "effortArgs": ["--effort", "{effort}"]
        }, {
            "name": "a2", "bin": "b",
            "efforts": ["high"],
            "effortArgs": ["-c", "model_reasoning_effort={effort}"]
        }]
    }))
    inv1 = build_invocation(resolve_adapter("a1"), prompt="hi", effort="high")
    assert "--effort" in inv1["args"] and "high" in inv1["args"]
    
    inv2 = build_invocation(resolve_adapter("a2"), prompt="hi", effort="high")
    assert "-c" in inv2["args"] and "model_reasoning_effort=high" in inv2["args"]

def test_list_models_parsing(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    
    def fake_run(*args, **kwargs):
        class Result:
            returncode = 0
            stdout = "gemini-3.6-flash-high\n\nYou are not authenticated.\nDefault model: grok-4.5\nAvailable models:\n  * grok-4.5 (default)\n- some-model\nbadline:\n"
        return Result()
    
    monkeypatch.setattr("subprocess.run", fake_run)
    
    cfg = AdapterConfig(name="test", bin=sys.executable, modelsArgs=["models"])
    models = list_models(cfg)
    assert "gemini-3.6-flash-high" in models
    assert "grok-4.5" in models
    assert "some-model" in models
    assert "You are not authenticated." not in models
    assert "Default model: grok-4.5" not in models
    assert "Available models:" not in models
    assert "badline:" not in models

def test_list_models_fallback_on_failure(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    
    def fake_run(*args, **kwargs):
        class Result:
            returncode = 1
        return Result()
    
    monkeypatch.setattr("subprocess.run", fake_run)
    
    cfg = AdapterConfig(name="test", bin=sys.executable, modelsArgs=["models"], models=["fallback-model"])
    models = list_models(cfg)
    assert models == ["fallback-model"]

def test_adapter_validation_rejects_bad_efforts(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b",
            "efforts": ["high"],
            "effortArgs": ["--effort"]
        }]
    }))
    with pytest.raises(ValueError):
        load_adapters()
        
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b",
            "efforts": [],
            "effortArgs": ["--effort", "{effort}"]
        }]
    }))
    with pytest.raises(ValueError):
        load_adapters()

    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": "b",
            "defaultModel": "model",
            "modelArgs": []
        }]
    }))
    with pytest.raises(ValueError):
        load_adapters()

@pytest.mark.asyncio
async def test_run_task_stores_effort_in_meta(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "a1", "bin": sys.executable,
            "runArgs": ["-c", "print('ok')"],
            "efforts": ["high"],
            "effortArgs": ["-e", "{effort}"]
        }]
    }))
    
    res = await run_task(agent="a1", prompt="x", effort="high")
    job = res["job"]
    assert job["effort"] == "high"
    assert job["effectiveEffort"] == "high"
    assert job["warnings"] == []
    assert "ok" in res["result"]


@pytest.mark.asyncio
async def test_run_task_completes_when_agent_does_not_support_effort(tmp_path, monkeypatch):
    _isolate_dispatch(tmp_path, monkeypatch)
    cfg = user_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "agents": [{
            "name": "cursor-like", "bin": sys.executable,
            "runArgs": ["-c", "print('ok without effort')"]
        }]
    }))

    res = await run_task(agent="cursor-like", prompt="x", effort="high")
    job = res["job"]
    assert job["status"] == "done"
    assert job["effort"] == "high"
    assert job["effectiveEffort"] is None
    assert "does not support reasoning effort" in job["warnings"][0]
    assert "ok without effort" in res["result"]
