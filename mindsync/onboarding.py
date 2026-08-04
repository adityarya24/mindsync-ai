"""Safe CLI discovery, MCP registration, setup, and doctor support."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from mindsync import __version__
from mindsync.dispatch.adapters import load_adapters
from mindsync.dispatch.proc import resolve_bin, spawn_spec
from mindsync.orchestration import OrchestrationPolicy, load_policy, policy_path, save_policy
from mindsync.storage import atomic_private_write


@dataclass(frozen=True)
class CliSpec:
    name: str
    bin: str
    list_args: tuple[str, ...] | None
    add_style: str | None
    remove_args: tuple[str, ...] | None = None


CLI_SPECS: dict[str, CliSpec] = {
    "codex": CliSpec("codex", "codex", ("mcp", "list", "--json"), "codex", ("mcp", "remove", "mindsync")),
    "claude": CliSpec("claude", "claude", ("mcp", "list"), "claude", ("mcp", "remove", "--scope", "user", "mindsync")),
    "gemini": CliSpec("gemini", "gemini", ("mcp", "list"), "gemini", ("mcp", "remove", "mindsync")),
    "grok": CliSpec("grok", "grok", ("mcp", "list", "--json"), "grok", ("mcp", "remove", "mindsync")),
    "cursor": CliSpec("cursor", "cursor-agent", ("mcp", "list"), "cursor-json"),
    # Antigravity and Gemini CLI are backends in one Google/Gemini family. The
    # current AGY CLI has no MCP-management command, so Gemini CLI is the host.
    "agy": CliSpec("agy", "agy", None, None),
}


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[str, list[str]], CommandResult]
Resolver = Callable[[str], str | None]


def _run_command(resolved_bin: str, args: list[str]) -> CommandResult:
    spec = spawn_spec(resolved_bin, args)
    try:
        result = subprocess.run(
            [spec["bin"], *spec["args"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(1, stderr=str(exc))
    return CommandResult(result.returncode, result.stdout or "", result.stderr or "")


def _server_env(cli_name: str) -> dict[str, str]:
    env = {"MINDSYNC_CALLER_CLI": cli_name}
    explicit_home = os.environ.get("MINDSYNC_HOME")
    if explicit_home:
        env["MINDSYNC_HOME"] = explicit_home
    return env


def _env_args(flag: str, values: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in values.items():
        args.extend([flag, f"{key}={value}"])
    return args


def registration_args(cli_name: str, python_exe: str | None = None) -> list[str]:
    python = python_exe or sys.executable
    env = _server_env(cli_name)
    if cli_name == "codex":
        return ["mcp", "add", *_env_args("--env", env), "mindsync", "--", python, "-m", "mindsync.server"]
    if cli_name == "claude":
        return ["mcp", "add", "--scope", "user", *_env_args("-e", env), "mindsync", "--", python, "-m", "mindsync.server"]
    if cli_name == "gemini":
        return [
            "mcp", "add", "--scope", "user", "--transport", "stdio",
            *_env_args("-e", env),
            "--description", "MindSync multi-agent orchestration",
            "mindsync", python, "-m", "mindsync.server",
        ]
    if cli_name == "grok":
        return ["mcp", "add", "--scope", "user", *_env_args("-e", env), "mindsync", "--", python, "-m", "mindsync.server"]
    raise ValueError(f"CLI '{cli_name}' does not support command-based MCP registration")


def _json_has_mindsync(text: str) -> bool:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    if isinstance(data, dict):
        if "mindsync" in data or "mindsync" in (data.get("mcpServers") or {}):
            return True
        servers = data.get("servers")
        if isinstance(servers, list):
            return any(
                isinstance(item, dict) and item.get("name") == "mindsync" for item in servers
            )
    if isinstance(data, list):
        return any(isinstance(item, dict) and item.get("name") == "mindsync" for item in data)
    return False


def _text_has_mindsync(text: str) -> bool:
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return bool(re.search(r"(?im)^\s*(?:[-*○●✓✔!]\s*)?mindsync(?:\s|:|$)", clean))


def cursor_config_path(user_home: Path | None = None) -> Path:
    return (user_home or Path.home()) / ".cursor" / "mcp.json"


def cursor_is_configured(user_home: Path | None = None) -> bool:
    path = cursor_config_path(user_home)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return "mindsync" in (data.get("mcpServers") or {})


def _cursor_config_state(user_home: Path | None = None) -> tuple[bool, str | None]:
    path = cursor_config_path(user_home)
    if not path.is_file():
        return False, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"Invalid JSON at {path}: {exc}"
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return False, f"mcpServers in {path} is not an object"
    return "mindsync" in (servers or {}), None


def cli_status(
    cli_name: str,
    *,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
) -> dict[str, Any]:
    if cli_name not in CLI_SPECS:
        raise ValueError(f"Unknown CLI '{cli_name}'")
    spec = CLI_SPECS[cli_name]
    resolved = resolver(spec.bin)
    if not resolved:
        return {"cli": cli_name, "installed": False, "supported": spec.add_style is not None, "configured": False}
    if spec.add_style is None:
        detail = "Installed CLI exposes no supported MCP registration surface."
        if cli_name == "agy":
            detail = (
                "Antigravity is a worker backend in the Gemini/Antigravity family; "
                "this AGY CLI has no MCP registration command. Configure the Gemini CLI host."
            )
        return {
            "cli": cli_name,
            "installed": True,
            "supported": False,
            "configured": False,
            "worker_only": cli_name == "agy",
            "detail": detail,
        }
    if spec.add_style == "cursor-json":
        configured, detail = _cursor_config_state(user_home)
        return {
            "cli": cli_name,
            "installed": True,
            "supported": True,
            "configured": configured,
            "detail": detail,
        }
    result = runner(resolved, list(spec.list_args or ()))
    output = f"{result.stdout}\n{result.stderr}"
    configured = _json_has_mindsync(result.stdout) or _text_has_mindsync(output)
    approval_pending = configured and bool(
        re.search(r"(?i)(needs approval|pending approval|folder is untrusted)", output)
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "MCP list command failed"
    elif approval_pending:
        detail = "Configured; client approval or a trusted workspace is still required."
    else:
        detail = None
    return {
        "cli": cli_name,
        "installed": True,
        "supported": True,
        "configured": configured,
        "detail": detail,
    }


def _write_cursor_config(
    *,
    user_home: Path,
    force: bool,
    dry_run: bool,
    python_exe: str,
) -> dict[str, Any]:
    path = cursor_config_path(user_home)
    data: dict[str, Any] = {}
    original_text: str | None = None
    if path.is_file():
        try:
            original_text = path.read_text(encoding="utf-8")
            data = json.loads(original_text)
        except json.JSONDecodeError as exc:
            return {"cli": "cursor", "action": "error", "detail": f"Invalid JSON at {path}: {exc}"}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return {"cli": "cursor", "action": "error", "detail": f"mcpServers in {path} is not an object"}
    if "mindsync" in servers and not force:
        return {"cli": "cursor", "action": "already_configured", "path": str(path)}
    if dry_run:
        return {"cli": "cursor", "action": "would_configure", "path": str(path)}

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_name(f"mcp.{stamp}.{secrets.token_hex(3)}.json.bak")
        atomic_private_write(backup_path, original_text or "")
        backup = str(backup_path)
    servers["mindsync"] = {
        "command": python_exe,
        "args": ["-m", "mindsync.server"],
        "env": _server_env("cursor"),
    }
    atomic_private_write(path, json.dumps(data, indent=2) + "\n")
    return {"cli": "cursor", "action": "configured", "path": str(path), "backup": backup}


def setup(
    *,
    mode: str = "auto",
    cli_names: Iterable[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
    policy_file: Path | None = None,
    python_exe: str | None = None,
) -> dict[str, Any]:
    policy = OrchestrationPolicy(mode=mode)
    if not dry_run:
        save_policy(policy, policy_file)
    selected = list(cli_names or CLI_SPECS.keys())
    unknown = sorted(set(selected) - set(CLI_SPECS))
    if unknown:
        raise ValueError(f"Unknown CLI(s): {', '.join(unknown)}")

    python = python_exe or sys.executable
    home = user_home or Path.home()
    actions: list[dict[str, Any]] = []
    for name in selected:
        status = cli_status(name, runner=runner, resolver=resolver, user_home=home)
        if not status["installed"]:
            actions.append({"cli": name, "action": "not_installed"})
            continue
        if not status["supported"]:
            action = "worker_only" if status.get("worker_only") else "unsupported"
            actions.append({"cli": name, "action": action, "detail": status.get("detail")})
            continue
        if name == "cursor":
            actions.append(
                _write_cursor_config(
                    user_home=home,
                    force=force,
                    dry_run=dry_run,
                    python_exe=python,
                )
            )
            continue
        if status.get("detail") and not status["configured"]:
            actions.append({"cli": name, "action": "error", "detail": status["detail"]})
            continue
        if status["configured"] and not force:
            actions.append({"cli": name, "action": "already_configured"})
            continue
        spec = CLI_SPECS[name]
        resolved = resolver(spec.bin)
        if dry_run:
            actions.append({"cli": name, "action": "would_configure", "command": registration_args(name, python)})
            continue
        if force and status["configured"] and spec.remove_args:
            removed = runner(resolved or spec.bin, list(spec.remove_args))
            if removed.returncode != 0:
                actions.append({"cli": name, "action": "error", "detail": removed.stderr.strip() or "remove failed"})
                continue
        added = runner(resolved or spec.bin, registration_args(name, python))
        if added.returncode == 0:
            actions.append({"cli": name, "action": "configured"})
        else:
            actions.append({"cli": name, "action": "error", "detail": added.stderr.strip() or added.stdout.strip() or "registration failed"})
    return {
        "ok": not any(action["action"] == "error" for action in actions),
        "version": __version__,
        "mode": policy.mode,
        "dry_run": dry_run,
        "policy_file": str(policy_file or policy_path()),
        "actions": actions,
    }


def doctor(
    *,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
    policy_file: Path | None = None,
) -> dict[str, Any]:
    policy_error = None
    try:
        policy = load_policy(policy_file)
        policy_data = policy.model_dump()
    except ValueError as exc:
        policy_error = str(exc)
        policy_data = None
    clis = [
        cli_status(name, runner=runner, resolver=resolver, user_home=user_home)
        for name in CLI_SPECS
    ]
    workers = []
    for adapter in load_adapters().values():
        workers.append({
            "name": adapter.name,
            "display_name": adapter.displayName or adapter.name,
            "family": adapter.family or adapter.name,
            "available": bool(resolver(adapter.bin)),
            "capabilities": adapter.capabilities or ["general"],
        })
    configured_hosts = [item["cli"] for item in clis if item["configured"]]
    available_workers = [item["name"] for item in workers if item["available"]]
    worker_families: dict[str, list[str]] = {}
    for item in workers:
        if item["available"]:
            worker_families.setdefault(item["family"], []).append(item["name"])
    issues = []
    if policy_error:
        issues.append(policy_error)
    if not configured_hosts:
        issues.append("No supported CLI has MindSync configured.")
    if not available_workers:
        issues.append("No dispatch worker CLI is available on PATH.")
    return {
        "ok": not issues,
        "version": __version__,
        "python": sys.executable,
        "policy": policy_data,
        "policy_error": policy_error,
        "configured_hosts": configured_hosts,
        "available_workers": available_workers,
        "available_worker_families": worker_families,
        "issues": issues,
        "clis": clis,
        "workers": workers,
    }
