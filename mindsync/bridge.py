"""SSH/SCP bridge to an optional remote durable store.

Remote layout is configurable. Defaults match the sample scripts under
``examples/remote/``. No host, username, or server path is hard-coded.
"""

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

_remote_cache: dict[str, Any] = {"online": None, "checked_at": 0.0}


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


def remote_not_configured_error() -> str:
    return (
        "Remote sync is not configured. Set MINDSYNC_SSH_HOST and "
        "MINDSYNC_REMOTE_ROOT (see .env.example)."
    )


def check_remote_online(*, force: bool = False) -> bool:
    """Cached SSH reachability probe. False when remote is disabled or unreachable."""
    if not settings.remote_enabled:
        return False

    now = time.time()
    ttl = settings.remote_cache_ttl_seconds
    if (
        not force
        and _remote_cache["online"] is not None
        and (now - float(_remote_cache["checked_at"])) < ttl
    ):
        return bool(_remote_cache["online"])

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

    _remote_cache["online"] = online
    _remote_cache["checked_at"] = now
    return online


# Backward-compatible alias used during the rename.
check_vps_online = check_remote_online


def _ssh_script(script: str, *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run a bash script on the remote host via stdin (avoids local shell quoting).

    Always normalizes to LF bytes: Windows text mode would otherwise send CRLF
    and break remote bash (`set -o pipefail\\r`).
    """
    normalized = script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    res = subprocess.run(
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
        input=normalized,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return subprocess.CompletedProcess(
        args=res.args,
        returncode=res.returncode,
        stdout=(res.stdout or b"").decode("utf-8", errors="replace"),
        stderr=(res.stderr or b"").decode("utf-8", errors="replace"),
    )


def _maybe_source_env() -> str:
    """Optional remote env file source lines."""
    env_file = settings.remote_env_file
    if not env_file:
        return ""
    return f"""
if [ -f {shlex.quote(env_file)} ]; then
  set -a
  # shellcheck disable=SC1090
  source {shlex.quote(env_file)}
  set +a
fi
"""


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
    """Write one durable fact on the remote host using base64 transport for free text."""
    if not settings.remote_enabled:
        return WriteResult(ok=False, error=remote_not_configured_error())

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
    write_script = settings.remote_write_script
    script = f"""set -euo pipefail
cd {shlex.quote(remote_root)}
{_maybe_source_env()}
TEXT=$(printf '%s' {shlex.quote(b64)} | base64 -d)
python3 {shlex.quote(write_script)} write \\
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
    if not settings.remote_enabled:
        return WriteResult(ok=False, error=remote_not_configured_error())

    script = f"""set -euo pipefail
cd {shlex.quote(settings.remote_root)}
{_maybe_source_env()}
python3 {shlex.quote(settings.remote_consolidate_script)}
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
    if not settings.remote_enabled:
        return WriteResult(ok=False, error=remote_not_configured_error())

    settings.ensure_dirs()
    truth = settings.remote_truth_subdir.strip("/")
    remote = f"{settings.ssh_host}:{settings.remote_root}/{truth}/."
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
