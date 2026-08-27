"""Self-registration and honest roster status for dispatch agents."""

from __future__ import annotations

import os
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
    _json_has_mindsync,
    _run_command,
    _text_has_mindsync,
    cli_status,
    generic_mcp_registration_args,
    registration_args,
)

_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_CLI_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
# Distinctive enough to match anywhere in a name.
_NAME_HINT = re.compile(
    r"(agent|aider|claude|codex|continue|copilot|crush|cursor|"
    r"droid|gemini|goose|grok|hermes|opencode|openclaw|qwen|windsurf)"
)
# Short, generic tokens that are real CLI names but common substrings. Anchored
# to name segments so 'amp' matches amp and amp-cli, not uclampset or sg_timestamp.
_NAME_HINT_WORD = re.compile(r"(?:^|[-_])(amp|gpt|llm)(?:$|[-_])")
_SEED_BINS = frozenset({
    "aider",
    "amp",
    "claude",
    "codex",
    "continue",
    "copilot",
    "crush",
    "cursor-agent",
    "droid",
    "gemini",
    "goose",
    "grok",
    "hermes",
    "opencode",
    "openclaw",
    "pi",
    "qwen",
    "qwen-code",
    "windsurf",
})
_DENYLIST = frozenset({
    "aws", "az", "bash", "bat", "cargo", "cat", "choco", "cmake", "cmd", "code",
    "conda", "curl", "dir", "docker", "explorer", "fd", "find", "fish", "gcc",
    "gcloud", "gh", "git", "go", "grep", "java", "javac", "kubectl", "less",
    "ls", "make", "mindsync", "mindsync-codex-hook", "mindsync-dispatch",
    "more", "node", "notepad", "npm", "npx", "nvim", "pip", "pip3", "pipx",
    "powershell", "pwsh", "pytest", "python", "python3", "pythonw", "rg",
    "ruff", "rustc", "scoop", "scp", "sh", "ssh", "tar", "taskkill", "tasklist",
    "uv", "uvx", "vim", "wget", "where", "which", "winget", "zsh", "zip",
    "unzip",
    # System daemons and tools whose names end in -agent/-client. Never probe
    # these: several are password prompters or spawn a background daemon, and
    # one of them restarts a fleet of services.
    "dirmngr-client", "fail2ban-client", "gpg-agent", "gpg-connect-agent",
    "gpg-wks-client", "pam_timestamp_check", "pkttyagent", "ssh-agent",
    "systemd-tty-ask-password-agent",
})
_SKIP_DIR_PARTS = ("system32", "syswow64", "windowsapps", "systemapps")
_MAX_DISCOVERED = 40


def _leaf_cli_name(filename: str) -> str | None:
    name = filename
    if os.name == "nt":
        stem, ext = os.path.splitext(filename)
        if ext.lower() not in {".exe", ".cmd", ".bat", ".com"}:
            return None
        name = stem
    name = name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name not in _DENYLIST and _CLI_NAME_RE.fullmatch(name):
        return name
    return None


def path_cli_names() -> dict[str, str]:
    """Return unique PATH executable names that are not obviously system tools."""
    found: dict[str, str] = {}
    path_env = os.environ.get("PATH", "")
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        lowered = directory.replace("\\", "/").lower()
        if any(part in lowered for part in _SKIP_DIR_PARTS):
            continue
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            name = _leaf_cli_name(entry)
            if not name or name in found:
                continue
            found[name] = str(Path(directory) / entry)
    return found


def looks_like_agent_cli(name: str) -> bool:
    if name in _DENYLIST:
        return False
    if name in _SEED_BINS:
        return True
    return bool(_NAME_HINT.search(name) or _NAME_HINT_WORD.search(name))


def probe_mcp_capable(bin_name: str, runner: CommandRunner, resolver: Resolver) -> bool:
    resolved = resolver(bin_name)
    if not resolved:
        return False
    for args in (["mcp", "list", "--json"], ["mcp", "list"]):
        result = runner(resolved, list(args))
        text = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0:
            return True
        if _json_has_mindsync(result.stdout) or _text_has_mindsync(text):
            return True
        lowered = text.lower()
        if "mcp" in lowered and "add" in lowered and "unknown" not in lowered:
            return True
    return False


def _candidate_clis(
    *,
    resolver: Resolver,
    path_bins: dict[str, str] | None,
) -> list[tuple[str, str]]:
    """PATH names that look like an agent CLI and are not already known."""
    adapters = load_adapters()
    known_bins = {adapter.bin.lower() for adapter in adapters.values()}
    known_names = {adapter.name.lower() for adapter in adapters.values()}
    known_names.update(CLI_SPECS)
    known_bins.update(spec.bin.lower() for spec in CLI_SPECS.values())

    if path_bins is None:
        names = path_cli_names()
        for seed in _SEED_BINS:
            if seed not in names and resolver(seed):
                names[seed] = seed
    else:
        names = dict(path_bins)

    candidates: list[tuple[str, str]] = []
    for name, bin_name in sorted(names.items()):
        if name in known_names or name in known_bins:
            continue
        if not looks_like_agent_cli(name):
            continue
        if not resolver(name) and not resolver(bin_name):
            continue
        use_bin = name if resolver(name) else bin_name
        if not _AGENT_NAME_RE.fullmatch(name.replace("_", "-")):
            continue
        candidates.append((name, use_bin))
    return candidates


def discover_agent_clis(
    *,
    resolver: Resolver = resolve_bin,
    runner: CommandRunner = _run_command,
    path_bins: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Find known agent CLIs on PATH that are not already bundled presets.

    Only names on the seed allowlist are returned, because deciding what a
    binary is requires running it, and running an unrecognised binary is not a
    safe probe. On a plain Linux host the name shapes alone match things like
    ssh-agent, pkttyagent and *-fleet-restart scripts; probing those spawns
    daemons, blocks on a TTY password prompt, or restarts live services.
    Everything else that merely looks agent-ish is reported by
    suggest_unknown_clis() for the operator to add deliberately.
    """
    discovered: list[dict[str, Any]] = []
    for name, use_bin in _candidate_clis(resolver=resolver, path_bins=path_bins):
        if len(discovered) >= _MAX_DISCOVERED:
            break
        if name not in _SEED_BINS:
            continue
        discovered.append(
            {
                "name": name.replace("_", "-"),
                "bin": name if resolver(name) else Path(use_bin).stem.lower(),
                "mcp_capable": probe_mcp_capable(use_bin, runner, resolver),
            }
        )
    return discovered


def suggest_unknown_clis(
    *,
    resolver: Resolver = resolve_bin,
    path_bins: dict[str, str] | None = None,
) -> list[str]:
    """Agent-ish PATH names that are not recognised, never executed."""
    return [
        name.replace("_", "-")
        for name, _ in _candidate_clis(resolver=resolver, path_bins=path_bins)
        if name not in _SEED_BINS
    ][:_MAX_DISCOVERED]


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


def _mcp_already_configured(resolved: str, runner: CommandRunner) -> bool:
    for args in (["mcp", "list", "--json"], ["mcp", "list"]):
        result = runner(resolved, list(args))
        text = f"{result.stdout}\n{result.stderr}"
        if _json_has_mindsync(result.stdout) or _text_has_mindsync(text):
            return True
    return False


def _ensure_generic_mcp(
    name: str,
    bin_name: str,
    *,
    dry_run: bool,
    force: bool,
    runner: CommandRunner,
    resolver: Resolver,
    python_exe: str | None,
) -> dict[str, Any]:
    resolved = resolver(bin_name)
    if not resolved:
        return {
            "action": "skipped",
            "cli": name,
            "detail": f"'{bin_name}' is not on PATH; MCP was not registered",
        }
    if not probe_mcp_capable(bin_name, runner, resolver):
        return {
            "action": "skipped",
            "cli": name,
            "detail": "no MCP-management command for this CLI; dispatch registration still landed in the user roster",
        }
    if _mcp_already_configured(resolved, runner) and not force:
        return {"action": "already_configured", "cli": name}
    args = generic_mcp_registration_args(name, python_exe)
    if dry_run:
        return {"action": "would_configure", "cli": name, "command": args}
    if force:
        runner(resolved, ["mcp", "remove", "mindsync"])
    added = runner(resolved, args)
    if added.returncode != 0:
        return {
            "action": "error",
            "cli": name,
            "detail": added.stderr.strip() or added.stdout.strip() or "generic mcp add failed",
        }
    return {"action": "configured", "cli": name}


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
    if matched_cli is not None:
        mcp = _ensure_mcp(
            matched_cli,
            dry_run=dry_run,
            force=force,
            runner=runner,
            resolver=resolver,
            user_home=user_home,
            python_exe=python_exe,
        )
    else:
        mcp = _ensure_generic_mcp(
            agent_name,
            binary,
            dry_run=dry_run,
            force=force,
            runner=runner,
            resolver=resolver,
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
            resolved = resolver(adapter.bin)
            if resolved and _mcp_already_configured(resolved, runner):
                mcp_installed = True
                mcp_detail = "configured"
            elif probe_mcp_capable(adapter.bin, runner, resolver):
                mcp_detail = "installed, not configured"
            else:
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


def register_discovered_clis(
    *,
    dry_run: bool = False,
    force: bool = False,
    runner: CommandRunner = _run_command,
    resolver: Resolver = resolve_bin,
    user_home: Path | None = None,
    python_exe: str | None = None,
    path_bins: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Register PATH-discovered agent CLIs into the user roster during setup."""
    actions: list[dict[str, Any]] = []
    for item in discover_agent_clis(
        resolver=resolver, runner=runner, path_bins=path_bins
    ):
        try:
            result = register_agent(
                name=item["name"],
                bin_name=item["bin"],
                capabilities=["coding"],
                runner=runner,
                resolver=resolver,
                user_home=user_home,
                python_exe=python_exe,
                dry_run=dry_run,
                force=force,
            )
        except ValueError as exc:
            actions.append(
                {
                    "cli": item["name"],
                    "action": "error",
                    "detail": f"PATH discovery failed: {exc}",
                }
            )
            continue
        mcp = result.get("mcp") or {}
        detail = (
            f"PATH discovery; roster={result['roster']['action']}; "
            f"mcp={mcp.get('action')}"
        )
        if mcp.get("detail"):
            detail = f"{detail} ({mcp['detail']})"
        actions.append(
            {
                "cli": item["name"],
                "action": result["roster"]["action"],
                "detail": detail,
            }
        )
    # Surface rather than guess: an unrecognised binary is only a name until
    # something runs it, and setup is not the place to run it.
    for name in suggest_unknown_clis(resolver=resolver, path_bins=path_bins):
        actions.append(
            {
                "cli": name,
                "action": "suggested",
                "detail": (
                    f"looks like an agent CLI but is not recognised; "
                    f"add it with 'mindsync register {name}'"
                ),
            }
        )
    return actions
