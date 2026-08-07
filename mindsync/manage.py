"""Human-facing `mindsync setup|doctor|config` command implementation."""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

from mindsync.onboarding import CLI_SPECS, doctor, setup
from mindsync.orchestration import load_policy, policy_path, update_policy


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
    for issue in report["issues"]:
        print(f"  issue    {issue}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mindsync")
    sub = parser.add_subparsers(dest="command")

    setup_parser = sub.add_parser("setup", help="Detect CLIs and register the MindSync MCP server")
    setup_parser.add_argument("--mode", choices=["auto", "suggest", "off"])
    setup_parser.add_argument("--cli", action="append", choices=sorted(CLI_SPECS))
    setup_parser.add_argument("--dry-run", action="store_true")
    setup_parser.add_argument("--force", action="store_true")

    doctor_parser = sub.add_parser("doctor", help="Diagnose policy, CLI registration, and workers")
    doctor_parser.add_argument("--json", action="store_true")

    config_parser = sub.add_parser("config", help="Read or change orchestration policy")
    config_parser.add_argument("key", nargs="?")
    config_parser.add_argument("value", nargs="?")

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
            )
        except (OSError, TimeoutError, ValueError) as exc:
            print(f"Setup failed: {exc}", file=sys.stderr)
            return 1
        _print_setup(result)
        return 0 if result["ok"] else 1

    if args.command == "doctor":
        try:
            report = doctor()
        except (OSError, TimeoutError, ValueError) as exc:
            print(f"Doctor failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_doctor(report)
        return 0 if report["ok"] else 1

    if args.command == "config":
        if args.key is None:
            print(json.dumps(load_policy().model_dump(), indent=2))
            print(f"Policy: {policy_path()}")
            return 0
        if args.value is None:
            policy = load_policy().model_dump()
            leaf = args.key.rsplit(".", 1)[-1]
            if leaf not in policy:
                print(f"Unknown orchestration setting '{args.key}'.", file=sys.stderr)
                return 2
            print(json.dumps(policy[leaf]))
            return 0
        try:
            policy = update_policy(args.key, _parse_value(args.key, args.value))
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

    build_parser().print_help()
    return 2
