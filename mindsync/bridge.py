"""SSH/SCP bridge to an optional remote durable store.

Remote layout is configurable. Defaults match the sample scripts under
``examples/remote/``. No host, username, or server path is hard-coded.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mindsync.config import settings


def resolve_openssh_tool(tool: str = "ssh") -> str:
    """Resolve the ssh/scp binary to invoke.

    An explicit ``MINDSYNC_SSH_BIN`` always wins; scp is taken from the same
    directory. Otherwise, on Windows, prefer the OS OpenSSH client over a bare
    PATH lookup: Git for Windows ships an MSYS ssh that cannot talk to the
    Windows ssh-agent, so if it shadows the system client every agent-held key
    fails to authenticate and the remote looks permanently offline.
    """
    configured = (settings.ssh_bin or "").strip()
    if configured:
        if tool == "ssh":
            return configured
        sibling = Path(configured).with_name(tool + Path(configured).suffix)
        return str(sibling) if sibling.is_file() else tool

    if sys.platform == "win32":
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        candidate = Path(system_root) / "System32" / "OpenSSH" / f"{tool}.exe"
        if candidate.is_file():
            return str(candidate)

    return tool

# Safe identifier patterns (no shell metacharacters, path traversal, or colon)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
# Entity keys are namespaced (`person:alice`, `project:web-api`), so a
# single leading `namespace:` prefix is allowed. The prefix itself may not
# contain a dot, which keeps the Windows alternate-data-stream shape
# (`file.txt:stream`) rejected exactly as a bare `_SAFE_ID` would.
_SAFE_ENTITY = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,62}:)?[A-Za-z0-9][A-Za-z0-9_.-]*$"
)
_SAFE_HEX = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_FORBIDDEN_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

def validate_agent(val: str) -> str:
    if not val or not _SAFE_ID.match(val):
        raise ValueError(f"Invalid agent name {val!r}: must be alphanumeric + _ . - (max 128)")
    return val

def _check_windows_safe(val: str) -> None:
    upper = val.upper()
    base = upper.split(".")[0]
    if base in _FORBIDDEN_WINDOWS:
        raise ValueError(f"Reserved Windows device name: {val}")
    for char in ('<', '>', ':', '"', '/', '\\', '|', '?', '*'):
        if char in val:
            raise ValueError(f"Forbidden character {char!r} in {val}")

def validate_entity(val: str) -> str:
    if not val or len(val) > 128 or not _SAFE_ENTITY.match(val):
        raise ValueError(
            f"Invalid entity name {val!r}: must be alphanumeric + _ . - "
            "with an optional 'namespace:' prefix (max 128)"
        )
    for segment in val.split(":"):
        _check_windows_safe(segment)
    return val

def validate_attribute(val: str) -> str:
    if not val or not _SAFE_ID.match(val):
        raise ValueError(f"Invalid attribute name {val!r}: must be alphanumeric + _ . - (max 128)")
    _check_windows_safe(val)
    return val

def validate_source(val: str) -> str:
    if not val or not _SAFE_SOURCE.match(val):
        raise ValueError(f"Invalid source {val!r}: must be alphanumeric + _ . : - (max 128)")
    for char in ('<', '>', '"', '/', '\\', '|', '?', '*'):
        if char in val:
            raise ValueError(f"Forbidden character {char!r} in source {val}")
    return val

def validate_fact_id(val: str) -> str:
    if not val or not _SAFE_HEX.match(val):
        raise ValueError(f"Invalid fact_id {val!r}")
    return val

def validate_fact_text(val: str) -> str:
    if not val:
        raise ValueError("Fact text cannot be empty")
    # Enforce limit on UTF-8 byte size
    if len(val.encode("utf-8")) > 50 * 1024:
        raise ValueError("Fact text exceeds 50KB limit")
    return val


# SSH key filenames and the .ssh dir are redacted explicitly (not just as
# part of a full-path match) because they can show up as bare fragments in
# OSError/subprocess messages without a recognizable leading path prefix,
# e.g. "Permission denied (publickey) ... .ssh\id_ed25519".
_KEY_FILE_RE = re.compile(r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?", re.IGNORECASE)
_SSH_DIR_RE = re.compile(r"\.ssh\b")
# Full absolute path matches (greedy to end-of-token, not just the first two
# segments) so nothing after e.g. "/home/user" survives, on POSIX or Windows.
_HOME_TILDE_RE = re.compile(r"~[/\\][^\s'\"]*")
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]+")
_POSIX_PATH_RE = re.compile(r"/[^\s'\"]+")


def _sanitize_error(err: str) -> str:
    if not err:
        return "unknown error"
    if settings.ssh_host:
        err = err.replace(settings.ssh_host, "[SSH_HOST]")
    if settings.remote_root:
        err = err.replace(settings.remote_root, "[REMOTE_ROOT]")
    # Scrub SSH key/identity references first, since they can appear as
    # bare fragments that a path-shaped regex wouldn't otherwise catch.
    err = _KEY_FILE_RE.sub("[KEY_FILE]", err)
    err = _SSH_DIR_RE.sub("[SSH_DIR]", err)
    # Scrub full absolute paths and home-relative (~) paths.
    err = _HOME_TILDE_RE.sub("[PATH]", err)
    err = _WINDOWS_PATH_RE.sub("[PATH]", err)
    err = _POSIX_PATH_RE.sub("[PATH]", err)
    return err


_remote_cache: dict[str, Any] = {"status": "unknown", "reason": None, "checked_at": 0.0, "duration": 0.0}
_probe_lock = threading.Lock()

class BridgeError(Exception):
    """Remote bridge failure."""


def _run(
    args: list[str],
    *,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
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
                    resolve_openssh_tool("ssh"),
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
            # Without ssh's own words, every failure looks identical and the
            # cause (wrong key, unknown host alias, agent not reachable) is
            # unrecoverable from the outside. Sanitized: no paths or hostnames.
            if status == "offline":
                detail = _sanitize_error((res.stderr or "").strip())
                if detail and detail != "unknown error":
                    reason = f"{reason}: {detail.splitlines()[-1][:200]}"
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
    and break remote bash (`set -o pipefail\r`).
    """
    normalized = script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    res = subprocess.run(
        [
            resolve_openssh_tool("ssh"),
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
    results: dict[str, Any] | None = None


def _looks_like_unsupported_batch_subcommand(result: "WriteResult") -> bool:
    """Detect an old, write-only remote writer that predates the `batch`
    subcommand (argparse rejects it with "invalid choice: 'batch'", exit 2).

    This doubles as the capability probe: rather than spend an extra SSH
    round trip proactively asking the remote what it supports, the first
    real batch attempt IS the probe -- if it fails with this specific
    signature we know the remote is old and fall back, otherwise the normal
    (and far more common) path costs nothing extra.
    """
    text = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return "invalid choice" in text and "batch" in text


def _write_batch_native(valid_facts: list[dict[str, Any]]) -> WriteResult:
    """Send the whole batch in one call via the `batch` subcommand (new remote)."""
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
        return WriteResult(ok=False, error=_sanitize_error(f"SSH write failed: {exc}"))

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "unknown remote error").strip()
        return WriteResult(ok=False, stdout=res.stdout, stderr=res.stderr, error=_sanitize_error(err))

    parsed_results = None
    try:
        parsed_results = json.loads(res.stdout)
    except json.JSONDecodeError:
        pass

    return WriteResult(ok=True, stdout=res.stdout.strip(), stderr=res.stderr, results=parsed_results)


def _looks_like_unsupported_fact_id(result: "WriteResult") -> bool:
    """Detect a remote whose `write` predates the `--fact_id` flag.

    argparse reports it as "unrecognized arguments: --fact_id ..." (exit 2),
    which is distinct from a rejected value and safe to retry without.
    """
    text = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return "unrecognized arguments" in text and "--fact_id" in text


# None = not probed yet. Set once per process by the first legacy write, so a
# remote that has no --fact_id costs one extra round trip in total rather than
# one per fact. A benign race here only causes a repeat probe.
_legacy_supports_fact_id: bool | None = None


def _legacy_write_call(fact: dict[str, Any], *, with_fact_id: bool) -> WriteResult:
    write_script = settings.remote_write_script
    parts = []
    if with_fact_id:
        parts.append(f"--fact_id {shlex.quote(fact['fact_id'])}")
    parts += [
        f"--agent {shlex.quote(fact['agent'])}",
        f"--entity {shlex.quote(fact['entity'])}",
        f"--attribute {shlex.quote(fact['attribute'])}",
        f"--text {shlex.quote(fact['text'])}",
        f"--source {shlex.quote(fact['source'])}",
        f"--confidence {shlex.quote(str(fact['confidence']))}",
    ]
    script = f"""set -euo pipefail
cd {shlex.quote(settings.remote_root)}
{_maybe_source_env()}
python3 {shlex.quote(write_script)} write {' '.join(parts)}
"""
    try:
        res = _ssh_script(script, timeout=30)
    except subprocess.TimeoutExpired:
        return WriteResult(ok=False, error="SSH write timed out")
    except OSError as exc:
        return WriteResult(ok=False, error=_sanitize_error(f"SSH write failed: {exc}"))

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "unknown remote error").strip()
        return WriteResult(
            ok=False,
            stdout=res.stdout,
            stderr=res.stderr,
            error=_sanitize_error(err),
        )
    return WriteResult(ok=True, stdout=res.stdout.strip(), stderr=res.stderr)


def _write_single_via_legacy_protocol(fact: dict[str, Any]) -> WriteResult:
    """Write one fact using the older single-write-only remote protocol.

    Some deployed writers are older still and have no ``--fact_id`` at all.
    Passing it makes argparse reject the whole call, which would strand every
    queued fact, so the flag is dropped and the write retried once the remote
    tells us it is unknown. The client tracks success per call either way.
    """
    global _legacy_supports_fact_id

    if _legacy_supports_fact_id is False:
        return _legacy_write_call(fact, with_fact_id=False)

    result = _legacy_write_call(fact, with_fact_id=True)
    if result.ok:
        _legacy_supports_fact_id = True
        return result
    if _looks_like_unsupported_fact_id(result):
        _legacy_supports_fact_id = False
        return _legacy_write_call(fact, with_fact_id=False)
    return result


def _write_batch_via_legacy_single_writes(valid_facts: list[dict[str, Any]]) -> WriteResult:
    """Fallback for an already-deployed write-only remote: send facts one at
    a time via the legacy `write` subcommand (one SSH round trip per fact).

    Normalizes the result into the same {success_ids, failed} shape the
    native `batch` path returns, so callers (sync_offline_facts) don't need
    to know or care which protocol was actually used.
    """
    success_ids: list[str] = []
    failed: list[dict[str, Any]] = []
    for fact in valid_facts:
        single = _write_single_via_legacy_protocol(fact)
        if single.ok:
            success_ids.append(fact["fact_id"])
        else:
            failed.append({"fact_id": fact["fact_id"], "error": single.error or "legacy write failed"})
    return WriteResult(
        ok=True,
        stdout=f"Legacy single-write fallback: {len(success_ids)} ok, {len(failed)} failed.",
        results={"success_ids": success_ids, "failed": failed},
    )


def write_batch_remote(facts: list[dict[str, Any]]) -> WriteResult:
    """Write a batch of durable facts on the remote host using base64 transport.

    Backward compatible with an already-deployed remote writer that only
    understands single writes (pre-`batch` protocol): if the native batch
    call fails because the remote doesn't recognize the `batch` subcommand,
    transparently falls back to one legacy `write` call per fact so a new
    client keeps working against an old, un-upgraded remote.
    """
    if not settings.remote_enabled:
        return WriteResult(ok=False, error=remote_not_configured_error())

    if not facts:
        return WriteResult(ok=True, stdout="No facts to write.")

    # Validate all facts before sending
    valid_facts = []
    for fact in facts:
        try:
            agent = validate_agent(fact.get("agent", ""))
            entity = validate_entity(fact.get("entity", ""))
            attribute = validate_attribute(fact.get("attribute", ""))
            source = validate_source(fact.get("source", ""))
            fid = validate_fact_id(fact.get("fact_id", ""))
            text = validate_fact_text(fact.get("text", ""))
            conf = float(fact.get("confidence", 1.0))
            if not 0.0 <= conf <= 1.0:
                continue # Skip invalid
            valid_facts.append({
                "fact_id": fid,
                "timestamp": fact.get("timestamp", ""),
                "agent": agent,
                "entity": entity,
                "attribute": attribute,
                "text": text,
                "source": source,
                "confidence": conf
            })
        except ValueError:
            continue

    if not valid_facts:
        return WriteResult(ok=False, error="No valid facts to write in batch.")

    result = _write_batch_native(valid_facts)
    if not result.ok and _looks_like_unsupported_batch_subcommand(result):
        return _write_batch_via_legacy_single_writes(valid_facts)
    return result


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
    validate_agent(agent)
    validate_entity(entity)
    validate_attribute(attribute)
    validate_source(source)
    validate_fact_id(fact_id)
    validate_fact_text(text)
    conf = float(confidence)
    if not 0.0 <= conf <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")

    fact = {
        "fact_id": fact_id,
        "agent": agent,
        "entity": entity,
        "attribute": attribute,
        "text": text,
        "source": source,
        "confidence": conf
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
        return WriteResult(ok=False, error=_sanitize_error(f"SSH consolidate failed: {exc}"))

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "unknown remote error").strip()
        return WriteResult(ok=False, stdout=res.stdout, stderr=res.stderr, error=_sanitize_error(err))
    return WriteResult(ok=True, stdout=res.stdout.strip(), stderr=res.stderr)


def pull_compiled_truth() -> WriteResult:
    """Pull remote compiled-truth directory without shell globs (Windows-safe)."""
    if not settings.remote_enabled:
        return WriteResult(ok=False, error=remote_not_configured_error())

    settings.ensure_dirs()
    staging_dir = Path(tempfile.mkdtemp(dir=str(settings.home), prefix="staging-truth-"))

    try:
        truth = settings.remote_truth_subdir.strip("/")
        remote = f"{settings.ssh_host}:{settings.remote_root}/{truth}/."
        try:
            res = _run(
                [
                    resolve_openssh_tool("scp"),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    f"ConnectTimeout={settings.ssh_connect_timeout}",
                    "-r",
                    remote,
                    str(staging_dir),
                ],
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return WriteResult(ok=False, error="SCP pull timed out")
        except OSError as exc:
            return WriteResult(ok=False, error=_sanitize_error(f"SCP pull failed: {exc}"))

        if res.returncode != 0:
            err = (res.stderr or res.stdout or "scp failed").strip()
            return WriteResult(
                ok=False,
                stdout=res.stdout,
                stderr=res.stderr,
                error=_sanitize_error(err),
            )

        from mindsync.storage import publish_compiled_truth

        try:
            publish_compiled_truth(staging_dir)
        except Exception as exc:
            return WriteResult(
                ok=False,
                error=_sanitize_error(f"Failed to publish truth: {exc}"),
            )
        return WriteResult(ok=True)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
