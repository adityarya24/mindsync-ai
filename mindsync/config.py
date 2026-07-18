"""Environment-driven configuration for MindSync.

All deployment-specific values come from environment variables.
The default install is local-only; remote sync is opt-in.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _default_home() -> Path:
    override = _env("MINDSYNC_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mindsync"


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

        self.ssh_connect_timeout: int = int(_env("MINDSYNC_SSH_TIMEOUT", "10") or "10")
        self.focus_stale_seconds: int = int(_env("MINDSYNC_FOCUS_STALE_SECS", "7200") or "7200")
        self.remote_cache_ttl_seconds: float = float(
            _env("MINDSYNC_REMOTE_CACHE_TTL", "30") or "30"
        )
        self.lock_timeout_seconds: float = float(_env("MINDSYNC_LOCK_TIMEOUT", "5") or "5")
        # A lock is only eligible to be stolen once its holder has stopped
        # renewing it for this long (see mindsync.storage.file_lock). A live
        # holder renews well inside this window, so a slow-but-alive
        # operation is never mistaken for a crashed one.
        self.lock_stale_seconds: float = float(
            _env("MINDSYNC_LOCK_STALE_SECS", "60") or "60"
        )

    @property
    def remote_enabled(self) -> bool:
        return bool(self.ssh_host and self.remote_root)

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.compiled_truth_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

settings = Settings()
