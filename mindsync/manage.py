"""Human-facing `mindsync setup|doctor|config` command implementation."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from mindsync.onboarding import CLI_SPECS, doctor, setup
from mindsync.orchestration import load_policy, policy_path, update_policy


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

    build_parser().print_help()
    return 2
