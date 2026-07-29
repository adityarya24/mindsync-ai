"""CLI entry: python -m mindsync.dispatch.cli <run|status|result|cancel|agents|_supervise>."""

from __future__ import annotations

import asyncio
import sys

from mindsync.dispatch.adapters import load_adapters, user_config_path
from mindsync.dispatch.runner import (
    cancel_job,
    describe_empty_result,
    job_result,
    run_task,
    supervise_job,
)
from mindsync.dispatch.store import get_job, list_jobs, reconcile_job


def parse_run_args(argv: list[str]) -> dict:
    flags: dict = {"model": None, "write": False, "background": False, "cwd": None, "worktree": False}
    rest: list[str] = []
    i = 0
    usage_str = 'usage: dispatch run <agent> "task..." [--background] [--write] [--model <m>] [--worktree] [--cwd <path>]'
    while i < len(argv):
        a = argv[i]
        if a == "--model":
            i += 1
            flags["model"] = argv[i] if i < len(argv) else None
        elif a == "--cwd":
            i += 1
            flags["cwd"] = argv[i] if i < len(argv) else None
        elif a == "--write":
            flags["write"] = True
        elif a == "--background":
            flags["background"] = True
        elif a == "--worktree":
            flags["worktree"] = True
        else:
            rest.append(a)
        i += 1
    if not rest:
        raise SystemExit(usage_str)
    agent = rest[0]
    prompt = " ".join(rest[1:]).strip()
    if not agent or not prompt:
        raise SystemExit(usage_str)
    return {"agent": agent, "prompt": prompt, **flags}


def fmt_job(m: dict) -> str:
    exit_bit = ""
    if m.get("exitCode") is not None:
        to = ", TIMED OUT" if m.get("timedOut") else ""
        exit_bit = f" (exit {m['exitCode']}{to})"
    prompt = m.get("prompt") or ""
    if len(prompt) > 100:
        prompt = prompt[:100] + "…"
    lines = [f"[{m['id']}] {m['agent']} — {m['status']}{exit_bit}", f"  prompt: {prompt}"]
    if m.get("worktreePath") and m.get("worktreeKept"):
        lines.append(f"  worktree kept: {m['worktreePath']} (branch {m['branch']})")
    return "\n".join(lines)


async def _async_main(argv: list[str]) -> int:
    if not argv:
        print("usage: dispatch <run|status|result|cancel|agents|_supervise> ...", file=sys.stderr)
        return 1
    cmd, *rest = argv
    if cmd == "run":
        opts = parse_run_args(rest)
        r = await run_task(**opts)
        job = r["job"]
        if opts["background"]:
            wt_info = f"\nworktree: {job['worktreePath']}  (branch: {job['branch']})" if job.get("worktreePath") else ""
            print(
                f"Started background job {job['id']} (agent: {job['agent']}).{wt_info}\n"
                f"Check: python -m mindsync.dispatch.cli status {job['id']}"
            )
            return 0
        wt_info = f"worktree: {job['worktreePath']}  (branch: {job['branch']})\n" if job.get("worktreePath") else ""
        print(f"{wt_info}{r.get('result') or describe_empty_result(job)}")
        if job.get("status") != "done":
            print(
                f"\n[job {job['id']} {job['status']}"
                f"{' — timed out' if job.get('timedOut') else ''}, exit {job.get('exitCode')}]",
                file=sys.stderr,
            )
            return 1
        return 0

    if cmd == "status":
        if rest:
            meta = get_job(rest[0])
            if not meta:
                print(f"No such job: {rest[0]}", file=sys.stderr)
                return 1
            jobs_list = [reconcile_job(meta)]
        else:
            jobs_list = list_jobs()
        print("\n".join(fmt_job(m) for m in jobs_list) if jobs_list else "No jobs yet.")
        return 0

    if cmd == "result":
        if not rest:
            print("usage: dispatch result <job-id>", file=sys.stderr)
            return 1
        data = job_result(rest[0])
        reconcile_job(data["meta"])
        fresh = get_job(data["meta"]["id"])
        print(data["result"] or describe_empty_result(fresh or data["meta"]))
        return 0

    if cmd == "cancel":
        if not rest:
            print("usage: dispatch cancel <job-id>", file=sys.stderr)
            return 1
        meta = cancel_job(rest[0])
        print(f"Job {meta['id']}: {meta['status']}")
        return 0

    if cmd == "agents":
        for a in load_adapters().values():
            label = f" — {a.displayName}" if a.displayName else ""
            print(f"{a.name}{label} (bin: {a.bin})")
        print(f"\nCustom agents: {user_config_path()}")
        return 0

    if cmd == "_supervise":
        if not rest:
            print("usage: dispatch _supervise <job-id>", file=sys.stderr)
            return 1
        await supervise_job(rest[0])
        return 0

    print("usage: dispatch <run|status|result|cancel|agents|_supervise> ...", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    # Allow `python -m mindsync.dispatch.cli` with argparse-less argv
    try:
        code = asyncio.run(_async_main(args))
    except (ValueError, RuntimeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
