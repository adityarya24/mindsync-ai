"""Local JSON/JSONL storage with exclusive file locks."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from mindsync.config import chmod_tree_0600, settings


@contextmanager
def file_lock(name: str, timeout: float | None = None) -> Generator[None, None, None]:
    """Cross-platform exclusive lock via O_EXCL lockfiles."""
    settings.ensure_dirs()
    timeout = settings.lock_timeout_seconds if timeout is None else timeout
    lock_path = settings.lock_dir / f"{name}.lock"
    # Unique token so release (and steal) only ever removes a lock we own;
    # a bare unlink could delete a lock another process just acquired.
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    deadline = time.time() + timeout

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{token}\n".encode("ascii"))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            if time.time() >= deadline:
                # Stale lock recovery: if lock is older than 60s, steal it.
                # os.replace is atomic, so concurrent stealers race for the
                # rename and only the winner removes the stale file; losers
                # just retry the acquire.
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    continue  # lock vanished; retry acquire
                if age <= 60:
                    raise TimeoutError(f"Could not acquire lock: {lock_path}") from None
                stale = lock_path.with_name(f"{lock_path.name}.stale-{token}")
                try:
                    os.replace(str(lock_path), str(stale))
                    os.unlink(str(stale))
                except OSError:
                    pass
                continue
            time.sleep(0.05)

    try:
        yield
    finally:
        try:
            if lock_path.read_text(encoding="ascii").strip() == token:
                lock_path.unlink(missing_ok=True)
        except OSError:
            pass


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
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


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
    existed = path.exists()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    if not existed:
        try:
            path.chmod(0o600)
        except OSError:
            pass


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
    with file_lock("queue"):
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


def claim_offline_queue() -> tuple[str, list[dict[str, Any]]]:
    settings.ensure_dirs()
    path = settings.offline_queue_file
    with file_lock("queue"):
        if not path.exists() or path.stat().st_size == 0:
            return "", []
        spool_id = uuid.uuid4().hex
        spool_path = settings.spool_dir / f"spool-{spool_id}.jsonl"
        try:
            os.replace(path, spool_path)
            # Create a fresh empty queue file so readers don't error
            open(path, "a", encoding="utf-8").close()
        except OSError:
            return "", []

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
                with file_lock("queue"):
                    append_jsonl(settings.dead_letter_file, record)
                continue
    return spool_id, facts


def requeue_failed_facts(
    spool_id: str, failed: list[dict[str, Any]], dead_letter: list[dict[str, Any]]
) -> None:
    settings.ensure_dirs()
    if dead_letter:
        # We don't have a specific file_lock for dead_letter, but we can use queue lock or just append
        with file_lock("queue"):
            for fact in dead_letter:
                append_jsonl(settings.dead_letter_file, fact)

    if failed:
        with file_lock("queue"):
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
                        with file_lock("queue"):
                            append_jsonl(settings.dead_letter_file, record)
                        continue
            if facts:
                with file_lock("queue"):
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
