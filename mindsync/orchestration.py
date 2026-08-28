"""Persistent orchestration policy shared by MindSync MCP and dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from mindsync.config import settings
from mindsync.storage import atomic_private_write, file_lock


ExecutionMode = Literal["worker", "orchestrator"]


def validate_execution_mode(value: Any) -> ExecutionMode:
    """Validate the execution boundary used by local and remote dispatch.

    The defaulting for legacy payloads happens at the call site so an explicit
    JSON ``null`` is still rejected as an untrusted shape.
    """
    if type(value) is not str or value not in {"worker", "orchestrator"}:
        raise ValueError("execution_mode must be exactly 'worker' or 'orchestrator'")
    return value


OnComplete = Literal["pr", "branch", "none"]


class ProjectPolicy(BaseModel):
    """Per-project overrides, keyed by repository root.

    These live in MindSync's own policy file rather than inside the repository.
    A dispatched agent writes to the repository, so a config file kept there
    would be one an agent could edit to turn its own publishing on.
    """

    onComplete: OnComplete | None = None


class OrchestrationPolicy(BaseModel):
    mode: Literal["auto", "suggest", "off"] = "auto"
    announce: bool = True
    maxParallel: int = Field(default=3, ge=1, le=16)
    avoidHumanFacingAgent: bool = True
    onComplete: OnComplete = "branch"
    projects: dict[str, ProjectPolicy] = Field(default_factory=dict)


def project_key(repo_root: str | Path | None) -> str | None:
    """Normalise a repository root into the key its overrides are stored under.

    Symlinks and case differences must not produce two entries for one repo,
    so the path is resolved and normalised the way the platform compares paths.
    """
    if not repo_root:
        return None
    try:
        resolved = Path(repo_root).expanduser().resolve()
    except OSError:
        resolved = Path(repo_root).expanduser()
    return os.path.normcase(str(resolved))


def project_on_complete(
    repo_root: str | Path | None, policy: OrchestrationPolicy | None = None
) -> OnComplete:
    """The on-complete mode in force for one repository.

    A project override wins over the global default; the environment variable
    is handled by the caller, because it overrides a single run rather than a
    stored setting.
    """
    cfg = policy or load_policy()
    key = project_key(repo_root)
    if key:
        entry = cfg.projects.get(key)
        if entry is not None and entry.onComplete is not None:
            return entry.onComplete
    return cfg.onComplete


def is_worker_process() -> bool:
    return os.environ.get("MINDSYNC_WORKER", "").strip().lower() in {"1", "true", "yes"}


def policy_path() -> Path:
    return settings.orchestration_file


def load_policy(path: Path | None = None) -> OrchestrationPolicy:
    target = path or policy_path()
    if not target.is_file():
        return OrchestrationPolicy()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid orchestration policy at {target}: {exc}") from exc
    try:
        return OrchestrationPolicy.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"Invalid orchestration policy at {target}: {exc}") from exc


def save_policy(policy: OrchestrationPolicy, path: Path | None = None) -> Path:
    target = path or policy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock("orchestration"):
        atomic_private_write(target, json.dumps(policy.model_dump(), indent=2) + "\n")
    return target


_POLICY_ALIASES = {
    "mode": "mode",
    "announce": "announce",
    "maxParallel": "maxParallel",
    "avoidHumanFacingAgent": "avoidHumanFacingAgent",
    "onComplete": "onComplete",
}
_PROJECT_FIELDS = {"onComplete"}


def resolve_policy_key(key: str) -> str:
    """The policy field a CLI key names, accepting the 'orchestration.' prefix."""
    field = _POLICY_ALIASES.get(key) or _POLICY_ALIASES.get(
        key[len("orchestration."):] if key.startswith("orchestration.") else ""
    )
    if field is None:
        allowed = ", ".join(sorted(_POLICY_ALIASES))
        raise ValueError(f"Unknown orchestration setting '{key}'. Allowed: {allowed}")
    return field


def update_policy(
    key: str, value: Any, path: Path | None = None, project: str | Path | None = None
) -> OrchestrationPolicy:
    field = resolve_policy_key(key)
    current = load_policy(path).model_dump()

    if project is not None:
        if field not in _PROJECT_FIELDS:
            allowed = ", ".join(sorted(_PROJECT_FIELDS))
            raise ValueError(
                f"'{field}' cannot be set per project. Per-project settings: {allowed}"
            )
        repo_key = project_key(project)
        if not repo_key:
            raise ValueError("A project override needs a repository path")
        entry = dict(current.get("projects", {}).get(repo_key) or {})
        entry[field] = value
        current.setdefault("projects", {})[repo_key] = entry
    else:
        current[field] = value

    policy = OrchestrationPolicy.model_validate(current)
    save_policy(policy, path)
    return policy


def effective_exclusions(
    values: list[str] | None,
    policy: OrchestrationPolicy | None = None,
    caller_cli: str | None = None,
) -> list[str]:
    result = [value for value in (values or []) if value.strip()]
    cfg = policy or load_policy()
    caller = (caller_cli or os.environ.get("MINDSYNC_CALLER_CLI", "")).strip().lower()
    caller_family = {
        "gemini": ("gemini", "agy"),
        "agy": ("agy", "gemini"),
    }.get(caller, (caller,) if caller else ())
    existing = {item.lower() for item in result}
    if cfg.avoidHumanFacingAgent:
        for member in caller_family:
            if member not in existing:
                result.append(member)
                existing.add(member)
    return result


def normalize_client_name(name: str | None) -> str | None:
    value = (name or "").strip().lower()
    aliases = {
        "codex": "codex",
        "claude": "claude",
        "gemini": "gemini",
        "grok": "grok",
        "cursor": "cursor",
        "antigravity": "agy",
        "agy": "agy",
    }
    for token, cli in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if token in value:
            return cli
    return None


def caller_cli_from_context(ctx: Any | None) -> str | None:
    if ctx is None:
        return None
    try:
        name = ctx.session.client_params.clientInfo.name
    except (AttributeError, ValueError):
        return None
    return normalize_client_name(name)


def server_instructions(policy: OrchestrationPolicy | None = None) -> str:
    if is_worker_process():
        return (
            "You are a delegated worker, not the human-facing orchestrator. Complete the assigned "
            "task locally, stay within its authorization and working-directory boundaries, verify "
            "your result, and return it to the parent. Do not call delegate_task or recursively "
            "assign work to another CLI."
        )
    invalid_note = ""
    if policy is not None:
        cfg = policy
    else:
        try:
            cfg = load_policy()
        except ValueError as exc:
            cfg = OrchestrationPolicy(mode="off")
            invalid_note = f" The saved orchestration policy is invalid: {exc}"
    announce = "Briefly announce useful delegation." if cfg.announce else "Do not announce routine delegation."
    return (
        "MindSync is available for orchestration. The CLI currently talking to the human owns "
        "task decomposition, authorization boundaries, worker coordination, verification, final "
        f"integration, and the final response. Orchestration mode is '{cfg.mode}'. In auto mode, "
        "delegate bounded independent work when that materially helps; keep tiny or blocking work "
        "local. In suggest mode, preview a route and offer it without launching. In off mode, work "
        "locally unless the human explicitly requests delegation. Use list_agents and route_task "
        "when useful; delegate_task automatically selects an installed capable worker and avoids "
        "the human-facing CLI when identified. Do not end the turn while delegated work is still "
        "running: launch everything you intend to run, then call job_wait on each job until it "
        "returns. That is the completion ping and it saves the human polling for status. Waiting "
        f"on one job before launching the next would serialise work you meant to run in parallel. "
        f"Never broaden the human's permissions. {announce} "
        f"Run at most {cfg.maxParallel} automatically delegated jobs concurrently.{invalid_note}"
    )


def policy_snapshot() -> dict[str, Any]:
    try:
        return {**load_policy().model_dump(), "worker_mode": is_worker_process()}
    except ValueError as exc:
        return {
            **OrchestrationPolicy(mode="off").model_dump(),
            "worker_mode": is_worker_process(),
            "error": str(exc),
        }
