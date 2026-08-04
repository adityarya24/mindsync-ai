"""Persistent orchestration policy shared by MindSync MCP and dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from mindsync.config import settings
from mindsync.storage import atomic_private_write, file_lock


class OrchestrationPolicy(BaseModel):
    mode: Literal["auto", "suggest", "off"] = "auto"
    announce: bool = True
    maxParallel: int = Field(default=3, ge=1, le=16)
    avoidHumanFacingAgent: bool = True


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


def update_policy(key: str, value: Any, path: Path | None = None) -> OrchestrationPolicy:
    aliases = {
        "mode": "mode",
        "orchestration.mode": "mode",
        "announce": "announce",
        "orchestration.announce": "announce",
        "maxParallel": "maxParallel",
        "orchestration.maxParallel": "maxParallel",
        "avoidHumanFacingAgent": "avoidHumanFacingAgent",
        "orchestration.avoidHumanFacingAgent": "avoidHumanFacingAgent",
    }
    field = aliases.get(key)
    if field is None:
        allowed = ", ".join(sorted(aliases))
        raise ValueError(f"Unknown orchestration setting '{key}'. Allowed: {allowed}")
    current = load_policy(path).model_dump()
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
        f"the human-facing CLI when identified. Never broaden the human's permissions. {announce} "
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
