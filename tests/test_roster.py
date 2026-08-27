"""Tests for mindsync register and honest roster status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.storage as storage
from mindsync.dispatch.adapters import load_adapters, user_config_path
from mindsync.onboarding import CommandResult
from mindsync.roster import (
    describe_agents,
    register_agent,
    resolve_register_capabilities,
)
from mindsync.server import list_agents


class RegisterRunner:
    def __init__(self) -> None:
        self.configured: set[str] = set()
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, resolved: str, args: list[str]) -> CommandResult:
        name = Path(resolved).name
        self.calls.append((name, list(args)))
        if args[:1] == ["--version"]:
            return CommandResult(0, f"{name} 1.0.0\n")
        if args[:2] == ["mcp", "list"]:
            payload = [{"name": item} for item in sorted(self.configured)]
            return CommandResult(0, json.dumps(payload))
        if args[:2] == ["mcp", "remove"]:
            self.configured.discard("mindsync")
            return CommandResult(0)
        if args[:2] == ["mcp", "add"]:
            self.configured.add("mindsync")
            return CommandResult(0)
        return CommandResult(1, stderr=f"unexpected: {args}")


def _resolver(name: str) -> str:
    return str(Path("fake-bin") / name)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DISPATCH_HOME", str(tmp_path / "dispatch-home"))
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "mindsync-home"))
    settings = config_mod.Settings()
    config_mod.settings = settings
    storage.settings = settings
    settings.ensure_dirs()
    return tmp_path / "user"


def test_capabilities_require_confirm_for_heavy_tags():
    assert resolve_register_capabilities(["coding", "review"]) == [
        "general",
        "coding",
        "review",
    ]
    with pytest.raises(ValueError, match="--confirm"):
        resolve_register_capabilities(["security"])
    assert resolve_register_capabilities(["security"], confirm=True) == [
        "general",
        "security",
    ]
    with pytest.raises(ValueError, match="Unknown capability"):
        resolve_register_capabilities(["telepathy"])


def test_register_unknown_cli_lands_in_roster_and_skips_mcp(tmp_path, monkeypatch):
    user_home = _isolate(tmp_path, monkeypatch)
    runner = RegisterRunner()

    first = register_agent(
        name="vidur",
        bin_name="opencode",
        capabilities=["coding", "review"],
        runner=runner,
        resolver=_resolver,
        user_home=user_home,
    )
    assert first["ok"] is True
    assert first["roster"]["action"] == "configured"
    assert first["mcp"]["action"] == "skipped"
    assert "no MCP-management command" in first["mcp"]["detail"]
    assert first["verify"]["binary_present"] is True
    assert first["verify"]["mcp_installed"] is False
    assert first["verify"]["routable"] is True
    saved = json.loads(Path(first["path"]).read_text(encoding="utf-8"))
    vidur = next(item for item in saved["agents"] if item["name"] == "vidur")
    assert vidur["bin"] == "opencode"
    assert vidur["capabilities"] == ["general", "coding", "review"]
    assert load_adapters()["vidur"].bin == "opencode"

    second = register_agent(
        name="vidur",
        bin_name="opencode",
        capabilities=["coding", "review"],
        runner=runner,
        resolver=_resolver,
        user_home=user_home,
    )
    assert second["roster"]["action"] == "already_configured"


def test_register_dry_run_does_not_write(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    result = register_agent(
        name="vidur",
        bin_name="opencode",
        capabilities=["coding"],
        dry_run=True,
        runner=RegisterRunner(),
        resolver=_resolver,
    )
    assert result["roster"]["action"] == "would_configure"
    assert not user_config_path().exists()


def test_register_updates_with_force(tmp_path, monkeypatch):
    user_home = _isolate(tmp_path, monkeypatch)
    register_agent(
        name="vidur",
        bin_name="opencode",
        capabilities=["coding"],
        runner=RegisterRunner(),
        resolver=_resolver,
        user_home=user_home,
    )
    with pytest.raises(ValueError, match="--force"):
        register_agent(
            name="vidur",
            bin_name="opencode",
            capabilities=["coding", "review"],
            runner=RegisterRunner(),
            resolver=_resolver,
            user_home=user_home,
        )
    updated = register_agent(
        name="vidur",
        bin_name="opencode",
        capabilities=["coding", "review"],
        force=True,
        runner=RegisterRunner(),
        resolver=_resolver,
        user_home=user_home,
    )
    assert updated["roster"]["action"] == "updated"
    assert updated["roster"]["agent"]["capabilities"] == ["general", "coding", "review"]


def test_register_known_cli_installs_mcp(tmp_path, monkeypatch):
    user_home = _isolate(tmp_path, monkeypatch)
    runner = RegisterRunner()
    result = register_agent(
        name="grok",
        bin_name="grok",
        capabilities=["review"],
        runner=runner,
        resolver=_resolver,
        user_home=user_home,
        python_exe="python-test",
    )
    assert result["mcp"]["action"] == "configured"
    assert result["verify"]["mcp_installed"] is True
    assert any(args[:2] == ["mcp", "add"] for _, args in runner.calls)


def test_describe_agents_reports_three_columns(tmp_path, monkeypatch):
    user_home = _isolate(tmp_path, monkeypatch)
    register_agent(
        name="vidur",
        bin_name="opencode",
        capabilities=["coding"],
        runner=RegisterRunner(),
        resolver=_resolver,
        user_home=user_home,
    )
    rows = describe_agents(runner=RegisterRunner(), resolver=_resolver, user_home=user_home)
    vidur = next(row for row in rows if row["name"] == "vidur")
    assert vidur["source"] == "user"
    assert vidur["binary_present"] is True
    assert vidur["mcp_installed"] is False
    assert vidur["routable"] is True
    assert "coding" in vidur["capabilities"]

    listed = list_agents()
    listed_vidur = next(item for item in listed if item["name"] == "vidur")
    assert listed_vidur["source"] == "user"
    assert "mcp_installed" in listed_vidur
    assert "routable" in listed_vidur
    assert "available" in listed_vidur
