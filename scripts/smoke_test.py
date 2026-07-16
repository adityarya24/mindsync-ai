#!/usr/bin/env python3
"""End-to-end smoke test for MindSync tools (no live agent client required).

Usage:
  # local-only (temp home)
  python scripts/smoke_test.py

  # against configured personal home/remote
  set MINDSYNC_HOME=...
  set MINDSYNC_SSH_HOST=...
  python scripts/smoke_test.py --with-remote-probe
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def _banner(msg: str) -> None:
    print(f"\n=== {msg} ===")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--with-remote-probe",
        action="store_true",
        help="Probe SSH remote if configured (may take a few seconds).",
    )
    parser.add_argument(
        "--use-env-home",
        action="store_true",
        help="Use existing MINDSYNC_HOME instead of an isolated temp dir.",
    )
    args = parser.parse_args()

    if not args.use_env_home:
        tmp = tempfile.mkdtemp(prefix="mindsync-smoke-")
        os.environ["MINDSYNC_HOME"] = tmp
        # Force local-only unless caller already set remote and asked to probe.
        if not args.with_remote_probe:
            os.environ.pop("MINDSYNC_SSH_HOST", None)
            os.environ.pop("MINDSYNC_REMOTE_ROOT", None)

    # Reload settings after env mutation.
    import mindsync.config as config_mod
    import mindsync.storage as storage_mod
    import mindsync.bridge as bridge_mod
    from mindsync import server as srv

    config_mod.settings = config_mod.Settings()
    storage_mod.settings = config_mod.settings
    bridge_mod.settings = config_mod.settings
    srv.settings = config_mod.settings
    # Reset remote cache
    bridge_mod._remote_cache["online"] = None
    bridge_mod._remote_cache["checked_at"] = 0.0

    settings = config_mod.settings
    settings.ensure_dirs()
    failures: list[str] = []

    _banner("health")
    h = srv.health("smoke")
    print(h)
    if not h.get("ok"):
        failures.append("health not ok")

    _banner("get_sync_context")
    ctx = srv.get_sync_context("agent-smoke", project_name="mindsync-mcp")
    print(
        {
            "home": ctx.get("home"),
            "remote_configured": ctx.get("remote_configured"),
            "remote_online": ctx.get("remote_online"),
            "truth_keys": ctx.get("compiled_truth_keys"),
        }
    )

    _banner("update_focus (no conflict)")
    r1 = srv.update_focus(
        "agent-a",
        "mindsync-mcp",
        "master",
        "rewrite README docs only",
        paths=["README.md"],
    )
    print(r1)
    if r1.get("warnings"):
        failures.append(f"unexpected warnings for agent-a: {r1['warnings']}")

    _banner("update_focus (expect path conflict)")
    r2 = srv.update_focus(
        "agent-b",
        "mindsync-mcp",
        "feature/x",
        "touch README later",
        paths=["README.md"],
    )
    print(r2)
    if not r2.get("warnings"):
        failures.append("expected path conflict warning for agent-b")

    _banner("update_focus (token overlap)")
    r3 = srv.update_focus(
        "agent-c",
        "mindsync-mcp",
        "feature/y",
        "edit mindsync/storage.py locking",
        paths=[],
    )
    # seed another agent with overlapping focus first
    srv.update_focus(
        "agent-d",
        "mindsync-mcp",
        "feature/z",
        "fix mindsync/storage.py queue",
        paths=[],
    )
    r4 = srv.update_focus(
        "agent-c",
        "mindsync-mcp",
        "feature/y",
        "edit mindsync/storage.py locking",
        paths=[],
    )
    print(r4)
    if not r4.get("warnings"):
        failures.append("expected token-overlap warning for agent-c")

    _banner("queue_durable_fact (likely local queue)")
    q = srv.queue_durable_fact(
        "agent-smoke",
        "system-mindsync",
        "smoke_test",
        "MindSync smoke test fact — safe to ignore",
        confidence=0.5,
    )
    print(q)
    if not q.get("ok"):
        failures.append(f"queue_durable_fact failed: {q}")

    if args.with_remote_probe and settings.remote_enabled:
        _banner("remote probe")
        online = bridge_mod.check_remote_online(force=True)
        print({"remote_online": online, "host": settings.ssh_host})
        h2 = srv.health("smoke")
        print({"health_remote_online": h2.get("remote_online")})
    else:
        _banner("remote probe skipped")
        print(
            {
                "remote_configured": settings.remote_enabled,
                "hint": "re-run with --with-remote-probe and remote env set",
            }
        )

    _banner("state file")
    state_path = Path(settings.state_file)
    print({"state_file": str(state_path), "exists": state_path.exists()})
    if state_path.exists():
        print(state_path.read_text(encoding="utf-8")[:800])

    _banner("result")
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: local smoke checks succeeded")
    print(f"MINDSYNC_HOME={settings.home}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
