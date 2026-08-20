"""MindSync AI FastMCP server — memory, event bus, and agent dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import Context, FastMCP

from mindsync import __version__
from mindsync.bridge import (
    check_remote_online,
    get_remote_status,
    consolidate_remote,
    pull_compiled_truth,
    remote_not_configured_error,
    validate_agent,
    validate_entity,
    validate_attribute,
    validate_source,
    validate_fact_text,
    write_fact_remote,
    write_batch_remote,
)
from mindsync.bus import (
    Event,
    EventType,
    poll_events as bus_poll_events,
    publish_event as bus_publish_event,
    subscribe as bus_subscribe,
)
from mindsync.config import settings
from mindsync.conflict import detect_focus_conflicts
from mindsync.dispatch import store as dispatch_store
from mindsync.dispatch.review import format_review as dispatch_format_review
from mindsync.dispatch.runner import (
    AutoDelegationSuggestion,
    cancel_job as dispatch_cancel_job,
    describe_empty_result as dispatch_describe_empty_result,
    job_result as dispatch_job_result,
    run_task as dispatch_run_task,
)
from mindsync.memory import (
    memory_bootstrap as memory_memory_bootstrap,
    memory_checkpoint as memory_memory_checkpoint,
    redact_memory_text,
    session_end as memory_session_end,
    session_start as memory_session_start,
)
from mindsync.orchestration import (
    caller_cli_from_context,
    effective_exclusions,
    load_policy,
    is_worker_process,
    policy_snapshot,
    server_instructions,
)
from mindsync.storage import (
    claim_offline_queue,
    enqueue_fact,
    locked_state,
    log_audit,
    read_compiled_truth,
    read_queue,
    recover_orphan_spools,
    requeue_failed_facts,
)

mcp = FastMCP("MindSync", instructions=server_instructions())
# FastMCP falls back to the `mcp` SDK's package version for serverInfo.version
# unless we override it explicitly; report MindSync's own version instead.
mcp._mcp_server.version = __version__


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batch_payload_bytes(batch: list[dict[str, Any]]) -> int:
    """Size of the COMPLETE serialized JSON array bridge.write_batch_remote
    will actually send -- brackets, commas, and all -- not just the sum of
    the individual items' lengths (which undercounts by the array overhead
    and can let a "bounded" batch exceed max_bytes once serialized)."""
    return len(json.dumps(batch).encode("utf-8"))


def _make_bounded_batches(
    facts: list[dict[str, Any]], max_count: int = 50, max_bytes: int = 20 * 1024
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Split facts into batches bounded by count and complete serialized size.

    Returns (batches, oversized). A fact that can't fit within max_bytes
    even by itself can never fit in any batch; it's returned separately so
    the caller can explicitly reject/dead-letter it instead of it being
    silently dropped or wedging the queue forever.
    """
    batches: list[list[dict[str, Any]]] = []
    oversized: list[dict[str, Any]] = []
    current_batch: list[dict[str, Any]] = []

    for fact in facts:
        if _batch_payload_bytes([fact]) > max_bytes:
            oversized.append(fact)
            continue

        candidate = current_batch + [fact]
        if current_batch and (
            len(candidate) > max_count or _batch_payload_bytes(candidate) > max_bytes
        ):
            batches.append(current_batch)
            current_batch = [fact]
        else:
            current_batch = candidate

    if current_batch:
        batches.append(current_batch)
    return batches, oversized


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
    remote_status = get_remote_status()
    log_audit(
        agent_name,
        "get_sync_context",
        f"project={project_name or 'any'} refresh={refresh_remote} remote_status={remote_status['status']}",
    )
    out: dict[str, Any] = {
        "local_state": snapshot,
        "compiled_truth": truth,
        "compiled_truth_keys": list(truth.keys()),
        "remote_status": remote_status,
        "remote_online": (remote_status.get("status") == "online") if settings.remote_enabled else False,
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
            paths=paths,
            stale_seconds=settings.focus_stale_seconds,
            now=now,
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
    try:
        bus_publish_event(
            Event(
                agent_name=agent_name,
                event_type=EventType.FOCUS_CHANGED,
                payload={
                    "project": project,
                    "branch": branch,
                    "focus": focus,
                    "paths": list(paths),
                    "warnings": warnings,
                },
            )
        )
    except Exception as exc:
        # The focus write already succeeded; surface the dropped event instead
        # of reporting a clean success the caller cannot act on.
        warnings.append(f"focus.changed event was not published: {exc}")
        log_audit(agent_name, "update_focus", f"bus publish failed: {exc}")

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
        validate_agent(agent_name)
        validate_entity(entity)
        validate_attribute(attribute)
        validate_fact_text(text)
        conf = float(confidence)
        if not 0.0 <= conf <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    fact = {
        "fact_id": uuid.uuid4().hex,
        "timestamp": _utc_now(),
        "agent": agent_name,
        "entity": entity,
        "attribute": attribute,
        "text": text,
        "source": f"agent:{agent_name}",
        "confidence": conf,
    }

    try:
        bus_publish_event(
            Event(
                agent_name=agent_name,
                event_type=EventType.MEMORY_UPDATED,
                payload={
                    "entity": entity,
                    "attribute": attribute,
                    "text": text,
                    "confidence": conf,
                    "fact_id": fact["fact_id"],
                },
            )
        )
    except Exception:
        pass

    if settings.remote_enabled:
        result = write_fact_remote(
            fact_id=fact["fact_id"],
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
    from mindsync.storage import file_lock

    try:
        with file_lock("sync", timeout=settings.lock_timeout_seconds):
            recover_orphan_spools()
            
            spool_id, facts, malformed_count = claim_offline_queue()
            if not facts:
                if spool_id:
                    requeue_failed_facts(spool_id, [], [])
                if malformed_count:
                    # The queue wasn't empty -- every record in it was
                    # unparseable and got quarantined. That's not "nothing to
                    # sync"; report it so it doesn't look like a silent no-op.
                    return {
                        "ok": False,
                        "status": "error",
                        "message": (
                            f"No valid offline facts; {malformed_count} malformed "
                            "record(s) quarantined to dead letter."
                        ),
                        "synced_count": 0,
                        "dead_letter_count": malformed_count,
                    }
                return {
                    "ok": True,
                    "status": "ok",
                    "message": "No offline facts in the queue.",
                    "synced_count": 0,
                }

            if not settings.remote_enabled:
                requeue_failed_facts(spool_id, facts, [])
                return {
                    "ok": False,
                    "status": "error",
                    "message": remote_not_configured_error(),
                    "synced_count": 0,
                }

            valid_facts = []
            dead_letter = []
            errors = []

            for fact in facts:
                try:
                    agent = validate_agent(str(fact.get("agent", agent_name)))
                    entity = validate_entity(str(fact.get("entity", "")))
                    attribute = validate_attribute(str(fact.get("attribute", "")))
                    text = validate_fact_text(str(fact.get("text", "")))
                    source = validate_source(str(fact.get("source", f"agent:{agent_name}")))
                    conf = float(fact.get("confidence", 1.0))
                    if not 0.0 <= conf <= 1.0:
                        raise ValueError("confidence must be between 0.0 and 1.0")
                except ValueError as exc:
                    dead_letter.append(fact)
                    errors.append(f"Malformed queued fact: {exc}")
                    continue
                # Normalize confidence to a real float on the fact (it may have
                # been loaded from JSONL as a string-ish value).
                fact["confidence"] = conf

                if "fact_id" not in fact:
                    content = f"{agent}:{entity}:{attribute}:{fact.get('timestamp')}:{text}"
                    fact["fact_id"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    
                fact["agent"] = agent
                fact["entity"] = entity
                fact["attribute"] = attribute
                fact["text"] = text
                fact["source"] = source
                fact["confidence"] = conf
                valid_facts.append(fact)

            success_count = 0
            failed = []

            # Partition into bounded batches. Facts too large to ever fit in
            # a single payload (even alone) are explicitly rejected/
            # dead-lettered here rather than silently dropped or wedging the
            # queue on every future sync attempt.
            batches, oversized = _make_bounded_batches(valid_facts)
            for fact in oversized:
                dead_letter.append(fact)
                errors.append(
                    f"Fact {fact.get('fact_id')} exceeds max payload size; "
                    "quarantined to dead letter."
                )

            for batch in batches:
                # Retry with backoff
                retries = 3
                delay = 1.0
                result = None
                for attempt in range(retries):
                    result = write_batch_remote(batch)
                    if result.ok:
                        break
                    if "not configured" in (result.error or ""):
                        break
                    if attempt < retries - 1:
                        time.sleep(delay)
                        delay *= 2.0
                
                # Check outcome of the batch
                if result and result.ok:
                    res_data = getattr(result, "results", None)
                    if res_data and isinstance(res_data, dict) and "success_ids" in res_data:
                        success_ids = set(res_data["success_ids"])
                        failed_info = res_data.get("failed") or []
                        
                        for fact in batch:
                            fid = fact.get("fact_id")
                            if fid in success_ids:
                                success_count += 1
                            else:
                                failed.append(fact)
                                err_msg = "Skipped or failed in batch insertion"
                                for f_err in failed_info:
                                    if f_err.get("fact_id") == fid:
                                        err_msg = f_err.get("error", err_msg)
                                        break
                                errors.append(f"Fact {fid} failed remote write: {err_msg}")
                    else:
                        success_count += len(batch)
                else:
                    failed.extend(batch)
                    if result and result.error:
                        errors.append(result.error)
                    else:
                        errors.append("Batch write failed completely.")

            requeue_failed_facts(spool_id, failed, dead_letter)

            pull_success = False
            consolidate_error = None
            pull_error = None
            if consolidate and success_count > 0:
                c_res = consolidate_remote()
                if not c_res.ok:
                    consolidate_error = c_res.error
                else:
                    p_res = pull_compiled_truth()
                    pull_success = p_res.ok
                    if not p_res.ok:
                        pull_error = p_res.error

            log_audit(
                agent_name,
                "sync_offline_facts",
                f"synced={success_count} remaining={len(failed)} dead_letter={len(dead_letter)} pull={pull_success}",
            )
            
            # Dead-lettered/failed records must never be reported as a clean
            # ok=True sync: "ok" (fully clean), "partial" (some synced, some
            # failed/dead-lettered), or "error" (nothing synced).
            overall_ok = len(failed) == 0 and len(dead_letter) == 0
            if consolidate and success_count > 0:
                if consolidate_error or pull_error:
                    overall_ok = False

            if overall_ok:
                status = "ok"
            elif success_count > 0:
                status = "partial"
            else:
                status = "error"

            return {
                "ok": overall_ok,
                "status": status,
                "synced_count": success_count,
                "remaining_queue": len(failed),
                "dead_letter_count": len(dead_letter),
                "pull_success": pull_success,
                "errors": errors[:5],
                "consolidate_error": consolidate_error,
                "pull_error": pull_error,
                "message": f"Synced {success_count} fact(s); {len(failed)} remaining in queue, {len(dead_letter)} dead.",
            }
    except TimeoutError:
        return {
            "ok": False,
            "status": "error",
            "message": "Another sync is currently in progress.",
            "synced_count": 0,
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
def publish_event(
    agent_name: str,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Publish an event to the AgentRelay Event Bus."""
    settings.ensure_dirs()
    event = Event(
        agent_name=agent_name,
        event_type=event_type,
        payload=payload or {},
        correlation_id=correlation_id,
    )
    published = bus_publish_event(event)
    log_audit(agent_name, "publish_event", f"type={event_type} seq={published.seq}")
    return {
        "ok": True,
        "event": published.to_dict(),
        "seq": published.seq,
        "message": f"Event '{event_type}' published with seq {published.seq}.",
    }


@mcp.tool()
def poll_events(
    agent_name: str,
    since_seq: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Poll events from the Event Bus since sequence number since_seq."""
    settings.ensure_dirs()
    events = bus_poll_events(since_seq=since_seq, limit=limit, agent_name=agent_name)
    log_audit(agent_name, "poll_events", f"since_seq={since_seq} returned={len(events)}")
    return {
        "ok": True,
        "events": [e.to_dict() for e in events],
        "count": len(events),
        "since_seq": since_seq,
    }


@mcp.tool()
def subscribe_events(
    agent_name: str,
    event_types: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Subscribe an agent to specific event types on the Event Bus."""
    settings.ensure_dirs()
    sub_info = bus_subscribe(agent_name, event_types)
    log_audit(agent_name, "subscribe_events", f"event_types={event_types}")
    return {
        "ok": True,
        "agent_name": agent_name,
        "event_types": sub_info.get("event_types", []),
        "message": f"Agent {agent_name} subscribed to {event_types or 'all events'}.",
    }


def _fmt_dispatch_job(m: dict[str, Any]) -> str:
    exit_bit = ""
    if m.get("exitCode") is not None:
        to = ", TIMED OUT" if m.get("timedOut") else ""
        exit_bit = f" (exit {m['exitCode']}{to})"
    prompt = m.get("prompt") or ""
    if len(prompt) > 100:
        prompt = prompt[:100] + "…"
    if m.get("role"):
        agent_str = f"{m['agent']} (role: {m['role']})"
    elif m.get("routing"):
        agent_str = f"{m['agent']} (auto)"
    else:
        agent_str = m["agent"]
    route_line = f"\n  route: {m['routing']['reason']}" if m.get("routing") else ""
    return f"[{m['id']}] {agent_str} — {m['status']}{exit_bit}\n  prompt: {prompt}{route_line}"


@mcp.tool()
async def delegate_task(
    agent: str | None = None,
    prompt: str = "",
    role: str | None = None,
    write: bool = False,
    background: bool = False,
    model: str | None = None,
    effort: str | None = None,
    cwd: str | None = None,
    worktree: bool = False,
    checks: list[str] | None = None,
    required_capabilities: list[str] | None = None,
    exclude_agents: list[str] | None = None,
    agent_name: str = "default_agent",
    ctx: Context | None = None,
) -> str:
    """Delegate a task to a headless CLI agent or role.

    The default agent='auto' selects an installed worker by capability. Pass
    required_capabilities when the orchestrator already knows what the task needs,
    and exclude_agents to keep the human-facing orchestrator out of the worker pool.
    Direct agent selection and static roles remain available as explicit overrides.
    If worktree is True, the agent runs in an isolated git worktree branching from cwd.
    checks are shell commands run after the agent finishes (for example a test command);
    read their outcome with job_review before spending anything on the job's output.
    """
    settings.ensure_dirs()
    if is_worker_process():
        return (
            "Error: recursive delegation is disabled for MindSync workers. "
            "Complete the assigned task locally and return it to the orchestrator."
        )
    effective_agent = agent if agent is not None else (None if role is not None else "auto")
    policy = load_policy() if effective_agent == "auto" else None
    exclusions = (
        effective_exclusions(
            exclude_agents,
            policy,
            caller_cli_from_context(ctx),
        )
        if effective_agent == "auto"
        else exclude_agents
    )
    try:
        res = await dispatch_run_task(
            agent=effective_agent,
            role=role,
            prompt=prompt,
            model=model,
            effort=effort,
            write=write,
            background=background,
            cwd=cwd,
            worktree=worktree,
            checks=checks,
            publisher_agent=agent_name,
            required_capabilities=required_capabilities,
            exclude_agents=exclusions,
        )
    except AutoDelegationSuggestion as exc:
        log_audit(agent_name, "delegate_task", f"suggested={exc.decision['agent']}")
        return (
            "Suggestion only; no job was launched. "
            f"{exc.decision['reason']} Ask the human or delegate explicitly if they approve."
        )
    except Exception as exc:
        log_audit(agent_name, "delegate_task", f"error={exc}")
        return f"Error: {exc}"
    job = res["job"]
    route_info = job.get("routing")
    route_line = (
        f"Auto route: {route_info['reason']}\n"
        if route_info and policy.announce
        else ""
    )
    warning_line = "".join(f"Warning: {warning}\n" for warning in job.get("warnings", []))
    log_audit(
        agent_name,
        "delegate_task",
        f"job={job.get('id')} agent={job.get('agent')} role={job.get('role')} bg={background} status={job.get('status')}",
    )
    if background:
        wt_info = f"\nworktree: {job['worktreePath']}  (branch: {job['branch']})" if job.get("worktreePath") else ""
        return (
            f"{route_line}{warning_line}Started background job {job['id']} (agent: {job['agent']}).{wt_info} "
            f"Wait for completion now: job_wait('{job['id']}'). Do not end the turn "
            "while delegated work is still running."
        )
    wt_info = f"worktree: {job['worktreePath']}  (branch: {job['branch']})\n" if job.get("worktreePath") else ""
    return f"{route_line}{warning_line}{wt_info}{res.get('result') or '(no output)'}"


@mcp.tool()
def route_task(
    prompt: str,
    required_capabilities: list[str] | None = None,
    exclude_agents: list[str] | None = None,
    agent_name: str = "default_agent",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Preview automatic worker selection without launching a job."""
    from mindsync.dispatch.routing import select_agent

    policy = load_policy()
    decision = select_agent(
        prompt,
        required_capabilities=required_capabilities,
        exclude_agents=effective_exclusions(
            exclude_agents,
            policy,
            caller_cli_from_context(ctx),
        ),
    )
    log_audit(agent_name, "route_task", decision["reason"])
    return decision


@mcp.tool()
def get_orchestration_policy(
    agent_name: str = "default_agent",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read automatic delegation policy. Call when deciding whether to delegate or offer it."""
    snapshot = policy_snapshot()
    snapshot["caller_cli"] = caller_cli_from_context(ctx) or os.environ.get(
        "MINDSYNC_CALLER_CLI"
    )
    log_audit(agent_name, "get_orchestration_policy", f"mode={snapshot['mode']}")
    return snapshot


@mcp.tool()
def list_agents(agent_name: str = "default_agent") -> list[dict[str, Any]]:
    """List dispatch agents with live binary availability and routing capabilities."""
    from mindsync.dispatch.adapters import load_adapters
    from mindsync.dispatch.proc import resolve_bin

    agents = []
    for adapter in load_adapters().values():
        agents.append({
            "name": adapter.name,
            "display_name": adapter.displayName or adapter.name,
            "family": adapter.family or adapter.name,
            "available": bool(resolve_bin(adapter.bin)),
            "capabilities": adapter.capabilities or ["general"],
            "routing_priority": adapter.routingPriority,
            "default_model": adapter.defaultModel,
            "efforts": adapter.efforts,
        })
    log_audit(agent_name, "list_agents", f"count={len(agents)}")
    return agents


@mcp.tool()
def job_status(job_id: str, agent_name: str = "default_agent") -> str:
    """Check the status of a delegated job."""
    settings.ensure_dirs()
    job = dispatch_store.get_job(job_id)
    if not job:
        return f"No such job: {job_id}"
    reconciled = dispatch_store.reconcile_job(job)
    log_audit(agent_name, "job_status", f"job={job_id} status={reconciled.get('status')}")
    return _fmt_dispatch_job(reconciled)


@mcp.tool()
async def job_wait(
    job_id: str,
    timeout_seconds: float = 900.0,
    poll_interval_seconds: float = 0.5,
    agent_name: str = "default_agent",
) -> str:
    """Wait for a background job to finish and return its review as a completion ping.

    Call this immediately after delegate_task(background=True) instead of repeatedly
    polling job_status or ending the turn. The pending MCP call resumes when the job
    completes, fails, or is cancelled, so the orchestrator can review and report the
    outcome without the human babysitting it.
    """
    settings.ensure_dirs()
    if timeout_seconds <= 0 or timeout_seconds > 3600:
        return "timeout_seconds must be greater than 0 and at most 3600."
    if poll_interval_seconds < 0.1 or poll_interval_seconds > 5:
        return "poll_interval_seconds must be between 0.1 and 5."

    deadline = time.monotonic() + timeout_seconds
    terminal = {"done", "failed", "cancelled"}
    while True:
        job = dispatch_store.get_job(job_id)
        if not job:
            return f"No such job: {job_id}"
        reconciled = dispatch_store.reconcile_job(job)
        status = reconciled.get("status")
        if status in terminal:
            log_audit(agent_name, "job_wait", f"job={job_id} status={status}")
            return (
                f"Completion ping: job {job_id} reached terminal status '{status}'.\n"
                f"{dispatch_format_review(reconciled)}"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log_audit(agent_name, "job_wait", f"job={job_id} status={status} timeout")
            return (
                f"Job {job_id} is still {status} after {timeout_seconds:g} seconds. "
                "Call job_wait again to keep the completion watch active."
            )
        await asyncio.sleep(min(poll_interval_seconds, remaining))


@mcp.tool()
def job_review(job_id: str, agent_name: str = "default_agent") -> str:
    """Get a mechanical review verdict for a job, including check results and diff summary. Call this before job_result to skip reading the output of work that did not pass."""
    settings.ensure_dirs()
    job = dispatch_store.get_job(job_id)
    if not job:
        return f"No such job: {job_id}"
    reconciled = dispatch_store.reconcile_job(job)
    log_audit(agent_name, "job_review", f"job={job_id} status={reconciled.get('status')}")
    return dispatch_format_review(reconciled)


@mcp.tool()
def job_result(job_id: str, agent_name: str = "default_agent") -> str:
    """Fetch the result file content for a completed job."""
    settings.ensure_dirs()
    try:
        res_data = dispatch_job_result(job_id)
    except ValueError as exc:
        return str(exc)
    meta = res_data["meta"]
    log_audit(agent_name, "job_result", f"job={job_id} status={meta.get('status')}")
    return res_data["result"] or dispatch_describe_empty_result(meta)


@mcp.tool()
def job_cancel(job_id: str, agent_name: str = "default_agent") -> str:
    """Cancel a running job and kill its process tree."""
    settings.ensure_dirs()
    try:
        meta = dispatch_cancel_job(job_id)
    except ValueError as exc:
        return str(exc)
    log_audit(agent_name, "job_cancel", f"job={job_id} status={meta.get('status')}")
    return f"Job {meta['id']}: {meta['status']}"


@mcp.tool()
def list_models(agent: str | None = None, agent_name: str = "default_agent") -> str:
    """List available models for the given agent or all agents. Use this for choosing a model before delegating a task."""
    settings.ensure_dirs()
    from mindsync.dispatch.adapters import resolve_adapter, list_models as adapter_list_models, load_adapters
    
    try:
        agents_to_list = [resolve_adapter(agent)] if agent else load_adapters().values()
    except KeyError as exc:
        return f"Error: {exc}"
        
    out = []
    for a in agents_to_list:
        out.append(f"Models for {a.name}:")
        models = adapter_list_models(a)
        if not models:
            out.append("  (none discovered)")
        for m in models:
            marker = "  (default)" if m == a.defaultModel else ""
            out.append(f"  {m}{marker}")
    
    return "\n".join(out)


@mcp.tool()
def list_roles(agent_name: str = "default_agent") -> str:
    """List configured roles and their agent, model, and effort mappings.

    Prefer specifying a role over an agent name when delegating tasks so that
    model and effort configurations are applied automatically.
    """
    settings.ensure_dirs()
    from mindsync.dispatch.adapters import load_roles, user_config_path

    roles = load_roles()
    if not roles:
        return f"No roles are configured; add a 'roles' block to {user_config_path()}"

    width = max((len(r.name) for r in roles.values()), default=10)
    width = max(width, 10)
    out = []
    for r in roles.values():
        parts = [f"{r.name:<{width}} -> {r.agent}"]
        if r.model:
            parts.append(f"model: {r.model}")
        if r.effort:
            parts.append(f"effort: {r.effort}")
        out.append("   ".join(parts))

    return "\n".join(out)


@mcp.tool()
def health(agent_name: str = "system") -> dict[str, Any]:
    """Report local paths, queue depth, and remote reachability."""
    settings.ensure_dirs()
    # include spool facts count in queue depth
    queue_len = len(read_queue())
    for spool_path in settings.spool_dir.glob("spool-*.jsonl"):
        try:
            with open(spool_path, "r", encoding="utf-8") as fh:
                queue_len += sum(1 for _ in fh)
        except OSError:
            pass
            
    remote_status = get_remote_status(force=True) if settings.remote_enabled else {"status": "unknown"}
    remote_online = (remote_status.get("status") == "online") if settings.remote_enabled else False
    with locked_state() as state:
        agents = list((state.get("agents_focus") or {}).keys())
    return {
        "ok": True,
        "home": str(settings.home),
        "state_file": str(settings.state_file),
        "queue_depth": queue_len,
        "remote_configured": settings.remote_enabled,
        "remote_status": remote_status,
        "remote_online": remote_online,
        "ssh_host": settings.ssh_host or None,
        "remote_root": settings.remote_root or None,
        "active_agents": agents,
        "truth_files": list(read_compiled_truth().keys()),
        "orchestration": policy_snapshot(),
    }


@mcp.tool()
def session_start(
    agent_name: str,
    project_key: str,
    workspace: Optional[str] = None,
    branch: Optional[str] = None,
    goal: Optional[str] = None,
) -> dict[str, Any]:
    """Start a tracked local-memory session and return its session ID."""
    try:
        validated_agent = validate_agent(agent_name)
        session_id = memory_session_start(
            project_key=project_key,
            agent=validated_agent,
            workspace=workspace,
            branch=branch,
            goal=goal,
        )
        log_audit(validated_agent, "session_start", f"project={project_key} session={session_id}")
        return {"ok": True, "session_id": session_id}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def memory_checkpoint(
    agent_name: str,
    session_id: str,
    status: Optional[str] = None,
    decisions: Optional[Any] = None,
    files_changed: Optional[Any] = None,
    tests: Optional[Any] = None,
    pending: Optional[Any] = None,
    blockers: Optional[Any] = None,
    durable_facts: Optional[Any] = None,
) -> dict[str, Any]:
    """Save validated, best-effort-redacted structured session memory."""
    try:
        validated_agent = validate_agent(agent_name)
        safe_status = redact_memory_text(status) if status is not None else None
        checkpoint_id = memory_memory_checkpoint(
            session_id=session_id,
            status=safe_status,
            decisions=decisions,
            files_changed=files_changed,
            tests=tests,
            pending=pending,
            blockers=blockers,
            durable_facts=durable_facts,
        )
        log_audit(
            validated_agent,
            "memory_checkpoint",
            f"session={session_id} checkpoint={checkpoint_id} status={safe_status}",
        )
        warnings: list[str] = []
        try:
            if durable_facts or safe_status:
                bus_publish_event(
                    Event(
                        agent_name=validated_agent,
                        event_type=EventType.MEMORY_UPDATED,
                        payload={
                            "session_id": session_id,
                            "checkpoint_id": checkpoint_id,
                            "status": safe_status,
                            "has_durable_facts": bool(durable_facts),
                        },
                    )
                )
        except Exception as exc:
            warning = f"memory.updated event was not published: {exc}"
            warnings.append(warning)
            log_audit(validated_agent, "memory_checkpoint", warning)

        return {"ok": True, "checkpoint_id": checkpoint_id, "warnings": warnings}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def session_end(
    agent_name: str,
    session_id: str,
    status: str = "completed",
) -> dict[str, Any]:
    """Mark a memory session as completed or failed."""
    try:
        validated_agent = validate_agent(agent_name)
        safe_status = redact_memory_text(status)
        memory_session_end(session_id=session_id, status=safe_status)
        log_audit(
            validated_agent,
            "session_end",
            f"session={session_id} status={safe_status}",
        )
        return {"ok": True}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def memory_bootstrap(
    agent_name: str,
    project_key: str,
    budget_chars: int = 20_000,
) -> dict[str, Any]:
    """Retrieve bounded project context, prioritizing open and recent sessions."""
    try:
        validated_agent = validate_agent(agent_name)
        data = memory_memory_bootstrap(project_key=project_key, budget_chars=budget_chars)
        log_audit(
            validated_agent,
            "memory_bootstrap",
            f"project={project_key} sessions={len(data.get('bootstraps', []))}",
        )
        return {"ok": True, "data": data}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    if len(sys.argv) > 1:
        from mindsync.manage import main as manage_main

        raise SystemExit(manage_main(sys.argv[1:]))
    settings.ensure_dirs()
    mcp.run()


if __name__ == "__main__":
    main()
