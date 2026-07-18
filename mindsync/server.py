"""MindSync FastMCP server — local-first multi-agent memory sync."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

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
from mindsync.config import settings
from mindsync.conflict import detect_focus_conflicts
from mindsync.storage import (
    enqueue_fact,
    locked_state,
    log_audit,
    read_compiled_truth,
    read_queue,
    claim_offline_queue,
    requeue_failed_facts,
    recover_orphan_spools,
)

mcp = FastMCP("MindSync")
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
    }


def main() -> None:
    settings.ensure_dirs()
    mcp.run()


if __name__ == "__main__":
    main()
