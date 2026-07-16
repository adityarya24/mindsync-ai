"""MindSync FastMCP server — local-first multi-agent memory sync."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from mindsync.bridge import (
    check_remote_online,
    consolidate_remote,
    pull_compiled_truth,
    remote_not_configured_error,
    validate_id,
    write_fact_remote,
)
from mindsync.config import settings
from mindsync.conflict import detect_focus_conflicts
from mindsync.storage import (
    enqueue_fact,
    locked_state,
    log_audit,
    read_compiled_truth,
    read_queue,
    rewrite_queue,
)

mcp = FastMCP("MindSync")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
def get_sync_context(
    agent_name: str,
    project_name: Optional[str] = None,
    refresh_remote: bool = False,
) -> dict[str, Any]:
    """Load local session state + compiled truth. Optionally pull remote truth first.

    Offline-first: always returns local data even if remote is unreachable or disabled.
    """
    settings.ensure_dirs()
    pull_info: dict[str, Any] | None = None
    if refresh_remote:
        if check_remote_online():
            result = pull_compiled_truth()
            pull_info = {"ok": result.ok, "error": result.error}
        else:
            reason = (
                remote_not_configured_error()
                if not settings.remote_enabled
                else "Remote offline; skipped pull"
            )
            pull_info = {"ok": False, "error": reason}

    with locked_state() as state:
        snapshot = dict(state)

    truth = read_compiled_truth()
    online = check_remote_online()
    log_audit(
        agent_name,
        "get_sync_context",
        f"project={project_name or 'any'} refresh={refresh_remote} remote_online={online}",
    )
    out: dict[str, Any] = {
        "local_state": snapshot,
        "compiled_truth": truth,
        "compiled_truth_keys": list(truth.keys()),
        "remote_online": online,
        "remote_configured": settings.remote_enabled,
        "home": str(settings.home),
    }
    if pull_info is not None:
        out["pull"] = pull_info
    return out


@mcp.tool()
def update_focus(
    agent_name: str,
    project: str,
    branch: str,
    focus: str,
    paths: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Update this agent's focus and warn on overlaps with other non-stale agents."""
    settings.ensure_dirs()
    now = datetime.now(timezone.utc)
    paths = paths or []

    with locked_state() as state:
        agents_focus = state.setdefault("agents_focus", {})
        warnings = detect_focus_conflicts(
            agent_name,
            project,
            branch,
            focus,
            agents_focus,
            stale_seconds=settings.focus_stale_seconds,
            now=now,
        )
        my_paths = {p.replace("\\", "/").lower() for p in paths if p}
        if my_paths:
            for other_agent, other in list(agents_focus.items()):
                if other_agent == agent_name or not isinstance(other, dict):
                    continue
                other_paths = {
                    str(p).replace("\\", "/").lower()
                    for p in (other.get("paths") or [])
                    if p
                }
                overlap_paths = sorted(my_paths & other_paths)
                if overlap_paths and other.get("project") in (None, project):
                    warnings.append(
                        "CONFLICT WARNING: "
                        f"Agent '{other_agent}' claims paths: {', '.join(overlap_paths)}"
                    )

        agents_focus[agent_name] = {
            "project": project,
            "branch": branch,
            "focus": focus,
            "paths": list(paths),
            "timestamp": now.isoformat(),
        }
        state["active_project"] = project
        state["active_branch"] = branch
        state["current_focus"] = focus
        state["last_updated_by"] = agent_name
        state["timestamp"] = now.isoformat()
        state["last_run_status"] = "active"

    log_audit(
        agent_name,
        "update_focus",
        f"project={project} branch={branch} focus={focus!r} warnings={len(warnings)}",
    )
    return {
        "ok": True,
        "warnings": warnings,
        "message": f"Focus updated for {agent_name}.",
    }


@mcp.tool()
def queue_durable_fact(
    agent_name: str,
    entity: str,
    attribute: str,
    text: str,
    confidence: float = 1.0,
) -> dict[str, Any]:
    """Write a durable fact remotely if online; otherwise queue locally for later flush."""
    settings.ensure_dirs()
    try:
        validate_id("agent", agent_name)
        validate_id("entity", entity)
        validate_id("attribute", attribute)
        conf = float(confidence)
        if not 0.0 <= conf <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    fact = {
        "timestamp": _utc_now(),
        "agent": agent_name,
        "entity": entity,
        "attribute": attribute,
        "text": text,
        "source": f"agent:{agent_name}",
        "confidence": conf,
    }

    if check_remote_online():
        result = write_fact_remote(
            agent=agent_name,
            entity=entity,
            attribute=attribute,
            text=text,
            source=f"agent:{agent_name}",
            confidence=conf,
        )
        if result.ok:
            log_audit(agent_name, "write_fact_remote", f"{entity}.{attribute}")
            return {
                "ok": True,
                "synced_to_remote": True,
                "queued_locally": False,
                "details": result.stdout,
            }
        fallback_reason = result.error or "remote write failed"
        enqueue_fact(fact)
        log_audit(
            agent_name,
            "queue_fact_local",
            f"{entity}.{attribute} reason={fallback_reason}",
        )
        return {
            "ok": True,
            "synced_to_remote": False,
            "queued_locally": True,
            "fallback_reason": fallback_reason,
            "message": "Remote write failed; fact queued locally.",
        }

    enqueue_fact(fact)
    reason = "remote_not_configured" if not settings.remote_enabled else "remote_offline"
    log_audit(agent_name, "queue_fact_local", f"{entity}.{attribute} reason={reason}")
    return {
        "ok": True,
        "synced_to_remote": False,
        "queued_locally": True,
        "message": (
            "Remote not configured. Fact queued locally."
            if reason == "remote_not_configured"
            else "Remote offline. Fact queued locally."
        ),
    }


@mcp.tool()
def sync_offline_facts(agent_name: str, consolidate: bool = True) -> dict[str, Any]:
    """Flush the offline queue to remote, optionally consolidate + pull compiled truth."""
    settings.ensure_dirs()
    facts = read_queue()
    if not facts:
        return {"ok": True, "message": "No offline facts in the queue.", "synced_count": 0}

    if not settings.remote_enabled:
        return {
            "ok": False,
            "message": remote_not_configured_error(),
            "synced_count": 0,
        }

    if not check_remote_online(force=True):
        return {
            "ok": False,
            "message": "Cannot sync: remote is still offline.",
            "synced_count": 0,
        }

    success_count = 0
    failed: list[dict[str, Any]] = []
    errors: list[str] = []

    for fact in facts:
        result = write_fact_remote(
            agent=str(fact.get("agent", agent_name)),
            entity=str(fact["entity"]),
            attribute=str(fact["attribute"]),
            text=str(fact["text"]),
            source=str(fact.get("source", f"agent:{agent_name}")),
            confidence=float(fact.get("confidence", 1.0)),
        )
        if result.ok:
            success_count += 1
        else:
            failed.append(fact)
            if result.error:
                errors.append(result.error)

    rewrite_queue(failed)

    pull_success = False
    consolidate_error = None
    pull_error = None
    if consolidate and success_count > 0:
        c_res = consolidate_remote()
        if not c_res.ok:
            consolidate_error = c_res.error
        p_res = pull_compiled_truth()
        pull_success = p_res.ok
        if not p_res.ok:
            pull_error = p_res.error

    log_audit(
        agent_name,
        "sync_offline_facts",
        f"synced={success_count} remaining={len(failed)} pull={pull_success}",
    )
    return {
        "ok": len(failed) == 0
        and (pull_error is None if consolidate and success_count else True),
        "synced_count": success_count,
        "remaining_queue": len(failed),
        "pull_success": pull_success,
        "errors": errors[:5],
        "consolidate_error": consolidate_error,
        "pull_error": pull_error,
        "message": f"Synced {success_count} fact(s); {len(failed)} remaining in queue.",
    }


@mcp.tool()
def pull_truth(agent_name: str) -> dict[str, Any]:
    """Pull compiled-truth markdown from the remote host into the local cache."""
    settings.ensure_dirs()
    if not settings.remote_enabled:
        return {"ok": False, "message": remote_not_configured_error()}
    if not check_remote_online(force=True):
        return {"ok": False, "message": "Remote offline; cannot pull."}
    result = pull_compiled_truth()
    log_audit(agent_name, "pull_truth", f"ok={result.ok} error={result.error}")
    if not result.ok:
        return {"ok": False, "error": result.error}
    keys = list(read_compiled_truth().keys())
    return {
        "ok": True,
        "compiled_truth_keys": keys,
        "message": f"Pulled {len(keys)} truth file(s).",
    }


@mcp.tool()
def health(agent_name: str = "system") -> dict[str, Any]:
    """Report local paths, queue depth, and remote reachability."""
    settings.ensure_dirs()
    queue_len = len(read_queue())
    online = check_remote_online(force=True) if settings.remote_enabled else False
    with locked_state() as state:
        agents = list((state.get("agents_focus") or {}).keys())
    return {
        "ok": True,
        "home": str(settings.home),
        "state_file": str(settings.state_file),
        "queue_depth": queue_len,
        "remote_configured": settings.remote_enabled,
        "remote_online": online,
        "ssh_host": settings.ssh_host or None,
        "remote_root": settings.remote_root or None,
        "active_agents": agents,
        "truth_files": list(read_compiled_truth().keys()),
    }


def main() -> None:
    settings.ensure_dirs()
    mcp.run()


if __name__ == "__main__":
    main()
