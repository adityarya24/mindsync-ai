"""Environment-driven configuration for MindSync.

All deployment-specific values come from environment variables.
The default install is local-only; remote sync is opt-in.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import threading
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _positive_float_env(name: str, default: str) -> float:
    try:
        value = float(_env(name, default) or default)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if not value:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _default_home() -> Path:
    override = _env("MINDSYNC_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mindsync"


def legacy_dispatch_home() -> Path:
    return Path.home() / ".claude" / "agent-dispatch"


_LEGACY_MIGRATION_MARKER = ".legacy-migration-complete"
_LEGACY_MIGRATION_LOCK = "dispatch-legacy-migration"
_LEGACY_MIGRATION_THREAD_LOCK = threading.Lock()


def _copy_missing(src: Path, dest: Path) -> None:
    """Copy files and dirs from src into dest without replacing existing ones."""
    if src.is_symlink() or src.is_file():
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest, follow_symlinks=True)
        return
    if not src.is_dir():
        return
    dest.mkdir(parents=True, exist_ok=True)
    try:
        dest.chmod(0o700)
    except OSError:
        pass
    for item in src.iterdir():
        _copy_missing(item, dest / item.name)


def migrate_legacy_dispatch_home(target: Path, *, legacy: Path | None = None) -> bool:
    """Copy a Claude-era dispatch home into the MindSync dispatch home once.

    Existing files in ``target`` win. Returns True if the legacy tree existed
    and the copy walk completed. A marker prevents later legacy files from
    being copied after the migration has completed.
    """
    source = legacy if legacy is not None else legacy_dispatch_home()
    try:
        if source.resolve() == target.resolve():
            return False
    except OSError:
        return False

    # Import lazily to avoid the config -> storage -> config import cycle.
    from mindsync.storage import atomic_private_write, file_lock

    marker = target / _LEGACY_MIGRATION_MARKER
    # The OS lock coordinates separate processes. On Windows, ``msvcrt``
    # locking does not reliably contend between threads in one process, so a
    # process-local lock closes that remaining check-then-copy race.
    with _LEGACY_MIGRATION_THREAD_LOCK:
        with file_lock(_LEGACY_MIGRATION_LOCK):
            if marker.exists():
                return False
            if not source.is_dir():
                return False
            _copy_missing(source, target)
            atomic_private_write(marker, "legacy dispatch migration complete\n")
    return True


def dispatch_home() -> Path:
    """Roster and job directory. Default is ``~/.mindsync/dispatch``.

    ``AGENT_DISPATCH_HOME`` still wins for tests and explicit overrides. When
    using the default MindSync home, a one-time copy from the legacy
    ``~/.claude/agent-dispatch`` path fills any missing files.
    """
    override = _env("AGENT_DISPATCH_HOME")
    if override:
        return Path(override).expanduser()
    target = _default_home() / "dispatch"
    default_target = Path.home() / ".mindsync" / "dispatch"
    try:
        migrate = target.resolve() == default_target.resolve()
    except OSError:
        migrate = False
    if migrate:
        migrate_legacy_dispatch_home(target)
    return target


def chmod_tree_0600(path: Path) -> None:
    """Recursively enforce 0600 on files and 0700 on dirs under `path`.

    Best-effort: permission errors on individual entries are swallowed so one
    unreachable file doesn't abort the whole migration. Used to bring
    existing (pre-hardening) files under ``compiled-truth`` into line, not
    just newly written ones.
    """
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        root_path = Path(root)
        for d in dirs:
            try:
                (root_path / d).chmod(0o700)
            except OSError:
                pass
        for f in files:
            try:
                (root_path / f).chmod(0o600)
            except OSError:
                pass
    try:
        path.chmod(0o700)
    except OSError:
        pass


class Settings:
    def __init__(self) -> None:
        self.home: Path = _default_home()
        self.state_file: Path = self.home / "local-state.json"
        self.audit_file: Path = self.home / "local-audit.jsonl"
        self.offline_queue_file: Path = self.home / "offline_queue.jsonl"
        self.spool_dir: Path = self.home / "spools"
        self.dead_letter_file: Path = self.home / "dead_letter.jsonl"
        self.compiled_truth_dir: Path = self.home / "compiled-truth"
        self.lock_dir: Path = self.home / ".locks"
        self.events_file: Path = self.home / "events.jsonl"
        self.subscriptions_file: Path = self.home / "subscriptions.json"
        self.orchestration_file: Path = self.home / "orchestration.json"
        self.memory_db_file: Path = self.home / "session_memory.db"
        self.memory_model_url: str = _env(
            "MINDSYNC_MEMORY_MODEL_URL", "http://127.0.0.1:11434"
        )
        self.memory_embedding_model: str = _env("MINDSYNC_MEMORY_EMBEDDING_MODEL")
        self.memory_consolidation_model: str = _env(
            "MINDSYNC_MEMORY_CONSOLIDATION_MODEL"
        )
        try:
            self.memory_model_timeout_seconds = float(
                _env("MINDSYNC_MEMORY_MODEL_TIMEOUT", "60") or "60"
            )
        except ValueError as exc:
            raise ValueError(
                "MINDSYNC_MEMORY_MODEL_TIMEOUT must be a number"
            ) from exc
        if not 0 < self.memory_model_timeout_seconds <= 300:
            raise ValueError(
                "MINDSYNC_MEMORY_MODEL_TIMEOUT must be greater than 0 and at most 300"
            )

        # Remote is disabled until both host and root are set.
        self.ssh_host: str = _env("MINDSYNC_SSH_HOST")
        self.remote_root: str = _env("MINDSYNC_REMOTE_ROOT")

        # Relative to remote_root (or absolute paths if you prefer).
        self.remote_env_file: str = _env("MINDSYNC_REMOTE_ENV_FILE", "config/mindsync.env")
        self.remote_write_script: str = _env(
            "MINDSYNC_REMOTE_WRITE_SCRIPT",
            "tools/mindsync_fact.py",
        )
        self.remote_consolidate_script: str = _env(
            "MINDSYNC_REMOTE_CONSOLIDATE_SCRIPT",
            "tools/mindsync_consolidate.py",
        )
        self.remote_truth_subdir: str = _env(
            "MINDSYNC_REMOTE_TRUTH_SUBDIR",
            "compiled-truth",
        )

        # Path to the ssh client to shell out to. Empty means "auto-detect"
        # (see mindsync.bridge.resolve_openssh_tool) — set this to pin a
        # specific OpenSSH build when PATH resolution picks the wrong one.
        self.ssh_bin: str = _env("MINDSYNC_SSH_BIN")

        self.ssh_connect_timeout: int = int(_env("MINDSYNC_SSH_TIMEOUT", "10") or "10")
        self.focus_stale_seconds: int = int(_env("MINDSYNC_FOCUS_STALE_SECS", "7200") or "7200")
        self.remote_cache_ttl_seconds: float = float(
            _env("MINDSYNC_REMOTE_CACHE_TTL", "30") or "30"
        )
        self.lock_timeout_seconds: float = float(_env("MINDSYNC_LOCK_TIMEOUT", "5") or "5")
        # Queue writers (enqueue / claim / requeue) can see bursts of same-process
        # threads on Windows where msvcrt locking is slow; give them a longer,
        # still bounded, contention deadline than generic locks.
        self.queue_lock_timeout_seconds: float = _positive_float_env(
            "MINDSYNC_QUEUE_LOCK_TIMEOUT", "30"
        )
        self.lock_contention_backoff_base_seconds: float = _positive_float_env(
            "MINDSYNC_LOCK_CONTENTION_BACKOFF_BASE", "0.01"
        )
        self.lock_contention_backoff_max_seconds: float = _positive_float_env(
            "MINDSYNC_LOCK_CONTENTION_BACKOFF_MAX", "0.25"
        )
        # Kept for compatibility with pre-1.3 deployments. Locks are now
        # kernel-managed and released automatically when a process exits, so
        # storage.file_lock no longer uses an age threshold.
        self.lock_stale_seconds: float = float(
            _env("MINDSYNC_LOCK_STALE_SECS", "60") or "60"
        )

        self.worker_id: str = _env("MINDSYNC_WORKER_ID") or os.environ.get(
            "COMPUTERNAME", os.environ.get("HOSTNAME", "laptop-worker")
        )
        self.worker_poll_seconds: int = int(_env("MINDSYNC_WORKER_POLL_SECS", "30") or "30")
        self.worker_claim_stale_seconds: int = int(
            _env("MINDSYNC_WORKER_CLAIM_STALE_SECS", "300") or "300"
        )
        # Remote orchestrator execution is a privileged local opt-in. A remote
        # payload can request it, but the laptop owner must enable the mode too.
        self.worker_allow_orchestrator: bool = _env_bool(
            "MINDSYNC_WORKER_ALLOW_ORCHESTRATOR", False
        )
        if self.worker_poll_seconds < 1 or self.worker_claim_stale_seconds < 1:
            raise ValueError("Worker poll and stale intervals must be at least 1 second.")
        allowed_str = (
            _env("MINDSYNC_WORKER_ALLOWED_ROOTS")
            or _env("MINDSYNC_WORKER_ALLOWED_REPOS")
            or _env("MINDSYNC_ALLOWED_REPOS")
        )
        self.allowed_repos: list[str] = [
            p.strip() for p in re.split(r"[,;]", allowed_str) if p.strip()
        ]

    @property
    def remote_enabled(self) -> bool:
        return bool(self.ssh_host and self.remote_root)

    def ensure_dirs(self) -> None:
        def _make(p: Path) -> None:
            p.mkdir(parents=True, exist_ok=True)
            try:
                p.chmod(0o700)
            except OSError:
                pass
        
        _make(self.home)
        _make(self.compiled_truth_dir)
        _make(self.lock_dir)
        _make(self.spool_dir)

        # Enforce 0o600 permissions on existing files in home directory (migration)
        for item in self.home.glob("*"):
            if item.is_file() and not item.name.startswith("."):
                try:
                    item.chmod(0o600)
                except OSError:
                    pass

        # compiled-truth holds nested files that the top-level scan above
        # skips (it's a directory, not a file). Walk it recursively so
        # files copied in before this hardening was added get migrated too.
        chmod_tree_0600(self.compiled_truth_dir)

settings = Settings()
