"""Self-registration and honest roster status for dispatch agents."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mindsync import __version__
from mindsync.dispatch.adapters import load_adapters, upsert_user_agent, user_config_path
from mindsync.dispatch.proc import resolve_bin
from mindsync.dispatch.routing import (
    HEAVY_CAPABILITIES,
    KNOWN_CAPABILITIES,
    normalize_capabilities,
)
from mindsync.onboarding import (
    CLI_SPECS,
    CommandRunner,
    CommandResult,
    Resolver,
    _run_command,
    cli_status,
    registration_args,
)

_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def resolve_register_capabilities(
    values: list[str] | None,
    *,
    confirm: bool = False,
) -> list[str]:
    """Return operator-owned capabilities, requiring --confirm for heavy tags."""
    requested = normalize_capabilities(values)
    unknown = [item for item in requested if item not in KNOWN_CAPABILITIES]
    if unknown:
        known = ", ".join(sorted(KNOWN_CAPABILITIES))
        raise ValueError(f"Unknown capability: {', '.join(unknown)}. Known: {known}")
    heavy = [item for item in requested if item in HEAVY_CAPABILITIES]
    if heavy and not confirm:
        raise ValueError(
            "Capability "
            + ", ".join(heavy)
            + " changes routing trust and requires --confirm"
        )
    applied = ["general"]
    for item in requested:
        if item not in applied:
            applied.append(item)
    return applied


def _match_mcp_cli(name: str, bin_name: str) -> str | None:
    bin_leaf = Path(bin_name).name
    for cli_name, spec in CLI_SPECS.items():
        if spec.add_style is None:
            continue
        if name == cli_name or bin_leaf == spec.bin or bin_name == spec.bin:
            return cli_name
    return None


def _capability_weights(capabilities: list[str]) -> dict[str, int]:
    weights = {"coding": 80, "review": 70, "testing": 70, "debugging": 70, "repository": 70}
    return {cap: weights.get(cap, 50) for cap in capabilities if cap != "general"}


def _ensure_mcp(
    cli_name: str,
    *,
    dry_run: bool,
    force: bool,
    runner: CommandRunner,
    resolver: Resolver,
    user_home: Path | None,
    python_exe: str | None,
) -> dict[str, Any]:
    status = cli_status(cli_name, runner=runner, resolver=resolver, user_home=user_home)
    if not status["installed"]:
        return {
            "action": "skipped",
            "cli": cli_name,
            "detail": f"{cli_name} is not installed; MCP was not registered",
        }
    if not status["supported"]:
        return {
            "action": "skipped",
            "cli": cli_name,
            "detail": status.get("detail") or "no MCP-management command",
        }
    if status.get("detail") and not status["configured"]:
        return {"action": "error", "cli": cli_name, "detail": status["detail"]}
    if status["configured"] and not force:
        return {"action": "already_configured", "cli": cli_name}
    spec = CLI_SPECS[cli_name]
    resolved = resolver(spec.bin)
    args = registration_args(cli_name, python_exe)
    if dry_run:
        return {"action": "would_configure", "cli": cli_name, "command": args}
    if force and status["configured"] and spec.remove_args:
        removed = runner(resolved or spec.bin, list(spec.remove_args))
        if removed.returncode != 0:
            return {
                "action": "error",
                "cli": cli_name,
                "detail": removed.stderr.strip() or "remove failed",
            }
    added = runner(resolved or spec.bin, args)
    if added.returncode != 0:
        return {
            "action": "error",
            "cli": cli_name,
            "detail": added.stderr.strip() or added.stdout.strip() or "registration failed",
        }
    return {"action": "configured", "cli": cli_name}


def _verify_binary(bin_name: str, runner: CommandRunner, resolver: Resolver) -> dict[str, Any]:
    resolved = resolver(bin_name)
    if not resolved:
        return {"binary_present": False, "version": None, "detail": f"'{bin_name}' is not on PATH"}
    result: CommandResult = runner(resolved, ["--version"])
    output = (result.stdout or result.stderr or "").strip()
    line = output.splitlines()[0] if output else None
    if result.returncode != 0 and not line:
        return {
            "binary_present": True,
            "version": None,
            "detail": result.stderr.strip() or "--version failed",
        }
    return {"binary_present": True, "version": line, "detail": None}


def register_agent(
    *,
    name: str,
    bin_name: str,
    capabilities: list[str] | None = None,
    confirm: bool = False,
    display_name: str | None = None,
    family: str | None = None,
    routing_priority: int = 40,
    dry_run: bool = False,
    force: bool = False,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
    python_exe: str | None = None,
) -> dict[str, Any]:
    """Land an agent in the user roster and, when possible, as an MCP host."""
    agent_name = name.strip().lower()
    if not _AGENT_NAME_RE.fullmatch(agent_name):
        raise ValueError(
            "agent name must be lowercase letters, digits, and hyphens, starting with a letter"
        )
    binary = bin_name.strip()
    if not binary:
        raise ValueError("bin is required")
    if not 0 <= routing_priority <= 100:
        raise ValueError("routing priority must be between 0 and 100")

    adapters = load_adapters()
    user_names: set[str] = set()
    existing_path = user_config_path()
    if existing_path.is_file():
        try:
            from mindsync.dispatch.adapters import _read_user_config

            user_names = {
                str(item.get("name"))
                for item in _read_user_config(existing_path).get("agents") or []
                if isinstance(item, dict) and item.get("name")
            }
        except ValueError:
            user_names = set()
    bundled = adapters.get(agent_name)
    if bundled is not None and agent_name not in user_names and not force:
        if bundled.bin != binary:
            raise ValueError(
                f"'{agent_name}' is a bundled agent with bin '{bundled.bin}'. "
                "Use --force to overlay it in the user roster."
            )
        roster = {
            "action": "already_configured",
            "path": str(existing_path),
            "agent": bundled.model_dump(),
        }
        applied_caps = bundled.capabilities or ["general"]
    else:
        applied_caps = resolve_register_capabilities(capabilities, confirm=confirm)
        entry = {
            "name": agent_name,
            "bin": binary,
            "displayName": (display_name or agent_name).strip(),
            "family": (family or agent_name).strip().lower(),
            "input": "stdin",
            "capabilities": applied_caps,
            "capabilityWeights": _capability_weights(applied_caps),
            "routingPriority": routing_priority,
        }
        roster = upsert_user_agent(entry, force=force, dry_run=dry_run)

    matched_cli = _match_mcp_cli(agent_name, binary)
    if matched_cli is None:
        mcp = {
            "action": "skipped",
            "cli": None,
            "detail": (
                "no MCP-management command for this CLI; dispatch registration "
                "still landed in the user roster"
            ),
        }
    else:
        mcp = _ensure_mcp(
            matched_cli,
            dry_run=dry_run,
            force=force,
            runner=runner,
            resolver=resolver,
            user_home=user_home,
            python_exe=python_exe,
        )

    verify = _verify_binary(binary, runner, resolver)
    mcp_installed = False
    if matched_cli is not None and not dry_run:
        mcp_installed = bool(
            cli_status(
                matched_cli, runner=runner, resolver=resolver, user_home=user_home
            ).get("configured")
        )
    elif mcp.get("action") in {"already_configured", "configured", "would_configure"}:
        mcp_installed = mcp.get("action") != "error"

    verify["mcp_installed"] = mcp_installed
    verify["routable"] = bool(verify["binary_present"]) and roster.get("action") != "error"
    ok = roster.get("action") != "error" and mcp.get("action") != "error"
    return {
        "ok": ok,
        "version": __version__,
        "dry_run": dry_run,
        "roster": roster,
        "mcp": mcp,
        "verify": verify,
        "capabilities": applied_caps,
        "path": roster.get("path") or str(user_config_path()),
    }


def describe_agents(
    *,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
) -> list[dict[str, Any]]:
    """Roster rows: registered, binary present, MCP installed, routable."""
    user_names: set[str] = set()
    path = user_config_path()
    if path.is_file():
        try:
            from mindsync.dispatch.adapters import _read_user_config

            user_names = {
                str(item.get("name"))
                for item in _read_user_config(path).get("agents") or []
                if isinstance(item, dict) and item.get("name")
            }
        except ValueError:
            user_names = set()

    rows: list[dict[str, Any]] = []
    for adapter in load_adapters().values():
        binary_present = bool(resolver(adapter.bin))
        matched = _match_mcp_cli(adapter.name, adapter.bin)
        mcp_installed = False
        mcp_detail = "no MCP-management command"
        if matched is None:
            mcp_detail = "no MCP-management command for this CLI"
        else:
            status = cli_status(
                matched, runner=runner, resolver=resolver, user_home=user_home
            )
            if not status["installed"]:
                mcp_detail = f"{matched} is not installed"
            elif not status["supported"]:
                mcp_detail = status.get("detail") or "MCP registration unsupported"
            elif status["configured"]:
                mcp_installed = True
                mcp_detail = status.get("detail") or "configured"
            else:
                mcp_detail = status.get("detail") or "installed, not configured"
        rows.append(
            {
                "name": adapter.name,
                "display_name": adapter.displayName or adapter.name,
                "family": adapter.family or adapter.name,
                "bin": adapter.bin,
                "source": "user" if adapter.name in user_names else "preset",
                "binary_present": binary_present,
                "mcp_installed": mcp_installed,
                "mcp_detail": mcp_detail,
                "routable": binary_present,
                "capabilities": adapter.capabilities or ["general"],
                "routing_priority": adapter.routingPriority,
            }
        )
    rows.sort(key=lambda item: item["name"])
    return rows
