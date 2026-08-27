"""Tests for mindsync register and honest roster status."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.onboarding as onboarding
import mindsync.storage as storage
from mindsync.dispatch.adapters import load_adapters, user_config_path
from mindsync.manage import build_parser
from mindsync.onboarding import CommandResult
from mindsync.roster import (
    describe_agents,
    discover_agent_clis,
    suggest_unknown_clis,
    looks_like_agent_cli,
    path_cli_names,
    register_agent,
    resolve_register_capabilities,
)
from mindsync.server import list_agents

_KNOWN_MCP_BINS = {"codex", "claude", "gemini", "grok", "cursor", "cursor-agent"}


class RegisterRunner:
    def __init__(self, mcp_bins: set[str] | None = None) -> None:
        self.configured: set[str] = set()
        self.calls: list[tuple[str, list[str]]] = []
        self.mcp_bins = set(_KNOWN_MCP_BINS if mcp_bins is None else mcp_bins)

    def __call__(self, resolved: str, args: list[str]) -> CommandResult:
        name = Path(resolved).name
        self.calls.append((name, list(args)))
        if args[:1] == ["--version"]:
            return CommandResult(0, f"{name} 1.0.0\n")
        if args[:1] == ["mcp"] and name not in self.mcp_bins:
            return CommandResult(1, stderr="unknown command: mcp")
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


def test_looks_like_agent_cli_uses_seed_and_name_hints():
    assert looks_like_agent_cli("opencode") is True
    assert looks_like_agent_cli("my-agent") is True
    assert looks_like_agent_cli("windsurf") is True
    assert looks_like_agent_cli("ffmpeg") is False
    assert looks_like_agent_cli("git") is False


def test_path_cli_names_skips_denylist_and_system_dirs(tmp_path, monkeypatch):
    good = tmp_path / "bin"
    good.mkdir()
    system = tmp_path / "Windows" / "System32"
    system.mkdir(parents=True)

    def touch(directory: Path, name: str) -> None:
        filename = f"{name}.exe" if os.name == "nt" else name
        (directory / filename).write_bytes(b"")

    touch(good, "opencode")
    touch(good, "git")
    touch(good, "ffmpeg")
    touch(system, "my-agent")
    monkeypatch.setenv("PATH", os.pathsep.join([str(good), str(system)]))

    found = path_cli_names()
    assert "opencode" in found
    assert "git" not in found
    assert "ffmpeg" in found
    assert "my-agent" not in found


PATH_SAMPLE = {
    "opencode": str(Path("fake-bin") / "opencode"),
    "ffmpeg": str(Path("fake-bin") / "ffmpeg"),
    "git": str(Path("fake-bin") / "git"),
    "my-agent": str(Path("fake-bin") / "my-agent"),
    "codex": str(Path("fake-bin") / "codex"),
    "pi": str(Path("fake-bin") / "pi"),
}


def test_discover_agent_clis_registers_only_known_agent_clis(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    found = discover_agent_clis(
        resolver=lambda name: str(Path("fake-bin") / name),
        runner=RegisterRunner(),
        path_bins=PATH_SAMPLE,
    )
    names = {item["name"] for item in found}
    # 'my-agent' looks agent-ish but is unrecognised, so it is suggested, not
    # registered — proving it would mean executing it.
    assert names == {"opencode", "pi"}
    assert all(item["mcp_capable"] is False for item in found)


def test_agentish_unknowns_are_suggested_not_registered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert suggest_unknown_clis(
        resolver=lambda name: str(Path("fake-bin") / name),
        path_bins=PATH_SAMPLE,
    ) == ["my-agent"]


def test_discovery_never_executes_an_unrecognised_binary(tmp_path, monkeypatch):
    """The bug this guards: probing ran '<bin> mcp list' on anything whose name
    matched, which on a real Linux host meant spawning ssh-agent, blocking on
    pkttyagent, and running a *-fleet-restart script."""
    _isolate(tmp_path, monkeypatch)
    executed: list[str] = []

    def recording_runner(resolved_bin, args):
        executed.append(Path(resolved_bin).name)
        return RegisterRunner()(resolved_bin, args)

    dangerous = {
        name: str(Path("fake-bin") / name)
        for name in (
            "ssh-agent",
            "gpg-agent",
            "pkttyagent",
            "systemd-tty-ask-password-agent",
            "fail2ban-client",
            "hermes-fleet-restart",
            "apport-cli",
            "mmcli",
        )
    }
    found = discover_agent_clis(
        resolver=lambda name: str(Path("fake-bin") / name),
        runner=recording_runner,
        path_bins=dangerous,
    )
    assert found == []
    assert executed == []

    # Named daemons stay out of the roster entirely; only the fleet-restart
    # script is agent-ish enough to be worth telling the operator about.
    assert suggest_unknown_clis(
        resolver=lambda name: str(Path("fake-bin") / name),
        path_bins=dangerous,
    ) == ["hermes-fleet-restart"]


def test_generic_substrings_do_not_look_like_agent_clis():
    for name in ("uclampset", "wamp", "sample", "sg_timestamp", "apport-cli", "mmcli"):
        assert looks_like_agent_cli(name) is False, name
    for name in ("amp", "amp-cli", "gpt-cli", "my-agent", "opencode"):
        assert looks_like_agent_cli(name) is True, name


def test_register_unknown_mcp_capable_cli_uses_generic_add(tmp_path, monkeypatch):
    user_home = _isolate(tmp_path, monkeypatch)
    runner = RegisterRunner(mcp_bins={"opencode"})
    result = register_agent(
        name="vidur",
        bin_name="opencode",
        capabilities=["coding"],
        runner=runner,
        resolver=_resolver,
        user_home=user_home,
        python_exe="python-test",
    )
    assert result["mcp"]["action"] == "configured"
    assert result["verify"]["mcp_installed"] is True
    assert any(
        name == "opencode" and args[:2] == ["mcp", "add"] and "--scope" in args
        for name, args in runner.calls
    )
    assert "MINDSYNC_CALLER_CLI=vidur" in next(
        args for name, args in runner.calls if args[:2] == ["mcp", "add"]
    )


def _selective_resolver(*allowed: str):
    def resolver(name: str) -> str | None:
        if name in allowed:
            return str(Path("fake-bin") / name)
        return None

    return resolver


def test_setup_discovers_unknown_path_cli(tmp_path, monkeypatch):
    user_home = _isolate(tmp_path, monkeypatch)
    runner = RegisterRunner()
    result = onboarding.setup(
        mode="auto",
        runner=runner,
        resolver=_selective_resolver("opencode"),
        user_home=user_home,
        policy_file=tmp_path / "orchestration.json",
        python_exe="python-test",
        install_hooks=False,
        path_bins={
            "opencode": str(Path("fake-bin") / "opencode"),
            "git": str(Path("fake-bin") / "git"),
            "ffmpeg": str(Path("fake-bin") / "ffmpeg"),
        },
    )
    names = {item["cli"] for item in result["actions"]}
    assert "opencode" in names
    assert "git" not in names
    assert "ffmpeg" not in names
    discovered = next(item for item in result["actions"] if item["cli"] == "opencode")
    assert discovered["action"] == "configured"
    assert "PATH discovery" in discovered["detail"]
    assert load_adapters()["opencode"].bin == "opencode"


def test_setup_with_cli_skips_path_discovery(tmp_path, monkeypatch):
    user_home = _isolate(tmp_path, monkeypatch)
    result = onboarding.setup(
        mode="auto",
        cli_names=["codex"],
        runner=RegisterRunner(),
        resolver=_selective_resolver("codex", "opencode"),
        user_home=user_home,
        policy_file=tmp_path / "orchestration.json",
        python_exe="python-test",
        install_hooks=False,
        path_bins={"opencode": str(Path("fake-bin") / "opencode")},
    )
    assert all(item["cli"] != "opencode" for item in result["actions"])
    assert any(item["cli"] == "codex" for item in result["actions"])


def test_setup_no_discover_flag_is_parsed():
    args = build_parser().parse_args(["setup", "--no-discover"])
    assert args.no_discover is True
    default = build_parser().parse_args(["setup"])
    assert default.no_discover is False
