"""SSH/SCP bridge to the remote gbrain durable store."""

from __future__ import annotations

import base64
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from mindsync.config import settings

# Safe identifiers for remote CLI args (no shell metacharacters).
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")

_vps_cache: dict[str, Any] = {"online": None, "checked_at": 0.0}


class BridgeError(Exception):
    """Remote bridge failure."""


def validate_id(label: str, value: str) -> str:
    if not value or not _SAFE_ID.match(value):
        raise ValueError(
            f"Invalid {label} {value!r}: use letters, digits, and _ . / : - only "
            f"(max 128 chars)."
        )
    return value


def _run(
    args: list[str],
    *,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def check_vps_online(*, force: bool = False) -> bool:
    """Cached SSH reachability probe."""
    now = time.time()
    ttl = settings.vps_cache_ttl_seconds
    if (
        not force
        and _vps_cache["online"] is not None
        and (now - float(_vps_cache["checked_at"])) < ttl
    ):
        return bool(_vps_cache["online"])

    online = False
    try:
        res = _run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={settings.ssh_connect_timeout}",
                settings.ssh_host,
                "echo",
                "1",
            ],
            timeout=settings.ssh_connect_timeout + 2,
            check=False,
        )
        online = res.returncode == 0 and res.stdout.strip() == "1"
    except (OSError, subprocess.TimeoutExpired):
        online = False

    _vps_cache["online"] = online
    _vps_cache["checked_at"] = now
    return online


def _ssh_script(script: str, *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run a bash script on the remote host via stdin (avoids local shell quoting)."""
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={settings.ssh_connect_timeout}",
            settings.ssh_host,
            "bash",
            "-s",
        ],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass
class WriteResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


def write_fact_remote(
    *,
    agent: str,
    entity: str,
    attribute: str,
    text: str,
    source: str,
    confidence: float,
) -> WriteResult:
    """Write one durable fact on the VPS using base64 transport for free text."""
    try:
        agent = validate_id("agent", agent)
        entity = validate_id("entity", entity)
        attribute = validate_id("attribute", attribute)
        source = validate_id("source", source)
        conf = float(confidence)
        if not 0.0 <= conf <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
    except ValueError as exc:
        return WriteResult(ok=False, error=str(exc))

    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    remote_root = settings.remote_root
    # Remote script: decode text safely, then call gbrain_fact.py with quoted args.
    script = f"""set -euo pipefail
cd {shlex.quote(remote_root)}
set -a
# shellcheck disable=SC1091
source config/gbrain.env
set +a
TEXT=$(printf '%s' {shlex.quote(b64)} | base64 -d)
python3 tools/gbrain_fact.py write \\
  --agent {shlex.quote(agent)} \\
  --entity {shlex.quote(entity)} \\
  --attribute {shlex.quote(attribute)} \\
  --text "$TEXT" \\
  --source {shlex.quote(source)} \\
  --confidence {conf}
"""
    try:
        res = _ssh_script(script, timeout=90)
    except subprocess.TimeoutExpired:
        return WriteResult(ok=False, error="SSH write timed out")
    except OSError as exc:
        return WriteResult(ok=False, error=f"SSH write failed: {exc}")

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "unknown remote error").strip()
        return WriteResult(ok=False, stdout=res.stdout, stderr=res.stderr, error=err)
    return WriteResult(ok=True, stdout=res.stdout.strip(), stderr=res.stderr)


def consolidate_remote() -> WriteResult:
    script = f"""set -euo pipefail
cd {shlex.quote(settings.remote_root)}
set -a
# shellcheck disable=SC1091
source config/gbrain.env
set +a
python3 tools/gbrain_consolidate.py
"""
    try:
        res = _ssh_script(script, timeout=120)
    except subprocess.TimeoutExpired:
        return WriteResult(ok=False, error="SSH consolidate timed out")
    except OSError as exc:
        return WriteResult(ok=False, error=f"SSH consolidate failed: {exc}")

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "unknown remote error").strip()
        return WriteResult(ok=False, stdout=res.stdout, stderr=res.stderr, error=err)
    return WriteResult(ok=True, stdout=res.stdout.strip(), stderr=res.stderr)


def pull_compiled_truth() -> WriteResult:
    """Pull remote compiled-truth directory without shell globs (Windows-safe)."""
    settings.ensure_dirs()
    remote = f"{settings.ssh_host}:{settings.remote_root}/compiled-truth/."
    dest = str(settings.compiled_truth_dir)
    try:
        res = _run(
            [
                "scp",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={settings.ssh_connect_timeout}",
                "-r",
                remote,
                dest,
            ],
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return WriteResult(ok=False, error="SCP pull timed out")
    except OSError as exc:
        return WriteResult(ok=False, error=f"SCP pull failed: {exc}")

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "scp failed").strip()
        return WriteResult(ok=False, stdout=res.stdout, stderr=res.stderr, error=err)
    return WriteResult(ok=True, stdout=res.stdout.strip(), stderr=res.stderr)
