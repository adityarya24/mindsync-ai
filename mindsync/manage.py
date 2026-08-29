"""Human-facing `mindsync setup|doctor|config` command implementation."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from typing import Any

from mindsync.onboarding import CLI_SPECS, doctor, setup
from mindsync.orchestration import (
    load_policy,
    policy_path,
    project_on_complete,
    update_policy,
)
from mindsync.roster import describe_agents, register_agent


def _non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return value


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _job_timeout_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0 or value > 3600:
        raise argparse.ArgumentTypeError("must be greater than 0 and at most 3600")
    return value


def _parse_value(key: str, raw: str) -> Any:
    leaf = key.rsplit(".", 1)[-1]
    if leaf in {"announce", "avoidHumanFacingAgent"}:
        normalized = raw.strip().lower()
        if normalized not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
        return normalized == "true"
    if leaf == "maxParallel":
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
    if leaf == "onComplete":
        normalized = raw.strip().lower()
        if normalized not in {"pr", "branch", "none"}:
            raise ValueError(f"{key} must be one of: pr, branch, none")
        return normalized
    return raw


def _print_setup(result: dict[str, Any]) -> None:
    prefix = "DRY RUN — " if result["dry_run"] else ""
    print(f"{prefix}MindSync {result['version']} orchestration mode: {result['mode']}")
    for action in result["actions"]:
        detail = f" — {action['detail']}" if action.get("detail") else ""
        print(f"  {action['cli']:<8} {action['action']}{detail}")
    if result["dry_run"]:
        print("No policy or CLI configuration was changed.")
    else:
        print(f"Policy: {result['policy_file']}")
        print("Restart configured CLIs so they load MindSync and its orchestration instructions.")
        if any(
            action.get("cli") == "codex-hook" and action.get("action") == "configured"
            for action in result["actions"]
        ):
            print(
                "Codex standalone memory hooks were registered at ~/.codex/hooks.json. "
                "Trust them in Codex if prompted, then restart the session."
            )


def _print_doctor(report: dict[str, Any]) -> None:
    state = "OK" if report["ok"] else "FAIL"
    print(f"MindSync {report['version']} · {state} · Python {report['python']}")
    if report["policy_error"]:
        print(f"  policy   FAIL — {report['policy_error']}")
    else:
        policy = report["policy"]
        print(
            "  policy   OK — "
            f"mode={policy['mode']} maxParallel={policy['maxParallel']} "
            f"announce={str(policy['announce']).lower()}"
        )
    for cli in report["clis"]:
        if not cli["installed"]:
            state = "not installed"
        elif not cli["supported"]:
            state = (
                "installed, worker backend"
                if cli.get("worker_only")
                else "installed, MCP registration unsupported"
            )
            if cli.get("detail"):
                state += f" — {cli['detail']}"
        elif cli["configured"]:
            state = "configured"
            if cli.get("detail"):
                state += f" — {cli['detail']}"
        else:
            state = "installed, not configured"
            if cli.get("detail"):
                state += f" — {cli['detail']}"
        print(f"  {cli['cli']:<8} {state}")
    families = report["available_worker_families"]
    available = [
        f"{family} [{', '.join(backends)}]" if len(backends) > 1 else backends[0]
        for family, backends in families.items()
    ]
    print(f"  workers  {', '.join(available) if available else 'none available'}")
    memory = report.get("memory") or {}
    if memory.get("db_error"):
        print(f"  memory   FAIL — {memory['db_error']}")
    else:
        git_bit = (
            f"git identity {memory['git_project']}"
            if memory.get("git_project")
            else "no git identity in this directory (dispatch memory stays off here)"
        )
        print(
            f"  memory   OK — {memory.get('sessions', 0)} sessions, "
            f"{memory.get('facts', 0)} facts, {git_bit}"
        )
    hooks = memory.get("codex_hooks") or {}
    if hooks.get("configured"):
        shown = ", ".join(hooks.get("paths") or [])
        print(f"  hooks    configured — {shown}")
    else:
        print(
            "  hooks    missing — Codex standalone memory is off until you run mindsync setup"
        )
    for issue in report["issues"]:
        print(f"  issue    {issue}")


def _print_register(result: dict[str, Any]) -> None:
    prefix = "DRY RUN — " if result["dry_run"] else ""
    roster = result["roster"]
    mcp = result["mcp"]
    verify = result["verify"]
    print(f"{prefix}MindSync {result['version']} register {roster.get('action')}")
    print(f"  roster   {roster.get('action')} — {result['path']}")
    mcp_cli = mcp.get("cli") or "n/a"
    mcp_detail = f" — {mcp['detail']}" if mcp.get("detail") else ""
    print(f"  mcp      {mcp_cli} {mcp.get('action')}{mcp_detail}")
    binary = "yes" if verify.get("binary_present") else "no"
    version = verify.get("version") or verify.get("detail") or ""
    version_bit = f" ({version})" if version else ""
    print(f"  binary   {binary}{version_bit}")
    print(f"  mcp-in   {'yes' if verify.get('mcp_installed') else 'no'}")
    print(f"  routable {'yes' if verify.get('routable') else 'no'}")
    print(f"  caps     {', '.join(result.get('capabilities') or ['general'])}")


def _print_agents(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No agents in the roster.")
        return
    for row in rows:
        binary = "yes" if row["binary_present"] else "no"
        mcp = "yes" if row["mcp_installed"] else "no"
        routable = "yes" if row["routable"] else "no"
        print(
            f"{row['name']:<12} binary={binary}  mcp={mcp}  routable={routable}  "
            f"bin={row['bin']}  source={row['source']}"
        )
        print(f"             {row['mcp_detail']}")
        print(f"             capabilities: {', '.join(row['capabilities'])}")
    from mindsync.dispatch.adapters import user_config_path

    print(f"\nUser roster: {user_config_path()}")


def _format_db_size(size_bytes: int) -> str:
    size_kb = size_bytes / 1024
    if size_kb < 1024:
        return f"{size_kb:.1f} KB"
    return f"{size_kb / 1024:.1f} MB"


def _print_memory_stats(report: dict[str, Any]) -> None:
    print(
        f"MindSync session memory — {report['total_sessions']} sessions "
        f"({report['active_sessions']} active), "
        f"{report['total_checkpoints']} checkpoints, "
        f"{report['total_facts']} facts "
        f"({report['generated_facts']} generated), "
        f"{report['pending_consolidations']} pending proposals, "
        f"{_format_db_size(report['db_size_bytes'])}"
    )
    if not report["projects"]:
        print("  no projects recorded")
    for project in report["projects"]:
        print(
            f"  {project['project_key']}: {project['sessions']} sessions "
            f"({project['active_sessions']} active)"
        )


def _print_memory_list(entries: list[dict[str, Any]]) -> None:
    if not entries:
        print("No sessions found.")
        return
    for entry in entries:
        state = entry.get("session_status") or "unknown"
        ended = entry.get("ended_at") or "in progress"
        print(
            f"[{entry['session_id']}] {state} · {entry['agent']} · "
            f"project: {entry['project_key']} · started: {entry['started_at']} · "
            f"ended: {ended} · checkpoints: {entry.get('checkpoint_count', 0)}"
        )


def _print_memory_show(session: dict[str, Any]) -> None:
    print(f"Session {session['session_id']}")
    print(f"  project:   {session['project_key']}")
    print(f"  agent:     {session['agent']}")
    if session.get("workspace"):
        print(f"  workspace: {session['workspace']}")
    if session.get("branch"):
        print(f"  branch:    {session['branch']}")
    if session.get("goal"):
        print(f"  goal:      {session['goal']}")
    print(f"  status:    {session.get('session_status') or 'unknown'}")
    print(f"  started:   {session['started_at']}")
    if session.get("ended_at"):
        print(f"  ended:     {session['ended_at']}")
    checkpoints = session.get("checkpoints") or []
    print(f"  checkpoints ({len(checkpoints)}):")
    for checkpoint in checkpoints:
        summary = checkpoint.get("status") or "no status"
        parts = [f"summary: {summary}"]
        for field in ("decisions", "files_changed", "tests", "pending", "blockers", "durable_facts"):
            if checkpoint.get(field):
                parts.append(f"{field}: {json.dumps(checkpoint[field], ensure_ascii=False)}")
        print(f"    - {checkpoint['timestamp']} · " + " · ".join(parts))


def _print_memory_prune(result: dict[str, Any]) -> None:
    prefix = "DRY RUN — " if result["dry_run"] else ""
    deleted = (
        f"{result['deleted']} deleted" if result["deleted"] is not None else "nothing deleted"
    )
    print(
        f"{prefix}prune candidates: {result['candidates']} ({deleted}) · "
        f"protected durable-fact sessions: {result['protected_durable']} · "
        f"kept by --keep-last: {result['kept_by_keep_last']}"
    )
    for session_id in result["session_ids"]:
        print(f"  {session_id}")
    if result["dry_run"] and result["candidates"]:
        print("Re-run with --yes to delete these sessions.")


def _run_memory_command(args: argparse.Namespace) -> int:
    from mindsync import memory as memory_mod

    try:
        if args.memory_command == "stats":
            report = memory_mod.memory_stats()
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                _print_memory_stats(report)
            return 0
        if args.memory_command == "list":
            entries = memory_mod.memory_list(
                project_key=args.project, limit=args.limit
            )
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                _print_memory_list(entries)
            return 0
        if args.memory_command == "show":
            session = memory_mod.memory_show(args.session_id)
            if args.json:
                print(json.dumps(session, indent=2))
            else:
                _print_memory_show(session)
            return 0
        if args.memory_command == "prune":
            result = memory_mod.memory_prune(
                project_key=args.project,
                older_than_days=args.older_than_days,
                keep_last=args.keep_last,
                dry_run=not args.yes,
            )
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                _print_memory_prune(result)
            return 0
        if args.memory_command == "recall":
            result = memory_mod.memory_recall(
                project_key=args.project,
                query=args.query,
                limit=args.limit,
                min_similarity=args.min_similarity,
                model=args.model,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.memory_command == "consolidate":
            result = memory_mod.memory_consolidate_preview(
                project_key=args.project,
                limit=args.limit,
                min_similarity=args.min_similarity,
                embedding_model=args.embedding_model,
                consolidation_model=args.model,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.memory_command == "apply":
            result = memory_mod.memory_consolidation_apply(args.proposal_id)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.memory_command == "undo":
            result = memory_mod.memory_consolidation_undo(args.fact_id)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.memory_command == "proposals":
            result = memory_mod.memory_consolidation_list(
                project_key=args.project,
                status=args.status,
                limit=args.limit,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"Memory database error: {exc}", file=sys.stderr)
        return 1
    print(f"Unknown memory command '{args.memory_command}'.", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mindsync")
    sub = parser.add_subparsers(dest="command")

    setup_parser = sub.add_parser(
        "setup",
        help="Detect installed CLIs, register MCP hosts, and add PATH agent CLIs to the roster",
    )
    setup_parser.add_argument("--mode", choices=["auto", "suggest", "off"])
    setup_parser.add_argument("--cli", action="append", choices=sorted(CLI_SPECS))
    setup_parser.add_argument("--dry-run", action="store_true")
    setup_parser.add_argument("--force", action="store_true")
    setup_parser.add_argument(
        "--no-hooks",
        action="store_true",
        help="Skip Codex standalone memory hook registration",
    )
    setup_parser.add_argument(
        "--no-discover",
        action="store_true",
        help="Do not scan PATH for extra coding-agent CLIs",
    )

    doctor_parser = sub.add_parser("doctor", help="Diagnose policy, CLI registration, workers, and memory")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip probing host CLIs for MCP registration (does not start any CLI; "
             "MCP columns report 'not probed'). Use on a machine running a bound chat channel.",
    )

    register_parser = sub.add_parser(
        "register",
        help="Add an agent to the user dispatch roster and install MCP when the CLI allows it",
    )
    register_parser.add_argument("--name", required=True, help="Roster name, e.g. vidur")
    register_parser.add_argument("--bin", dest="bin_name", required=True, help="Executable on PATH")
    register_parser.add_argument(
        "--capability",
        dest="capabilities",
        action="append",
        help="Known capability tag (repeatable). Heavy tags need --confirm",
    )
    register_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Allow security, large-context, or multimodal capability tags",
    )
    register_parser.add_argument("--display-name")
    register_parser.add_argument("--family")
    register_parser.add_argument("--priority", type=int, default=40)
    register_parser.add_argument("--dry-run", action="store_true")
    register_parser.add_argument("--force", action="store_true")
    register_parser.add_argument("--json", action="store_true")

    agents_parser = sub.add_parser(
        "agents",
        help="Show whether each roster agent is binary-present, MCP-installed, and routable",
    )
    agents_parser.add_argument("--json", action="store_true")
    agents_parser.add_argument(
        "--check-mcp",
        action="store_true",
        help="Also probe each host CLI for MindSync MCP registration (starts the CLI; "
             "may disrupt a running host such as a bound chat channel)",
    )

    config_parser = sub.add_parser("config", help="Read or change orchestration policy")
    config_parser.add_argument("key", nargs="?")
    config_parser.add_argument("value", nargs="?")
    config_parser.add_argument(
        "--project",
        metavar="PATH",
        help="Apply to one repository instead of every project (onComplete only)",
    )

    worker_parser = sub.add_parser("worker", help="Poll or process remote queue jobs")
    worker_parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
    worker_parser.add_argument("--poll-secs", type=_positive_int, help="Poll interval in seconds")
    worker_parser.add_argument(
        "--stale-secs", type=_positive_int, help="Stale claim threshold in seconds"
    )
    worker_parser.add_argument("--worker-id", type=str, help="Worker identifier")
    worker_parser.add_argument(
        "--allow-orchestrator",
        action="store_true",
        help="Allow explicitly submitted orchestrator jobs for this worker process",
    )

    submit_parser = sub.add_parser("submit", help="Submit a job to the remote queue")
    submit_parser.add_argument("--repo", required=True, help="Path to target repository")
    submit_parser.add_argument("--task-file", help="Path to task file containing prompt")
    submit_parser.add_argument("--prompt", help="Prompt text for the job")
    submit_parser.add_argument(
        "--agent", help="Configured agent (required for orchestrator mode unless --role is used)"
    )
    submit_parser.add_argument(
        "--role", help="Configured role (required for orchestrator mode unless --agent is used)"
    )
    submit_parser.add_argument("--branch", help="Target git branch")
    submit_parser.add_argument(
        "--timeout-seconds",
        type=_job_timeout_seconds,
        default=900.0,
        help="Agent execution timeout in seconds (greater than 0, at most 3600; default: 900)",
    )
    submit_parser.add_argument(
        "--commit",
        action="store_true",
        help="After a successful run, stage and commit the worker checkout (never push)",
    )
    submit_parser.add_argument(
        "--execution-mode",
        "--mode",
        dest="execution_mode",
        choices=["worker", "orchestrator"],
        default="worker",
        help="Execution boundary; orchestrator requires --agent or --role (default: worker)",
    )

    status_parser = sub.add_parser("status", help="Get status of a remote job or list remote jobs")
    status_parser.add_argument("job_id", nargs="?", help="Job ID to query")

    memory_parser = sub.add_parser("memory", help="Inspect or maintain local session memory")
    memory_sub = memory_parser.add_subparsers(dest="memory_command", required=True)

    memory_stats_parser = memory_sub.add_parser(
        "stats", help="Session/checkpoint totals, projects, and database size"
    )
    memory_stats_parser.add_argument("--json", action="store_true")

    memory_list_parser = memory_sub.add_parser(
        "list", help="List sessions, most recently active first"
    )
    memory_list_parser.add_argument("--project", help="Filter by project key")
    memory_list_parser.add_argument("--limit", type=_positive_int, default=50)
    memory_list_parser.add_argument("--json", action="store_true")

    memory_show_parser = memory_sub.add_parser(
        "show", help="Show one session with all of its checkpoints"
    )
    memory_show_parser.add_argument("session_id", help="32-character session identifier")
    memory_show_parser.add_argument("--json", action="store_true")

    memory_prune_parser = memory_sub.add_parser(
        "prune", help="Delete old ended sessions (dry-run by default)"
    )
    memory_prune_parser.add_argument("--project", help="Limit pruning to one project key")
    memory_prune_parser.add_argument(
        "--older-than-days",
        type=_positive_int,
        help="Only consider sessions that ended more than N days ago",
    )
    memory_prune_parser.add_argument(
        "--keep-last",
        type=_non_negative_int,
        default=0,
        help="Always keep the most recent N ended sessions per project (default: 0)",
    )
    memory_prune_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete; without this flag the command is a dry run",
    )
    memory_prune_parser.add_argument("--json", action="store_true")

    memory_recall_parser = memory_sub.add_parser(
        "recall", help="Recall project facts related to a cue using local embeddings"
    )
    memory_recall_parser.add_argument("--project", required=True, help="Project key")
    memory_recall_parser.add_argument("--query", required=True, help="Recall cue")
    memory_recall_parser.add_argument("--limit", type=_positive_int, default=5)
    memory_recall_parser.add_argument("--min-similarity", type=float, default=0.0)
    memory_recall_parser.add_argument("--model", help="Override local embedding model")

    memory_consolidate_parser = memory_sub.add_parser(
        "consolidate",
        help="Create a reviewable consolidation proposal (does not apply it)",
    )
    memory_consolidate_parser.add_argument("--project", required=True, help="Project key")
    memory_consolidate_parser.add_argument("--limit", type=_positive_int, default=5)
    memory_consolidate_parser.add_argument(
        "--min-similarity", type=float, default=0.45
    )
    memory_consolidate_parser.add_argument(
        "--embedding-model", help="Override local embedding model"
    )
    memory_consolidate_parser.add_argument(
        "--model", help="Override local consolidation model"
    )

    memory_apply_parser = memory_sub.add_parser(
        "apply", help="Apply a reviewed consolidation proposal"
    )
    memory_apply_parser.add_argument("proposal_id")

    memory_undo_parser = memory_sub.add_parser(
        "undo", help="Undo one generated consolidation fact"
    )
    memory_undo_parser.add_argument("fact_id")

    memory_proposals_parser = memory_sub.add_parser(
        "proposals", help="List consolidation proposals for review or audit"
    )
    memory_proposals_parser.add_argument("--project", help="Filter by project key")
    memory_proposals_parser.add_argument(
        "--status", choices=["pending", "applied", "superseded", "undone"]
    )
    memory_proposals_parser.add_argument("--limit", type=_positive_int, default=50)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "setup":
        mode = args.mode
        if mode is None and sys.stdin.isatty():
            answer = input("Orchestration mode [auto/suggest/off] (auto): ").strip().lower()
            mode = answer or "auto"
            if mode not in {"auto", "suggest", "off"}:
                print("Invalid mode. Choose auto, suggest, or off.", file=sys.stderr)
                return 2
        try:
            result = setup(
                mode=mode or "auto",
                cli_names=args.cli,
                dry_run=args.dry_run,
                force=args.force,
                install_hooks=not args.no_hooks,
                discover=False if args.no_discover else None,
            )
        except (OSError, TimeoutError, ValueError) as exc:
            print(f"Setup failed: {exc}", file=sys.stderr)
            return 1
        _print_setup(result)
        return 0 if result["ok"] else 1

    if args.command == "doctor":
        try:
            report = doctor(probe_hosts=not getattr(args, "no_probe", False))
        except (OSError, TimeoutError, ValueError) as exc:
            print(f"Doctor failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_doctor(report)
        return 0 if report["ok"] else 1

    if args.command == "register":
        try:
            result = register_agent(
                name=args.name,
                bin_name=args.bin_name,
                capabilities=args.capabilities,
                confirm=args.confirm,
                display_name=args.display_name,
                family=args.family,
                routing_priority=args.priority,
                dry_run=args.dry_run,
                force=args.force,
            )
        except (OSError, ValueError) as exc:
            print(f"Register failed: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_register(result)
        return 0 if result["ok"] else 1

    if args.command == "agents":
        try:
            rows = describe_agents(probe_hosts=getattr(args, "check_mcp", False))
        except (OSError, ValueError) as exc:
            print(f"Agents failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            _print_agents(rows)
        return 0

    if args.command == "config":
        project = getattr(args, "project", None)
        if args.key is None:
            print(json.dumps(load_policy().model_dump(), indent=2))
            print(f"Policy: {policy_path()}")
            return 0
        if args.value is None:
            policy = load_policy()
            leaf = args.key.rsplit(".", 1)[-1]
            if leaf not in policy.model_dump():
                print(f"Unknown orchestration setting '{args.key}'.", file=sys.stderr)
                return 2
            if project is not None and leaf == "onComplete":
                print(json.dumps(project_on_complete(project, policy)))
                return 0
            print(json.dumps(policy.model_dump()[leaf]))
            return 0
        try:
            policy = update_policy(
                args.key, _parse_value(args.key, args.value), project=project
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(policy.model_dump(), indent=2))
        print("Restart configured CLIs to refresh MindSync's session instructions.")
        return 0

    if args.command == "worker":
        from mindsync.config import settings
        from mindsync.remote_queue import RemoteQueue, run_worker_loop, run_worker_once

        queue = RemoteQueue()
        worker_id = args.worker_id or settings.worker_id
        poll_secs = (
            args.poll_secs if args.poll_secs is not None else settings.worker_poll_seconds
        )
        stale_secs = (
            args.stale_secs if args.stale_secs is not None else settings.worker_claim_stale_seconds
        )
        allowed_repos = settings.allowed_repos

        if not queue.remote_root:
            print("Worker requires MINDSYNC_REMOTE_ROOT.", file=sys.stderr)
            return 2
        if not allowed_repos:
            print(
                "Worker requires a non-empty MINDSYNC_WORKER_ALLOWED_REPOS allow-list.",
                file=sys.stderr,
            )
            return 2

        if args.once:
            res = run_worker_once(
                queue=queue,
                worker_id=worker_id,
                allowed_repos=allowed_repos,
                stale_seconds=stale_secs,
                allow_orchestrator=True if args.allow_orchestrator else None,
            )
            if res:
                print(f"Processed job {res['job_id']}: status={res['status']}")
            else:
                print("No pending jobs.")
            return 0
        else:
            print(
                f"Starting MindSync worker '{worker_id}' (poll={poll_secs}s, stale={stale_secs}s)..."
            )
            try:
                run_worker_loop(
                    queue=queue,
                    worker_id=worker_id,
                    allowed_repos=allowed_repos,
                    poll_seconds=poll_secs,
                    stale_seconds=stale_secs,
                    allow_orchestrator=True if args.allow_orchestrator else None,
                )
            except KeyboardInterrupt:
                print("\nWorker stopped.")
            return 0

    if args.command == "submit":
        if not args.prompt and not args.task_file:
            print("Error: Either --prompt or --task-file must be provided.", file=sys.stderr)
            return 2
        prompt = args.prompt or ""
        if not prompt and args.task_file:
            from pathlib import Path

            tf = Path(args.task_file)
            if not tf.is_file():
                print(f"Error: Task file not found: {args.task_file}", file=sys.stderr)
                return 1
            prompt = tf.read_text(encoding="utf-8")

        from mindsync.remote_queue import RemoteQueue

        queue = RemoteQueue()
        try:
            job_id = queue.submit_job(
                repo_path=args.repo,
                prompt=prompt,
                task_file=args.task_file,
                agent=args.agent,
                role=args.role,
                branch=args.branch,
                timeout_seconds=args.timeout_seconds,
                commit=args.commit,
                execution_mode=args.execution_mode,
            )
            print(job_id)
            return 0
        except Exception as exc:
            from mindsync.bridge import _sanitize_error

            print(f"Submit failed: {_sanitize_error(str(exc))}", file=sys.stderr)
            return 1

    if args.command == "status":
        from mindsync.remote_queue import RemoteQueue

        queue = RemoteQueue()
        if args.job_id:
            try:
                info = queue.get_status(args.job_id)
            except Exception as exc:
                from mindsync.bridge import _sanitize_error

                print(f"Status failed: {_sanitize_error(str(exc))}", file=sys.stderr)
                return 1
            if not info:
                print(f"No such remote job: {args.job_id}", file=sys.stderr)
                return 1
            state = info["state"]
            data = info["job"]
            print(f"[{data.get('job_id')}] status: {state}")
            print(f"  created_at: {data.get('created_at')}")
            print(f"  repo_path: {data.get('repo_path')}")
            print(f"  timeout_seconds: {data.get('timeout_seconds', 900)}")
            print(
                "  execution_mode: "
                f"{data.get('execution_mode', 'worker')} "
                f"(delegation_depth: {data.get('delegation_depth', 0)})"
            )
            if data.get("claimed_at"):
                print(f"  claimed_at: {data.get('claimed_at')} by {data.get('worker_id')}")
            if data.get("ended_at"):
                print(f"  ended_at: {data.get('ended_at')} (exit code: {data.get('exit_code')})")
            if data.get("branch"):
                print(f"  branch: {data.get('branch')}")
            if data.get("commit_sha"):
                print(f"  commit_sha: {data.get('commit_sha')}")
            if data.get("result"):
                print(f"\nResult:\n{data.get('result')}")
            return 0
        else:
            jobs = queue.list_all_jobs()
            if not jobs:
                print("No remote jobs found.")
                return 0
            for item in jobs:
                print(
                    f"[{item['job_id']}] {item['state']} - repo: {item.get('repo_path')} "
                    f"agent: {item.get('agent') or 'auto'} "
                    f"mode: {item.get('execution_mode', 'worker')}"
                )
            return 0

    if args.command == "memory":
        return _run_memory_command(args)

    build_parser().print_help()
    return 2
