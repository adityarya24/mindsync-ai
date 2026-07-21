"""Task execution, supervision, cancel/result, and EventBus hooks."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from mindsync.dispatch.adapters import (
    build_invocation,
    resolve_adapter,
    user_config_path,
)
from mindsync.dispatch.proc import (
    kill_tree,
    process_name,
    resolve_bin,
    spawn_background,
    spawn_foreground,
)
from mindsync.dispatch import store


class AgentNotInstalledError(RuntimeError):
    def __init__(self, adapter: Any) -> None:
        display = getattr(adapter, "displayName", None) or adapter.name
        hint = getattr(adapter, "installHint", None)
        msg = f"'{adapter.bin}' ({display}) is not on PATH."
        if hint:
            msg += f" Install: {hint}"
        super().__init__(msg)
        self.name = "AgentNotInstalledError"


def assert_arg_mode_spawn_safe(
    adapter: Any,
    resolved_bin: str,
    platform: str | None = None,
) -> None:
    """Block prompt-as-arg through Windows .cmd/.bat shims (cmd.exe re-parse)."""
    plat = platform if platform is not None else sys.platform
    if plat != "win32" or getattr(adapter, "input", None) != "arg":
        return
    if not re.search(r"\.(cmd|bat)$", resolved_bin, re.I):
        return
    raise RuntimeError(
        f"Adapter '{adapter.name}' passes the prompt as a command-line argument, which is unsafe "
        f"through the Windows shim '{Path(resolved_bin).name}' (cmd.exe can execute prompt text). "
        f"Point bin at the underlying .exe or script in {user_config_path()}, "
        "or switch the adapter to stdin input."
    )


def _publish_job_event(event_type: str, meta: dict[str, Any], agent_name: str = "dispatch") -> None:
    try:
        from mindsync.bus import Event, EventType, publish_event

        et = {
            "job.started": EventType.JOB_STARTED,
            "job.completed": EventType.JOB_COMPLETED,
            "job.failed": EventType.JOB_FAILED,
        }.get(event_type, event_type)
        publish_event(
            Event(
                agent_name=agent_name,
                event_type=et,
                payload={
                    "job_id": meta.get("id"),
                    "agent": meta.get("agent"),
                    "status": meta.get("status"),
                    "exit_code": meta.get("exitCode"),
                    "timed_out": meta.get("timedOut"),
                    "model": meta.get("model"),
                    "write": meta.get("write"),
                },
                correlation_id=meta.get("id"),
            )
        )
    except Exception:
        # Event bus must never break dispatch.
        pass


async def run_task(
    *,
    agent: str,
    prompt: str,
    model: str | None = None,
    write: bool = False,
    background: bool = False,
    cwd: str | None = None,
    publisher_agent: str = "dispatch",
) -> dict[str, Any]:
    workdir = cwd or os.getcwd()
    adapter = resolve_adapter(agent)
    bin_path = resolve_bin(adapter.bin)
    if not bin_path:
        raise AgentNotInstalledError(adapter)
    assert_arg_mode_spawn_safe(adapter, bin_path)

    meta = store.create_job(
        agent=agent,
        prompt=prompt,
        cwd=workdir,
        model=model,
        write=write,
    )

    if background:
        paths = store.job_paths(meta["id"])
        # Re-enter via CLI supervise so the MCP server process is not blocked.
        py = sys.executable
        bg = spawn_background(
            py,
            ["-m", "mindsync.dispatch.cli", "_supervise", meta["id"]],
            cwd=workdir,
            stdout_path=paths["supervisorLog"],
            stderr_path=paths["supervisorLog"],
        )
        updated = store.update_job(
            meta["id"],
            {
                "status": "running",
                "pid": bg["pid"],
                "spawnedName": bg["spawnedName"],
            },
        )
        _publish_job_event("job.started", updated, agent_name=publisher_agent)
        return {"job": updated}

    done = await supervise_job(meta["id"], publisher_agent=publisher_agent)
    return {"job": done, "result": job_result(meta["id"])["result"]}


async def supervise_job(
    job_id: str,
    *,
    publisher_agent: str = "dispatch",
) -> dict[str, Any]:
    meta = store.get_job(job_id)
    if meta is None:
        raise ValueError(f"No such job: {job_id}")

    self_pid = os.getpid()
    running = store.update_job(
        job_id,
        {
            "status": "running",
            "pid": self_pid,
            "spawnedName": process_name(self_pid) or Path(sys.executable).name.lower(),
        },
    )
    _publish_job_event("job.started", running, agent_name=publisher_agent)

    adapter = resolve_adapter(meta["agent"])
    bin_path = resolve_bin(adapter.bin)
    if not bin_path:
        failed = store.update_job(
            job_id,
            {
                "status": "failed",
                "exitCode": -1,
                "endedAt": store.utc_now(),
                "timedOut": False,
            },
        )
        _publish_job_event("job.failed", failed, agent_name=publisher_agent)
        raise AgentNotInstalledError(adapter)
    assert_arg_mode_spawn_safe(adapter, bin_path)

    inv = build_invocation(
        adapter,
        prompt=meta["prompt"],
        model=meta.get("model"),
        write=bool(meta.get("write")),
    )
    paths = store.job_paths(job_id)
    result = await spawn_foreground(
        bin_path,
        inv["args"],
        cwd=meta.get("cwd") or os.getcwd(),
        timeout_ms=int(inv["timeoutMs"]),
        input_text=inv["input"],
    )
    paths["stdout"].write_text(result["stdout"], encoding="utf-8", errors="replace")
    paths["stderr"].write_text(result["stderr"], encoding="utf-8", errors="replace")
    paths["result"].write_text(result["stdout"], encoding="utf-8", errors="replace")

    was_cancelled = store.get_job(job_id)
    if was_cancelled and was_cancelled.get("status") == "cancelled":
        status = "cancelled"
    elif result["timedOut"] or result["exitCode"] != 0:
        status = "failed"
    else:
        status = "done"

    final = store.update_job(
        job_id,
        {
            "status": status,
            "exitCode": result["exitCode"],
            "timedOut": result["timedOut"],
            "endedAt": store.utc_now(),
        },
    )
    if status == "done":
        _publish_job_event("job.completed", final, agent_name=publisher_agent)
    elif status == "failed":
        _publish_job_event("job.failed", final, agent_name=publisher_agent)
    return final


def cancel_job(job_id: str) -> dict[str, Any]:
    meta = store.get_job(job_id)
    if not meta:
        raise ValueError(f"No such job: {job_id}")
    if meta.get("status") != "running":
        return meta
    pid = meta.get("pid")
    if pid is not None:
        kill_tree(int(pid))
    return store.update_job(
        job_id,
        {
            "status": "cancelled",
            "endedAt": store.utc_now(),
        },
    )


def job_result(job_id: str) -> dict[str, Any]:
    meta = store.get_job(job_id)
    if not meta:
        raise ValueError(f"No such job: {job_id}")
    result_path = store.job_paths(job_id)["result"]
    text = result_path.read_text(encoding="utf-8", errors="replace") if result_path.is_file() else None
    return {"meta": meta, "result": text}
