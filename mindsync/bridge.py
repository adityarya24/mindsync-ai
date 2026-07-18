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
import threading
from dataclasses import dataclass
from typing import Any

from mindsync.config import settings

# Safe identifiers for remote CLI args (no shell metacharacters).
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")

_remote_cache: dict[str, Any] = {"status": "unknown", "reason": None, "checked_at": 0.0, "duration": 0.0}
_probe_lock = threading.Lock()

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

def get_remote_status(*, force: bool = False) -> dict[str, Any]:
    if not settings.remote_enabled:
        return {"status": "unknown", "reason": "remote_not_configured", "checked_at": 0.0, "duration": 0.0, "cache_age": 0.0}

    now = time.time()
    ttl = settings.remote_cache_ttl_seconds
    
    with _probe_lock:
        cache_age = now - float(_remote_cache["checked_at"])
        if not force and _remote_cache["checked_at"] > 0 and cache_age < ttl:
            res = dict(_remote_cache)
            res["cache_age"] = cache_age
            return res
            
        start = time.time()
        status = "offline"
        reason = "unknown"
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
            if res.returncode == 0:
                status = "online"
                reason = "ok"
            elif res.returncode == 255:
                status = "offline"
                reason = "ssh_auth_or_timeout"
            else:
                status = "offline"
                reason = f"exit_code_{res.returncode}"
        except subprocess.TimeoutExpired:
            status = "offline"
            reason = "timeout"
        except OSError:
            status = "offline"
            reason = "os_error"
            
        duration = time.time() - start
        _remote_cache.update({
            "status": status,
            "reason": reason,
            "checked_at": time.time(),
            "duration": round(duration, 3)
        })
        
        res = dict(_remote_cache)
        res["cache_age"] = 0.0
        return res

def check_remote_online(*, force: bool = False) -> bool:
    """Cached SSH reachability probe. False when remote is disabled or unreachable."""
    return get_remote_status(force=force)["status"] == "online"


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


def write_batch_remote(facts: list[dict[str, Any]]) -> WriteResult:
    """Write a batch of durable facts on the remote host using base64 transport."""
    if not settings.remote_enabled:
        return WriteResult(ok=False, error=remote_not_configured_error())

    if not facts:
        return WriteResult(ok=True, stdout="No facts to write.")

    # Validate all facts before sending
    valid_facts = []
    for fact in facts:
        try:
            agent = validate_id("agent", fact.get("agent", ""))
            entity = validate_id("entity", fact.get("entity", ""))
            attribute = validate_id("attribute", fact.get("attribute", ""))
            source = validate_id("source", fact.get("source", ""))
            conf = float(fact.get("confidence", 1.0))
            if not 0.0 <= conf <= 1.0:
                continue # Skip invalid
            fact_id = validate_id("fact_id", fact.get("fact_id", ""))
            valid_facts.append({
                "fact_id": fact_id,
                "timestamp": fact.get("timestamp", ""),
                "agent": agent,
                "entity": entity,
                "attribute": attribute,
                "text": fact.get("text", ""),
                "source": source,
                "confidence": conf
            })
        except ValueError:
            continue

    if not valid_facts:
        return WriteResult(ok=False, error="No valid facts to write in batch.")

    payload = json.dumps(valid_facts)
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    remote_root = settings.remote_root
    write_script = settings.remote_write_script
    script = f"""set -euo pipefail
cd {shlex.quote(remote_root)}
{_maybe_source_env()}
PAYLOAD=$(printf '%s' {shlex.quote(b64)} | base64 -d)
python3 {shlex.quote(write_script)} batch --payload "$PAYLOAD"
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


def write_fact_remote(
    *,
    fact_id: str,
    agent: str,
    entity: str,
    attribute: str,
    text: str,
    source: str,
    confidence: float,
) -> WriteResult:
    """Write one durable fact on the remote host."""
    fact = {
        "fact_id": fact_id,
        "agent": agent,
        "entity": entity,
        "attribute": attribute,
        "text": text,
        "source": source,
        "confidence": confidence
    }
    return write_batch_remote([fact])


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
