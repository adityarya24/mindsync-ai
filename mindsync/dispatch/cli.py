"""CLI entry: python -m mindsync.dispatch.cli <run|status|result|review|cancel|agents|models|_supervise>."""

from __future__ import annotations

import asyncio
import sys

from mindsync.dispatch.adapters import load_adapters, user_config_path
from mindsync.dispatch.memory_lifecycle import DEFAULT_MEMORY_MODE
from mindsync.dispatch.review import format_review, verdict
from mindsync.dispatch.runner import (
    cancel_job,
    describe_empty_result,
    job_result,
    run_task,
    supervise_job,
)
from mindsync.dispatch.store import get_job, list_jobs, reconcile_job
from mindsync.orchestration import load_policy


def parse_run_args(argv: list[str]) -> dict:
    flags: dict = {
        "model": None,
        "effort": None,
        "role": None,
        "write": False,
        "background": False,
        "cwd": None,
        "worktree": False,
        "checks": [],
        "required_capabilities": [],
        "exclude_agents": [],
        "memory_project": None,
        "memory_mode": DEFAULT_MEMORY_MODE,
    }
    rest: list[str] = []
    i = 0
    usage_str = (
        'usage: dispatch run <agent|auto> "task..." [options]\n'
        '   or: dispatch run --role <name> "task..." [options]   (no agent: the role picks one)\n'
        "options: [--background] [--write] [--model <m>] [--effort <level>] "
        "[--worktree] [--cwd <path>] [--check <command>]... "
        "[--capability <name>]... [--exclude-agent <name>]... "
        "[--memory-project <key>] [--memory-mode <auto|explicit|off>]"
    )
    while i < len(argv):
        a = argv[i]
        if a == "--role":
            i += 1
            flags["role"] = argv[i] if i < len(argv) else None
        elif a == "--model":
            i += 1
            flags["model"] = argv[i] if i < len(argv) else None
        elif a == "--effort":
            i += 1
            flags["effort"] = argv[i] if i < len(argv) else None
        elif a == "--cwd":
            i += 1
            flags["cwd"] = argv[i] if i < len(argv) else None
        elif a == "--check":
            i += 1
            if i < len(argv):
                flags["checks"].append(argv[i])
        elif a == "--capability":
            i += 1
            if i < len(argv):
                flags["required_capabilities"].append(argv[i])
        elif a == "--exclude-agent":
            i += 1
            if i < len(argv):
                flags["exclude_agents"].append(argv[i])
        elif a == "--memory-project":
            i += 1
            flags["memory_project"] = argv[i] if i < len(argv) else None
        elif a == "--memory-mode":
            i += 1
            # Unlike --memory-project, None is not a valid value here: it
            # surfaces as "memory_mode must be one of: ..." and never
            # mentions that the flag was simply missing its argument.
            if i >= len(argv):
                raise SystemExit(usage_str)
            flags["memory_mode"] = argv[i]
        elif a == "--write":
            flags["write"] = True
        elif a == "--background":
            flags["background"] = True
        elif a == "--worktree":
            flags["worktree"] = True
        else:
            rest.append(a)
        i += 1

    role = flags.pop("role")

    # With --role there is no positional agent, so every remaining word is prompt.
    # Deciding by "does the first word happen to name an agent" looked convenient but
    # is unworkable: prompts talk about agents by name ("codex is hanging on Windows"),
    # so that rule rejects valid tasks, and it silently swallows a mistyped agent name
    # into the prompt when it does not match.
    if role is not None:
        agent = None
        prompt = " ".join(rest).strip()
    else:
        if len(rest) < 2:
            raise SystemExit(usage_str)
        agent = rest[0]
        prompt = " ".join(rest[1:]).strip()

    if (agent is None and role is None) or (agent is not None and role is not None) or not prompt:
        raise SystemExit(usage_str)

    return {"agent": agent, "role": role, "prompt": prompt, **flags}


def fmt_job(m: dict) -> str:
    exit_bit = ""
    if m.get("exitCode") is not None:
        to = ", TIMED OUT" if m.get("timedOut") else ""
        exit_bit = f" (exit {m['exitCode']}{to})"
    verdict_bit = ""
    if m.get("checkResults"):
        v = verdict(m)
        v_str = "pass" if v["passed"] else "fail"
        verdict_bit = f"  verdict: {v_str}"
    prompt = m.get("prompt") or ""
    if len(prompt) > 100:
        prompt = prompt[:100] + "…"
    if m.get("role"):
        agent_str = f"{m['agent']} (role: {m['role']})"
    elif m.get("routing"):
        agent_str = f"{m['agent']} (auto)"
    else:
        agent_str = m["agent"]
    lines = [
        f"[{m['id']}] {agent_str} — {m['status']}{exit_bit}{verdict_bit}",
        f"  prompt: {prompt}",
    ]
    if m.get("worktreePath") and m.get("worktreeKept"):
        lines.append(f"  worktree kept: {m['worktreePath']} (branch {m['branch']})")
    if m.get("routing"):
        lines.append(f"  route: {m['routing']['reason']}")
    for warning in m.get("warnings", []):
        lines.append(f"  warning: {warning}")
    return "\n".join(lines)


async def _async_main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: dispatch "
            "<run|status|result|review|cancel|agents|models|roles|_supervise> ...",
            file=sys.stderr,
        )
        return 1
    cmd, *rest = argv
    if cmd == "run":
        opts = parse_run_args(rest)
        r = await run_task(**opts)
        job = r["job"]
        route_info = job.get("routing")
        if route_info and load_policy().announce:
            print(f"AUTO ROUTE: {route_info['reason']}")
        for warning in job.get("warnings", []):
            print(f"WARNING: {warning}", file=sys.stderr)
        if opts["background"]:
            wt_info = (
                f"\nworktree: {job['worktreePath']}  (branch: {job['branch']})"
                if job.get("worktreePath")
                else ""
            )
            print(
                f"Started background job {job['id']} (agent: {job['agent']}).{wt_info}\n"
                f"Check: python -m mindsync.dispatch.cli status {job['id']}"
            )
            return 0
        wt_info = (
            f"worktree: {job['worktreePath']}  (branch: {job['branch']})\n"
            if job.get("worktreePath")
            else ""
        )
        print(f"{wt_info}{r.get('result') or describe_empty_result(job)}")

        v_pass = True
        if opts.get("checks"):
            v = verdict(job)
            v_pass = v["passed"]
            print(f"\nVERDICT: {'PASS' if v_pass else 'FAIL'}")

        if job.get("status") != "done" or not v_pass:
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

    if cmd == "review":
        if not rest:
            print("usage: dispatch review <job-id>", file=sys.stderr)
            return 1
        meta = get_job(rest[0])
        if not meta:
            print(f"No such job: {rest[0]}", file=sys.stderr)
            return 1
        reconcile_job(meta)
        fresh = get_job(rest[0]) or meta
        print(format_review(fresh))
        v = verdict(fresh)
        return 0 if v["passed"] else 1

    if cmd == "cancel":
        if not rest:
            print("usage: dispatch cancel <job-id>", file=sys.stderr)
            return 1
        meta = cancel_job(rest[0])
        print(f"Job {meta['id']}: {meta['status']}")
        return 0

    if cmd == "agents":
        from mindsync.roster import describe_agents

        rows = describe_agents()
        for row in rows:
            binary = "yes" if row["binary_present"] else "no"
            mcp = "yes" if row["mcp_installed"] else "no"
            routable = "yes" if row["routable"] else "no"
            label = f" — {row['display_name']}" if row["display_name"] != row["name"] else ""
            print(
                f"{row['name']}{label}  binary={binary}  mcp={mcp}  "
                f"routable={routable}  bin={row['bin']}"
            )
            print(f"    {row['mcp_detail']}")
            print(f"    capabilities: {','.join(row['capabilities'])}")
        print(f"\nCustom agents: {user_config_path()}")
        return 0

    if cmd == "models":
        from mindsync.dispatch.adapters import (
            list_models as adapter_list_models,
            resolve_adapter,
        )

        agents_to_list = [resolve_adapter(rest[0])] if rest else load_adapters().values()
        for a in agents_to_list:
            print(f"Models for {a.name}:")
            models = adapter_list_models(a)
            if not models:
                print("  (none discovered)")
            for m in models:
                marker = "  (default)" if m == a.defaultModel else ""
                print(f"  {m}{marker}")
        return 0

    if cmd == "roles":
        from mindsync.dispatch.adapters import load_roles
        roles = load_roles()
        if not roles:
            print(f"No roles are configured; add a 'roles' block to {user_config_path()}")
            return 0
        width = max((len(r.name) for r in roles.values()), default=10)
        width = max(width, 10)
        for r in roles.values():
            parts = [f"{r.name:<{width}} -> {r.agent}"]
            if r.model:
                parts.append(f"model: {r.model}")
            if r.effort:
                parts.append(f"effort: {r.effort}")
            print("   ".join(parts))
        return 0

    if cmd == "_supervise":
        if not rest:
            print("usage: dispatch _supervise <job-id>", file=sys.stderr)
            return 1
        await supervise_job(rest[0])
        return 0

    print(
        "usage: dispatch <run|status|result|review|cancel|agents|models|roles|_supervise> ...",
        file=sys.stderr,
    )
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

