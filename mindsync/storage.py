"""Local JSON/JSONL storage with exclusive file locks."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
import warnings
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from mindsync.config import chmod_tree_0600, settings

# On Windows, ``msvcrt`` file locks do not reliably contend between threads in
# one process. A per-name thread mutex serialises same-process access before
# the OS lock loop without changing cross-process semantics.
_THREAD_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = (
    weakref.WeakValueDictionary()
)
_THREAD_LOCKS_GUARD = threading.Lock()


def _get_thread_lock(name: str) -> threading.Lock:
    """Return the per-name thread mutex used on Windows.

    Ephemeral lock names (for example per-job dispatch locks) must not pin
    mutexes for the process lifetime. ``WeakValueDictionary`` drops entries
    once no ``file_lock`` call still holds a strong reference, while
    concurrent waiters for the same name still share one live ``Lock``.
    """
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(name)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[name] = lock
        return lock


def _contention_sleep(attempt: int, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    base = settings.lock_contention_backoff_base_seconds
    cap = settings.lock_contention_backoff_max_seconds
    delay = min(base * (2**attempt), cap, remaining)
    if delay > 0:
        time.sleep(delay)


@contextmanager
def _queue_file_lock() -> Generator[None, None, None]:
    with file_lock("queue", timeout=settings.queue_lock_timeout_seconds):
        yield


def atomic_private_write(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Atomically replace ``path`` without ever creating a loose-permission temp file."""
    temp = path.with_name(f".{path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    fd: int | None = None
    try:
        fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        try:
            os.fchmod(fd, mode)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
    finally:
        if fd is not None:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _try_os_lock(fd: int) -> bool:
    """Acquire an exclusive non-blocking OS lock on byte zero / the file."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return False
    return True


def _release_os_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(
    name: str, timeout: float | None = None, stale_after: float | None = None
) -> Generator[None, None, None]:
    """Cross-platform exclusive lock backed by the operating system.

    The lock file is intentionally persistent. The kernel releases the actual
    lock when a process exits, so crash recovery needs no age-based unlink or
    rename. That removes the check-then-replace race where a stale-lock
    contender could delete a newly acquired live lock.

    ``stale_after`` remains accepted for API compatibility but is deprecated
    and ignored: abandoned OS locks are released immediately by the kernel.
    """
    settings.ensure_dirs()
    timeout = settings.lock_timeout_seconds if timeout is None else timeout
    if stale_after is not None:
        warnings.warn(
            "file_lock(stale_after=...) is deprecated and ignored; OS locks recover on exit",
            DeprecationWarning,
            stacklevel=2,
        )
    lock_path = settings.lock_dir / f"{name}.lock"
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    deadline = time.monotonic() + timeout
    thread_lock = _get_thread_lock(name) if os.name == "nt" else None
    thread_lock_acquired = False
    if thread_lock is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not thread_lock.acquire(timeout=remaining):
            raise TimeoutError(f"Could not acquire lock: {lock_path}") from None
        thread_lock_acquired = True

    fd: int | None = None
    attempt = 0
    try:
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                if _try_os_lock(fd):
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, f"{token}\n".encode("ascii"))
                    os.ftruncate(fd, len(token) + 1)
                    os.fsync(fd)
                    break
                os.close(fd)
                fd = None
            except OSError:
                if fd is not None:
                    os.close(fd)
                    fd = None
                if os.name != "nt":
                    raise

            if time.monotonic() >= deadline:
                raise TimeoutError(f"Could not acquire lock: {lock_path}") from None
            _contention_sleep(attempt, deadline)
            attempt += 1

        try:
            yield
        finally:
            if fd is not None:
                try:
                    _release_os_lock(fd)
                finally:
                    os.close(fd)
    finally:
        if thread_lock_acquired and thread_lock is not None:
            thread_lock.release()


def _default_state() -> dict[str, Any]:
    return {
        "active_project": "unknown",
        "active_branch": "unknown",
        "last_updated_by": None,
        "timestamp": _utc_now(),
        "custom_names": {},
        "agents_focus": {},
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict[str, Any]:
    settings.ensure_dirs()
    path = settings.state_file
    if not path.exists():
        state = _default_state()
        save_state(state)
        return state
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        data.setdefault("agents_focus", {})
        data.setdefault("custom_names", {})
        return data
    except (OSError, json.JSONDecodeError):
        return _default_state()


def save_state(state: dict[str, Any]) -> None:
    settings.ensure_dirs()
    path = settings.state_file
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    atomic_private_write(path, payload)


@contextmanager
def locked_state() -> Generator[dict[str, Any], None, None]:
    """Load-modify-save under exclusive lock."""
    with file_lock("state"):
        state = load_state()
        yield state
        save_state(state)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    settings.ensure_dirs()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    fd = os.open(str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):
        pass
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def log_audit(agent_name: str, action: str, details: str) -> None:
    append_jsonl(
        settings.audit_file,
        {
            "timestamp": _utc_now(),
            "agent": agent_name,
            "action": action,
            "details": details,
        },
    )


def enqueue_fact(fact: dict[str, Any]) -> None:
    with _queue_file_lock():
        append_jsonl(settings.offline_queue_file, fact)


def read_queue() -> list[dict[str, Any]]:
    path = settings.offline_queue_file
    if not path.exists() or path.stat().st_size == 0:
        return []
    facts: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                facts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return facts


def claim_offline_queue() -> tuple[str, list[dict[str, Any]], int]:
    """Claim the offline queue file for processing.

    Returns (spool_id, facts, malformed_count). Lines that fail to parse as
    JSON are quarantined straight to the dead-letter file and excluded from
    ``facts`` -- ``malformed_count`` lets the caller tell "queue was empty"
    apart from "queue was entirely malformed" (both leave ``facts`` empty).
    """
    settings.ensure_dirs()
    path = settings.offline_queue_file
    with _queue_file_lock():
        if not path.exists() or path.stat().st_size == 0:
            return "", [], 0
        spool_id = uuid.uuid4().hex
        spool_path = settings.spool_dir / f"spool-{spool_id}.jsonl"
        try:
            os.replace(path, spool_path)
            # Create a fresh empty queue file so readers don't error
            open(path, "a", encoding="utf-8").close()
        except OSError:
            return "", [], 0

    facts: list[dict[str, Any]] = []
    malformed_count = 0
    with open(spool_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                facts.append(json.loads(line))
            except json.JSONDecodeError:
                record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": "malformed_json",
                    "raw_record": line,
                }
                with _queue_file_lock():
                    append_jsonl(settings.dead_letter_file, record)
                malformed_count += 1
                continue
    return spool_id, facts, malformed_count


def requeue_failed_facts(
    spool_id: str, failed: list[dict[str, Any]], dead_letter: list[dict[str, Any]]
) -> None:
    settings.ensure_dirs()
    if dead_letter:
        # We don't have a specific file_lock for dead_letter, but we can use queue lock or just append
        with _queue_file_lock():
            for fact in dead_letter:
                append_jsonl(settings.dead_letter_file, fact)

    if failed:
        with _queue_file_lock():
            for fact in failed:
                append_jsonl(settings.offline_queue_file, fact)

    if spool_id:
        spool_path = settings.spool_dir / f"spool-{spool_id}.jsonl"
        spool_path.unlink(missing_ok=True)


def recover_orphan_spools() -> None:
    settings.ensure_dirs()
    for spool_path in settings.spool_dir.glob("spool-*.jsonl"):
        try:
            facts: list[dict[str, Any]] = []
            with open(spool_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        facts.append(json.loads(line))
                    except json.JSONDecodeError:
                        record = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "error": "malformed_json",
                            "raw_record": line,
                        }
                        with _queue_file_lock():
                            append_jsonl(settings.dead_letter_file, record)
                        continue
            if facts:
                with _queue_file_lock():
                    for fact in facts:
                        append_jsonl(settings.offline_queue_file, fact)
            spool_path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def locked_truth() -> Generator[None, None, None]:
    """Lock the compiled truth directory for atomic reads/updates."""
    with file_lock("truth"):
        yield


def _validate_staging_manifest(staging_dir: Path) -> None:
    files = list(staging_dir.glob("*.md"))
    import re
    for f in files:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", f.stem):
            raise ValueError(f"Staged file has invalid entity name: {f.name}")
        try:
            f.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Staged file {f.name} is not valid UTF-8: {exc}")


def publish_compiled_truth(staging_dir: Path) -> None:
    settings.ensure_dirs()
    dest_dir = settings.compiled_truth_dir

    _validate_staging_manifest(staging_dir)

    staged_files = list(staging_dir.glob("*.md"))
    if not staged_files:
        # A successful-but-empty pull (remote returned nothing, or the
        # staging dir came back empty) must never be allowed to wipe out
        # the existing truth. Abort before touching dest_dir.
        raise ValueError(
            "Refusing to publish: staging directory has no compiled-truth "
            "files (an empty pull would erase the existing truth)"
        )

    with locked_truth():
        import tempfile
        import shutil

        temp_dest = Path(tempfile.mkdtemp(dir=str(settings.home), prefix="truth-new-"))
        # Set when a double failure (swap + rollback) leaves temp_dest as the
        # only surviving copy of the new truth — must not be deleted then.
        preserve_temp_dest = False
        try:
            for staged in staged_files:
                dest_file = temp_dest / staged.name
                shutil.copy2(staged, dest_file)
                try:
                    dest_file.chmod(0o600)
                except OSError:
                    pass
            chmod_tree_0600(temp_dest)

            backup_dest: Path | None = Path(
                tempfile.mkdtemp(dir=str(settings.home), prefix="truth-old-")
            )
            backup_dest.rmdir()
            backup_created = False
            try:
                os.rename(str(dest_dir), str(backup_dest))
                backup_created = True
            except OSError:
                backup_dest = None

            try:
                os.rename(str(temp_dest), str(dest_dir))
            except Exception as exc:
                if backup_created and backup_dest is not None and backup_dest.exists():
                    try:
                        os.rename(str(backup_dest), str(dest_dir))
                    except OSError as rollback_exc:
                        # Catastrophic: the swap failed AND restoring the
                        # backup failed. compiled-truth may now be missing.
                        # Do not silently swallow this — and do not delete
                        # temp_dest/backup_dest, they hold the only
                        # surviving copies of the new/old truth for manual
                        # recovery.
                        preserve_temp_dest = True
                        raise OSError(
                            "compiled-truth publish failed AND rollback failed; "
                            "compiled-truth directory may be missing. New data "
                            f"preserved at {temp_dest}, old data preserved at "
                            f"{backup_dest}. swap error: {exc}; "
                            f"rollback error: {rollback_exc}"
                        ) from rollback_exc
                raise OSError(f"Failed to swap compiled-truth directory: {exc}") from exc

            chmod_tree_0600(dest_dir)

            if backup_created and backup_dest is not None and backup_dest.exists():
                shutil.rmtree(backup_dest, ignore_errors=True)

        finally:
            if not preserve_temp_dest and temp_dest.exists():
                shutil.rmtree(temp_dest, ignore_errors=True)


def read_compiled_truth() -> dict[str, str]:
    settings.ensure_dirs()
    out: dict[str, str] = {}
    with locked_truth():
        for path in sorted(settings.compiled_truth_dir.glob("*.md")):
            try:
                out[path.stem] = path.read_text(encoding="utf-8")
            except OSError:
                continue
    return out
