"""Remote queue and claim implementation for MindSync remote dispatch.

Provides SSH-transported queue management (via bridge.py) and local directory-based
queue management for testing and local operations.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mindsync.bridge import _sanitize_error, _ssh_script
from mindsync.config import settings
from mindsync.storage import atomic_private_write, file_lock

_JOB_ID_RE = re.compile(r"^[0-9a-z]+-[0-9a-f]+$", re.I)


def validate_job_id(job_id: str) -> str:
    if not job_id or not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
        raise ValueError(f"Invalid job_id: {job_id!r}")
    return job_id


def generate_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{secrets.token_hex(4)}"


def validate_repo_path(repo_path: str, allowed_roots: List[str]) -> bool:
    if not repo_path or not allowed_roots:
        return False
    try:
        candidate = Path(repo_path).resolve()
        for root in allowed_roots:
            if not root or not str(root).strip():
                continue
            allowed = Path(str(root).strip()).resolve()
            if candidate == allowed or allowed in candidate.parents:
                return True
        return False
    except Exception:
        return False


def _redact(text: str) -> str:
    return _sanitize_error(text) if text else ""


def _excerpt(text: str, limit: int = 4000) -> str:
    redacted = _redact(text)
    return redacted if len(redacted) <= limit else "…(truncated)…\n" + redacted[-limit:]


def _write_local_json(path: Path, data: Dict[str, Any]) -> None:
    atomic_private_write(path, json.dumps(data, indent=2))


class RemoteQueue:
    def __init__(
        self,
        remote_root: Optional[str] = None,
        ssh_host: Optional[str] = None,
    ) -> None:
        if remote_root is not None:
            self.remote_root = remote_root
            self.ssh_host = ssh_host if ssh_host is not None else ""
        else:
            self.remote_root = settings.remote_root
            self.ssh_host = ssh_host if ssh_host is not None else settings.ssh_host

        if self.ssh_host and self.ssh_host != settings.ssh_host:
            raise ValueError("RemoteQueue must use the SSH host configured for the bridge.")

    @property
    def is_ssh(self) -> bool:
        return bool(self.ssh_host and self.remote_root)

    def _remote_path(self, *parts: str) -> str:
        root = (self.remote_root or "").strip().rstrip("/")
        if not root or root in {"/", "~"} or "\n" in root or "\x00" in root:
            raise RuntimeError("A safe MINDSYNC_REMOTE_ROOT is required.")
        return "/".join((root, *parts))

    def _ensure_dirs_local(self) -> None:
        if not self.remote_root:
            raise RuntimeError("Remote root is not configured.")
        root = Path(self.remote_root)
        for sub in ("queue/pending", "queue/claimed", "queue/done"):
            d = root / sub
            d.mkdir(parents=True, exist_ok=True)
            try:
                d.chmod(0o700)
            except OSError:
                pass

    def ensure_remote_dirs(self) -> None:
        if not self.is_ssh:
            if self.remote_root:
                self._ensure_dirs_local()
            return

        queue_dir = self._remote_path("queue")
        pending_dir = self._remote_path("queue", "pending")
        claimed_dir = self._remote_path("queue", "claimed")
        done_dir = self._remote_path("queue", "done")
        script = f"""set -euo pipefail
mkdir -p {shlex.quote(pending_dir)} {shlex.quote(claimed_dir)} {shlex.quote(done_dir)}
chmod 700 {shlex.quote(queue_dir)} {shlex.quote(pending_dir)} {shlex.quote(claimed_dir)} {shlex.quote(done_dir)}
"""
        res = _ssh_script(script, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to ensure remote queue dirs: {_sanitize_error(res.stderr)}")

    def submit_job(
        self,
        *,
        repo_path: str,
        prompt: str,
        task_file: Optional[str] = None,
        agent: Optional[str] = None,
        branch: Optional[str] = None,
        role: Optional[str] = None,
        submitter: Optional[str] = None,
    ) -> str:
        if not repo_path or not str(repo_path).strip():
            raise ValueError("repo_path cannot be empty")
        if not prompt or not prompt.strip():
            raise ValueError("prompt cannot be empty")
        job_id = generate_job_id()
        created_at = datetime.now(timezone.utc).isoformat()
        job_data = {
            "job_id": job_id,
            "created_at": created_at,
            "repo_path": repo_path,
            "prompt": prompt,
            "task_file": task_file,
            "agent": agent,
            "branch": branch,
            "role": role,
            "submitter": submitter or os.environ.get("USER", os.environ.get("USERNAME", "remote")),
        }
        content = json.dumps(job_data, indent=2)

        if not self.is_ssh:
            if not self.remote_root:
                raise RuntimeError("Remote root is not configured.")
            self._ensure_dirs_local()
            target = Path(self.remote_root) / "queue" / "pending" / f"{job_id}.json"
            _write_local_json(target, job_data)
            return job_id

        # SSH mode
        b64_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
        pending_dir = self._remote_path("queue", "pending")
        pending_file = self._remote_path("queue", "pending", f"{job_id}.json")
        script = f"""set -euo pipefail
umask 077
mkdir -p {shlex.quote(pending_dir)}
PENDING_FILE={shlex.quote(pending_file)}
TMP_FILE="$PENDING_FILE.tmp.$$"
trap 'rm -f "$TMP_FILE"' EXIT
printf '%s' {shlex.quote(b64_content)} | base64 -d > "$TMP_FILE"
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$PENDING_FILE"
trap - EXIT
"""
        res = _ssh_script(script, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to submit remote job: {_sanitize_error(res.stderr)}")
        return job_id

    def list_pending_job_ids(self) -> List[str]:
        if not self.is_ssh:
            if not self.remote_root:
                return []
            pending_dir = Path(self.remote_root) / "queue" / "pending"
            if not pending_dir.is_dir():
                return []
            files = sorted(pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            ids = []
            for f in files:
                jid = f.stem
                try:
                    validate_job_id(jid)
                    ids.append(jid)
                except ValueError:
                    continue
            return ids

        pending_dir = self._remote_path("queue", "pending")
        script = f"""set -euo pipefail
if [ -d {shlex.quote(pending_dir)} ]; then
  ls -1tr {shlex.quote(pending_dir)}/*.json 2>/dev/null || true
fi
"""
        res = _ssh_script(script, timeout=30)
        if res.returncode != 0:
            return []
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        ids = []
        for line in lines:
            name = Path(line).stem
            try:
                validate_job_id(name)
                ids.append(name)
            except ValueError:
                continue
        return ids

    def claim_job(self, job_id: str, worker_id: str) -> Optional[Dict[str, Any]]:
        validate_job_id(job_id)
        now_str = datetime.now(timezone.utc).isoformat()

        if not self.is_ssh:
            if not self.remote_root:
                return None
            pending_file = Path(self.remote_root) / "queue" / "pending" / f"{job_id}.json"
            claimed_file = Path(self.remote_root) / "queue" / "claimed" / f"{job_id}.json"
            with file_lock(f"remote-queue-claim-{job_id}"):
                if not pending_file.is_file():
                    return None
                try:
                    data = json.loads(pending_file.read_text(encoding="utf-8"))
                    pending_file.rename(claimed_file)
                except (FileNotFoundError, OSError, json.JSONDecodeError):
                    return None

                data["worker_id"] = worker_id
                data["claimed_at"] = now_str
                _write_local_json(claimed_file, data)
                return data

        # SSH mode: atomic move
        pending_file = self._remote_path("queue", "pending", f"{job_id}.json")
        claimed_file = self._remote_path("queue", "claimed", f"{job_id}.json")
        script = f"""set -euo pipefail
PENDING={shlex.quote(pending_file)}
CLAIMED={shlex.quote(claimed_file)}
if mv "$PENDING" "$CLAIMED" 2>/dev/null; then
  chmod 600 "$CLAIMED"
  cat "$CLAIMED"
else
  echo "CLAIM_FAILED"
fi
"""
        res = _ssh_script(script, timeout=30)
        if res.returncode != 0 or "CLAIM_FAILED" in res.stdout:
            return None

        try:
            data = json.loads(res.stdout)
        except Exception:
            return None

        data["worker_id"] = worker_id
        data["claimed_at"] = now_str
        b64_content = base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("ascii")
        update_script = f"""set -euo pipefail
umask 077
CLAIMED={shlex.quote(claimed_file)}
TMP_FILE="$CLAIMED.tmp.$$"
trap 'rm -f "$TMP_FILE"' EXIT
printf '%s' {shlex.quote(b64_content)} | base64 -d > "$TMP_FILE"
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$CLAIMED"
trap - EXIT
"""
        update_res = _ssh_script(update_script, timeout=30)
        if update_res.returncode != 0:
            return None
        return data

    def requeue_stale_claims(self, stale_seconds: int = 300) -> int:
        now = time.time()
        requeued_count = 0

        if not self.is_ssh:
            if not self.remote_root:
                return 0
            claimed_dir = Path(self.remote_root) / "queue" / "claimed"
            if not claimed_dir.is_dir():
                return 0
            for claimed_file in list(claimed_dir.glob("*.json")):
                job_id = claimed_file.stem
                try:
                    validate_job_id(job_id)
                    data = json.loads(claimed_file.read_text(encoding="utf-8"))
                except Exception:
                    continue

                claimed_at_str = data.get("claimed_at")
                try:
                    claimed_dt = datetime.fromisoformat(claimed_at_str)
                    claimed_ts = claimed_dt.timestamp()
                except Exception:
                    claimed_ts = claimed_file.stat().st_mtime

                if (now - claimed_ts) > stale_seconds:
                    pending_file = Path(self.remote_root) / "queue" / "pending" / f"{job_id}.json"
                    data.pop("claimed_at", None)
                    data.pop("worker_id", None)
                    with file_lock(f"remote-queue-claim-{job_id}"):
                        try:
                            claimed_file.rename(pending_file)
                            _write_local_json(pending_file, data)
                            requeued_count += 1
                        except (FileNotFoundError, OSError):
                            pass
            return requeued_count

        # SSH mode stale scan
        claimed_dir = self._remote_path("queue", "claimed")
        script = f"""set -euo pipefail
if [ -d {shlex.quote(claimed_dir)} ]; then
  ls -1 {shlex.quote(claimed_dir)}/*.json 2>/dev/null || true
fi
"""
        res = _ssh_script(script, timeout=30)
        if res.returncode != 0:
            return 0
        claimed_paths = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        for cpath in claimed_paths:
            job_id = Path(cpath).stem
            try:
                validate_job_id(job_id)
            except ValueError:
                continue
            claimed_file = self._remote_path("queue", "claimed", f"{job_id}.json")
            cat_res = _ssh_script(f"cat -- {shlex.quote(claimed_file)}", timeout=30)
            if cat_res.returncode != 0:
                continue
            try:
                data = json.loads(cat_res.stdout)
            except Exception:
                continue

            claimed_at_str = data.get("claimed_at")
            try:
                claimed_dt = datetime.fromisoformat(claimed_at_str)
                claimed_ts = claimed_dt.timestamp()
            except Exception:
                stat_res = _ssh_script(
                    f"stat -c %Y -- {shlex.quote(claimed_file)}", timeout=30
                )
                try:
                    claimed_ts = float(stat_res.stdout.strip())
                except (TypeError, ValueError):
                    continue

            if (now - claimed_ts) > stale_seconds:
                data.pop("claimed_at", None)
                data.pop("worker_id", None)
                b64_content = base64.b64encode(json.dumps(data, indent=2).encode("utf-8")).decode("ascii")
                pending_file = self._remote_path("queue", "pending", f"{job_id}.json")
                requeue_script = f"""set -euo pipefail
umask 077
CLAIMED={shlex.quote(claimed_file)}
PENDING={shlex.quote(pending_file)}
if mv "$CLAIMED" "$PENDING" 2>/dev/null; then
  TMP_FILE="$PENDING.tmp.$$"
  trap 'rm -f "$TMP_FILE"' EXIT
  printf '%s' {shlex.quote(b64_content)} | base64 -d > "$TMP_FILE"
  chmod 600 "$TMP_FILE"
  mv "$TMP_FILE" "$PENDING"
  trap - EXIT
  echo "REQUEUED"
fi
"""
                r_res = _ssh_script(requeue_script, timeout=30)
                if "REQUEUED" in r_res.stdout:
                    requeued_count += 1
        return requeued_count

    def complete_job(
        self,
        job_id: str,
        *,
        status: str,
        worker_id: str,
        exit_code: int = 0,
        timed_out: bool = False,
        result: str = "",
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        validate_job_id(job_id)
        now_str = datetime.now(timezone.utc).isoformat()

        existing_data = self._read_job_raw(job_id) or {}
        completed_data = {
            **existing_data,
            "job_id": job_id,
            "status": status,
            "worker_id": worker_id,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "ended_at": now_str,
            "result": result,
            "stdout": stdout,
            "stderr": stderr,
        }
        content = json.dumps(completed_data, indent=2)

        if not self.is_ssh:
            if not self.remote_root:
                return
            done_file = Path(self.remote_root) / "queue" / "done" / f"{job_id}.json"
            claimed_file = Path(self.remote_root) / "queue" / "claimed" / f"{job_id}.json"
            pending_file = Path(self.remote_root) / "queue" / "pending" / f"{job_id}.json"

            with file_lock(f"remote-queue-claim-{job_id}"):
                _write_local_json(done_file, completed_data)
                claimed_file.unlink(missing_ok=True)
                pending_file.unlink(missing_ok=True)
            return

        # SSH mode
        b64_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
        done_dir = self._remote_path("queue", "done")
        done_file = self._remote_path("queue", "done", f"{job_id}.json")
        claimed_file = self._remote_path("queue", "claimed", f"{job_id}.json")
        pending_file = self._remote_path("queue", "pending", f"{job_id}.json")
        script = f"""set -euo pipefail
umask 077
mkdir -p {shlex.quote(done_dir)}
DONE_FILE={shlex.quote(done_file)}
TMP_FILE="$DONE_FILE.tmp.$$"
trap 'rm -f "$TMP_FILE"' EXIT
printf '%s' {shlex.quote(b64_content)} | base64 -d > "$TMP_FILE"
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$DONE_FILE"
trap - EXIT
rm -f -- {shlex.quote(claimed_file)} {shlex.quote(pending_file)}
"""
        res = _ssh_script(script, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to complete remote job: {_sanitize_error(res.stderr)}")

    def _read_job_raw(self, job_id: str) -> Optional[Dict[str, Any]]:
        for sub in ("claimed", "pending", "done"):
            if not self.is_ssh:
                if not self.remote_root:
                    return None
                p = Path(self.remote_root) / "queue" / sub / f"{job_id}.json"
                if p.is_file():
                    try:
                        return json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        return None
            else:
                p_str = self._remote_path("queue", sub, f"{job_id}.json")
                res = _ssh_script(f"cat -- {shlex.quote(p_str)}", timeout=30)
                if res.returncode == 0:
                    try:
                        return json.loads(res.stdout)
                    except Exception:
                        return None
        return None

    def get_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        validate_job_id(job_id)
        for sub in ("done", "claimed", "pending"):
            if not self.is_ssh:
                if not self.remote_root:
                    return None
                p = Path(self.remote_root) / "queue" / sub / f"{job_id}.json"
                if p.is_file():
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        state = data.get("status") or sub
                        return {"state": state, "job": data}
                    except Exception:
                        return None
            else:
                p_str = self._remote_path("queue", sub, f"{job_id}.json")
                res = _ssh_script(f"cat -- {shlex.quote(p_str)}", timeout=30)
                if res.returncode == 0:
                    try:
                        data = json.loads(res.stdout)
                        state = data.get("status") or sub
                        return {"state": state, "job": data}
                    except Exception:
                        return None
        return None

    def list_all_jobs(self) -> List[Dict[str, Any]]:
        results = []
        for sub in ("pending", "claimed", "done"):
            if not self.is_ssh:
                if not self.remote_root:
                    continue
                d = Path(self.remote_root) / "queue" / sub
                if not d.is_dir():
                    continue
                for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        results.append({"job_id": f.stem, "state": data.get("status") or sub, **data})
                    except Exception:
                        continue
            else:
                remote_dir = self._remote_path("queue", sub)
                script = f"ls -1 {shlex.quote(remote_dir)}/*.json 2>/dev/null || true"
                res = _ssh_script(script, timeout=30)
                if res.returncode == 0:
                    lines = [item.strip() for item in res.stdout.splitlines() if item.strip()]
                    for line in lines:
                        stem = Path(line).stem
                        try:
                            validate_job_id(stem)
                        except ValueError:
                            continue
                        remote_file = self._remote_path("queue", sub, f"{stem}.json")
                        cat_res = _ssh_script(
                            f"cat -- {shlex.quote(remote_file)}", timeout=30
                        )
                        if cat_res.returncode == 0:
                            try:
                                data = json.loads(cat_res.stdout)
                                results.append({"job_id": stem, "state": data.get("status") or sub, **data})
                            except Exception:
                                continue
        return results


def run_worker_once(
    queue: RemoteQueue,
    worker_id: str,
    allowed_repos: List[str],
    stale_seconds: int = 300,
) -> Optional[Dict[str, Any]]:
    # 1. Requeue stale claims
    queue.requeue_stale_claims(stale_seconds=stale_seconds)

    # 2. Poll pending jobs
    pending_ids = queue.list_pending_job_ids()
    for job_id in pending_ids:
        job = queue.claim_job(job_id, worker_id=worker_id)
        if job is None:
            continue

        repo_path = job.get("repo_path", "")
        if not validate_repo_path(repo_path, allowed_roots=allowed_repos):
            err_msg = _redact(
                f"Security error: repo_path {repo_path!r} is not in worker allow-list "
                f"{allowed_repos}."
            )
            queue.complete_job(
                job_id,
                status="failed",
                worker_id=worker_id,
                exit_code=-1,
                timed_out=False,
                result=err_msg,
                stderr=err_msg,
            )
            return {"job_id": job_id, "status": "failed", "error": err_msg}

        repo_path = str(Path(repo_path).resolve())
        if not Path(repo_path).exists():
            err_msg = _redact(
                f"Error: repo_path {repo_path!r} does not exist on worker machine."
            )
            queue.complete_job(
                job_id,
                status="failed",
                worker_id=worker_id,
                exit_code=-1,
                timed_out=False,
                result=err_msg,
                stderr=err_msg,
            )
            return {"job_id": job_id, "status": "failed", "error": err_msg}

        branch = job.get("branch")
        if branch:
            branch_check = subprocess.run(
                ["git", "-C", repo_path, "branch", "--show-current"],
                capture_output=True,
                text=True,
                check=False,
            )
            current_branch = branch_check.stdout.strip()
            if branch_check.returncode != 0 or current_branch != branch:
                err_msg = (
                    f"Branch mismatch: requested {branch!r}, but the worker checkout is on "
                    f"{current_branch or 'an unknown branch'!r}."
                )
                queue.complete_job(
                    job_id,
                    status="failed",
                    worker_id=worker_id,
                    exit_code=-1,
                    result=err_msg,
                    stderr=err_msg,
                )
                return {"job_id": job_id, "status": "failed", "error": err_msg}

        prompt = job.get("prompt") or ""
        if not isinstance(prompt, str) or not prompt.strip():
            err_msg = "Invalid remote job: prompt is empty."
            queue.complete_job(
                job_id,
                status="failed",
                worker_id=worker_id,
                exit_code=-1,
                result=err_msg,
                stderr=err_msg,
            )
            return {"job_id": job_id, "status": "failed", "error": err_msg}

        agent = job.get("agent")
        role = job.get("role")
        if not agent and not role:
            agent = "auto"

        try:
            import asyncio
            from mindsync.dispatch import store
            from mindsync.dispatch.runner import run_task, job_result as get_job_result

            run_kwargs: Dict[str, Any] = {
                "prompt": prompt,
                "cwd": repo_path,
                "write": True,
                "background": False,
            }
            if role:
                run_kwargs["role"] = role
            else:
                run_kwargs["agent"] = agent

            res = asyncio.run(run_task(**run_kwargs))
            local_job = res["job"]
            job_res = get_job_result(local_job["id"])
            local_job = job_res["meta"]
            local_paths = store.job_paths(local_job["id"])

            status = local_job.get("status", "done")
            exit_code = local_job.get("exitCode", 0)
            timed_out = local_job.get("timedOut", False)
            result_text = _excerpt(job_res.get("result") or "", limit=8000)
            stdout_text = (
                local_paths["stdout"].read_text(encoding="utf-8", errors="replace")
                if local_paths["stdout"].is_file()
                else ""
            )
            stderr_text = (
                local_paths["stderr"].read_text(encoding="utf-8", errors="replace")
                if local_paths["stderr"].is_file()
                else ""
            )

            queue.complete_job(
                job_id,
                status=status,
                worker_id=worker_id,
                exit_code=exit_code,
                timed_out=timed_out,
                result=result_text,
                stdout=_excerpt(stdout_text),
                stderr=_excerpt(stderr_text),
            )
            return {"job_id": job_id, "status": status, "result": result_text}

        except Exception as exc:
            err_msg = f"Execution error: {_sanitize_error(str(exc))}"
            queue.complete_job(
                job_id,
                status="failed",
                worker_id=worker_id,
                exit_code=-1,
                timed_out=False,
                result=err_msg,
                stderr=err_msg,
            )
            return {"job_id": job_id, "status": "failed", "error": err_msg}

    return None


def run_worker_loop(
    queue: RemoteQueue,
    worker_id: str,
    allowed_repos: List[str],
    poll_seconds: int = 30,
    stale_seconds: int = 300,
) -> None:
    queue.ensure_remote_dirs()
    while True:
        try:
            run_worker_once(
                queue=queue,
                worker_id=worker_id,
                allowed_repos=allowed_repos,
                stale_seconds=stale_seconds,
            )
        except Exception as exc:
            print(f"Worker iteration error: {_sanitize_error(str(exc))}")
        time.sleep(poll_seconds)
