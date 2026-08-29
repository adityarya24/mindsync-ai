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
from mindsync.dispatch.limits import reactive_reset_source
from mindsync.dispatch.proc import resolve_bin, spawn_spec
from mindsync.dispatch.usage.config import UsageConfig, load_usage_config
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
    # OpenCode's `mcp add` is interactive; we write ~/.config/opencode/opencode.jsonc.
    "opencode": CliSpec("opencode", "opencode", None, "opencode-json"),
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
    return generic_mcp_registration_args(cli_name, python)


def generic_mcp_registration_args(cli_name: str, python_exe: str | None = None) -> list[str]:
    """Best-effort `mcp add` used when the CLI is not in CLI_SPECS."""
    python = python_exe or sys.executable
    env = _server_env(cli_name)
    return [
        "mcp",
        "add",
        "--scope",
        "user",
        *_env_args("-e", env),
        "mindsync",
        "--",
        python,
        "-m",
        "mindsync.server",
    ]


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


def opencode_config_dir(user_home: Path | None = None) -> Path:
    return (user_home or Path.home()) / ".config" / "opencode"


def opencode_config_path(user_home: Path | None = None) -> Path:
    directory = opencode_config_dir(user_home)
    jsonc = directory / "opencode.jsonc"
    json_file = directory / "opencode.json"
    if jsonc.is_file():
        return jsonc
    if json_file.is_file():
        return json_file
    return jsonc


def _opencode_mcp_map(data: dict[str, Any]) -> dict[str, Any] | None:
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return None
    servers = mcp.get("servers")
    if isinstance(servers, dict):
        return servers
    return mcp


def _opencode_has_mindsync(data: dict[str, Any]) -> bool:
    bucket = _opencode_mcp_map(data)
    return bool(bucket) and "mindsync" in bucket


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


def _opencode_config_state(user_home: Path | None = None) -> tuple[bool, str | None]:
    path = opencode_config_path(user_home)
    if not path.is_file():
        return False, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"Invalid JSON at {path}: {exc}"
    if not isinstance(data, dict):
        return False, f"OpenCode config {path} is not an object"
    mcp = data.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        return False, f"mcp in {path} is not an object"
    return _opencode_has_mindsync(data), None


def cli_status(
    cli_name: str,
    *,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
    probe_hosts: bool = True,
) -> dict[str, Any]:
    if cli_name not in CLI_SPECS:
        raise ValueError(f"Unknown CLI '{cli_name}'")
    spec = CLI_SPECS[cli_name]
    resolved = resolver(spec.bin)
    if not resolved:
        return {"cli": cli_name, "installed": False, "supported": spec.add_style is not None, "configured": False}
    if not probe_hosts and spec.add_style not in (None, "cursor-json", "opencode-json"):
        # Reporting registration means running `<cli> mcp list`, which starts the
        # host CLI (and everything it loads, e.g. a bound chat-channel plugin).
        # Config-file hosts (cursor/opencode) are read from disk and stay cheap.
        return {
            "cli": cli_name,
            "installed": True,
            "supported": True,
            "configured": False,
            "detail": "not probed",
        }
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
    if spec.add_style == "opencode-json":
        configured, detail = _opencode_config_state(user_home)
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


def _write_opencode_config(
    *,
    user_home: Path,
    force: bool,
    dry_run: bool,
    python_exe: str,
) -> dict[str, Any]:
    path = opencode_config_path(user_home)
    data: dict[str, Any] = {}
    original_text: str | None = None
    if path.is_file():
        try:
            original_text = path.read_text(encoding="utf-8")
            parsed = json.loads(original_text)
        except json.JSONDecodeError as exc:
            return {"cli": "opencode", "action": "error", "detail": f"Invalid JSON at {path}: {exc}"}
        if not isinstance(parsed, dict):
            return {"cli": "opencode", "action": "error", "detail": f"{path} is not an object"}
        data = parsed
        if _opencode_has_mindsync(data) and not force:
            return {"cli": "opencode", "action": "already_configured", "path": str(path)}
    if dry_run:
        return {"cli": "opencode", "action": "would_configure", "path": str(path)}

    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        return {"cli": "opencode", "action": "error", "detail": f"mcp in {path} is not an object"}
    bucket = mcp["servers"] if isinstance(mcp.get("servers"), dict) else mcp
    bucket["mindsync"] = {
        "type": "local",
        "command": [python_exe, "-m", "mindsync.server"],
        "enabled": True,
        "environment": _server_env("opencode"),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_name(f"{path.stem}.{stamp}.{secrets.token_hex(3)}{path.suffix}.bak")
        atomic_private_write(backup_path, original_text or "")
        backup = str(backup_path)
    atomic_private_write(path, json.dumps(data, indent=2) + "\n")
    return {"cli": "opencode", "action": "configured", "path": str(path), "backup": backup}


def configure_json_host(
    cli_name: str,
    *,
    user_home: Path,
    force: bool,
    dry_run: bool,
    python_exe: str,
) -> dict[str, Any]:
    if cli_name == "cursor":
        return _write_cursor_config(
            user_home=user_home, force=force, dry_run=dry_run, python_exe=python_exe
        )
    if cli_name == "opencode":
        return _write_opencode_config(
            user_home=user_home, force=force, dry_run=dry_run, python_exe=python_exe
        )
    raise ValueError(f"CLI '{cli_name}' has no JSON host writer")


CODEX_HOOK_COMMAND = "mindsync-codex-hook"
CODEX_HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd")


def codex_hooks_path(user_home: Path | None = None) -> Path:
    return (user_home or Path.home()) / ".codex" / "hooks.json"


def _mindsync_hook_command(event: str) -> dict[str, Any]:
    hook: dict[str, Any] = {
        "type": "command",
        "command": CODEX_HOOK_COMMAND,
        "commandWindows": CODEX_HOOK_COMMAND,
        "timeout": 3,
    }
    if event == "SessionStart":
        hook["additionalContextLimit"] = 8000
    return hook


def _mindsync_hook_block(event: str) -> dict[str, Any]:
    block: dict[str, Any] = {"hooks": [_mindsync_hook_command(event)]}
    if event == "SessionStart":
        block["matcher"] = "startup|resume|clear|compact"
    return block


def bundled_codex_hooks_config() -> dict[str, Any]:
    return {
        "description": "Privacy-safe standalone MindSync lifecycle for Codex sessions.",
        "hooks": {event: [_mindsync_hook_block(event)] for event in CODEX_HOOK_EVENTS},
    }


def _value_mentions_codex_hook(value: Any) -> bool:
    if isinstance(value, str):
        return CODEX_HOOK_COMMAND in value
    if isinstance(value, dict):
        return any(_value_mentions_codex_hook(item) for item in value.values())
    if isinstance(value, list):
        return any(_value_mentions_codex_hook(item) for item in value)
    return False


def _hooks_cover_mindsync(data: dict[str, Any]) -> bool:
    root = data.get("hooks")
    if not isinstance(root, dict):
        return False
    return all(_value_mentions_codex_hook(root.get(event)) for event in CODEX_HOOK_EVENTS)


def _file_has_mindsync_hooks(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and _hooks_cover_mindsync(data)


def _ensure_mindsync_hook_events(data: dict[str, Any]) -> bool:
    """Merge MindSync hook blocks into existing Codex hooks. Return True if changed."""
    root = data.setdefault("hooks", {})
    if not isinstance(root, dict):
        raise ValueError("hooks is not an object")
    changed = False
    if not data.get("description"):
        data["description"] = bundled_codex_hooks_config()["description"]
        changed = True
    for event in CODEX_HOOK_EVENTS:
        entries = root.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} is not an array")
        if _value_mentions_codex_hook(entries):
            continue
        entries.append(_mindsync_hook_block(event))
        changed = True
    return changed


def _write_codex_hooks(
    *,
    user_home: Path,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    path = codex_hooks_path(user_home)
    original_text: str | None = None
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            original_text = path.read_text(encoding="utf-8")
            parsed = json.loads(original_text)
        except json.JSONDecodeError as exc:
            return {
                "cli": "codex-hook",
                "action": "error",
                "detail": f"Invalid JSON at {path}: {exc}",
            }
        if not isinstance(parsed, dict):
            return {
                "cli": "codex-hook",
                "action": "error",
                "detail": f"hooks file {path} is not an object",
            }
        data = parsed
        if _hooks_cover_mindsync(data) and not force:
            return {"cli": "codex-hook", "action": "already_configured", "path": str(path)}
    if dry_run:
        return {"cli": "codex-hook", "action": "would_configure", "path": str(path)}

    try:
        changed = _ensure_mindsync_hook_events(data)
    except ValueError as exc:
        return {"cli": "codex-hook", "action": "error", "detail": f"{path}: {exc}"}
    if not changed and _hooks_cover_mindsync(data):
        return {"cli": "codex-hook", "action": "already_configured", "path": str(path)}

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_name(f"hooks.{stamp}.{secrets.token_hex(3)}.json.bak")
        atomic_private_write(backup_path, original_text or "")
        backup = str(backup_path)
    atomic_private_write(path, json.dumps(data, indent=2) + "\n")
    return {
        "cli": "codex-hook",
        "action": "configured",
        "path": str(path),
        "backup": backup,
    }


def _codex_hooks_status(
    user_home: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    home = user_home or Path.home()
    user_path = codex_hooks_path(home)
    project_path = (cwd / ".codex" / "hooks.json") if cwd is not None else None
    paths: list[str] = []
    if _file_has_mindsync_hooks(user_path):
        paths.append(str(user_path))
    if project_path is not None and _file_has_mindsync_hooks(project_path):
        paths.append(str(project_path))
    return {
        "configured": bool(paths),
        "user_path": str(user_path),
        "project_path": str(project_path) if project_path is not None else None,
        "paths": paths,
    }


def _memory_doctor_report(
    *,
    user_home: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    workspace = cwd if cwd is not None else Path.cwd()
    db_error = None
    stats: dict[str, Any] | None = None
    try:
        from mindsync.memory import memory_stats

        stats = memory_stats()
    except Exception as exc:
        db_error = str(exc)

    git_project = None
    try:
        from mindsync.dispatch.memory_lifecycle import _infer_git_project_key

        if workspace.is_dir():
            git_project = _infer_git_project_key(str(workspace))
    except Exception:
        git_project = None

    return {
        "ok": db_error is None,
        "db_open": db_error is None,
        "db_error": db_error,
        "sessions": None if stats is None else stats.get("total_sessions"),
        "facts": None if stats is None else stats.get("total_facts"),
        "db_size_bytes": None if stats is None else stats.get("db_size_bytes"),
        "git_project": git_project,
        "codex_hooks": _codex_hooks_status(user_home, workspace if workspace.is_dir() else None),
    }


def setup(
    *,
    mode: str = "auto",
    cli_names: Iterable[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    install_hooks: bool = True,
    discover: bool | None = None,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
    policy_file: Path | None = None,
    python_exe: str | None = None,
    path_bins: dict[str, str] | None = None,
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
        spec = CLI_SPECS[name]
        if spec.add_style in {"cursor-json", "opencode-json"}:
            actions.append(
                configure_json_host(
                    name,
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

    if install_hooks:
        codex_touched = any(
            item.get("cli") == "codex" and item.get("action") != "not_installed"
            for item in actions
        )
        if codex_touched:
            actions.append(
                _write_codex_hooks(user_home=home, force=force, dry_run=dry_run)
            )

    if discover is None:
        discover = cli_names is None
    if discover:
        from mindsync.roster import register_discovered_clis

        actions.extend(
            register_discovered_clis(
                dry_run=dry_run,
                force=force,
                runner=runner,
                resolver=resolver,
                user_home=home,
                python_exe=python,
                path_bins=path_bins,
            )
        )

    return {
        "ok": not any(action["action"] == "error" for action in actions),
        "version": __version__,
        "mode": policy.mode,
        "dry_run": dry_run,
        "policy_file": str(policy_file or policy_path()),
        "actions": actions,
    }


def _usage_mode_for_adapter(
    adapter: Any,
    *,
    usage_config: UsageConfig | None = None,
) -> str:
    config = usage_config or load_usage_config()
    if adapter.usageReader and config.enabled:
        return "preemptive"
    if adapter.quotaErrorPatterns:
        return "reactive-only"
    return "disabled"


def doctor(
    *,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
    policy_file: Path | None = None,
    cwd: Path | None = None,
    probe_hosts: bool = True,
) -> dict[str, Any]:
    policy_error = None
    try:
        policy = load_policy(policy_file)
        policy_data = policy.model_dump()
    except ValueError as exc:
        policy_error = str(exc)
        policy_data = None
    clis = [
        cli_status(
            name, runner=runner, resolver=resolver, user_home=user_home,
            probe_hosts=probe_hosts,
        )
        for name in CLI_SPECS
    ]
    workers = []
    usage_config = load_usage_config()
    for adapter in load_adapters().values():
        workers.append({
            "name": adapter.name,
            "display_name": adapter.displayName or adapter.name,
            "family": adapter.family or adapter.name,
            "available": bool(resolver(adapter.bin)),
            "capabilities": adapter.capabilities or ["general"],
            "usage_mode": _usage_mode_for_adapter(adapter, usage_config=usage_config),
            "usage_reader": adapter.usageReader,
            "reactive_reset": reactive_reset_source(adapter),
            "provider": adapter.family or adapter.name,
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
    memory = _memory_doctor_report(user_home=user_home, cwd=cwd)
    if memory["db_error"]:
        issues.append(f"Session memory database could not be opened: {memory['db_error']}")
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
        "memory": memory,
    }
