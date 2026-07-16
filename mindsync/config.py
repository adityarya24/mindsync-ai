"""Environment-driven configuration for MindSync."""

from __future__ import annotations

import os
from pathlib import Path


def _default_home() -> Path:
    override = os.environ.get("MINDSYNC_HOME") or os.environ.get("LOCAL_GBRAIN_DIR")
    if override:
        return Path(override)
    # Sensible fallback for this machine; override via MINDSYNC_HOME elsewhere.
    return Path.home() / ".local-gbrain"


class Settings:
    def __init__(self) -> None:
        self.home: Path = _default_home()
        self.state_file: Path = self.home / "local-state.json"
        self.audit_file: Path = self.home / "local-audit.jsonl"
        self.offline_queue_file: Path = self.home / "offline_queue.jsonl"
        self.compiled_truth_dir: Path = self.home / "compiled-truth"
        self.lock_dir: Path = self.home / ".locks"

        self.ssh_host: str = os.environ.get("MINDSYNC_SSH_HOST", "openclaw-vps")
        self.remote_root: str = os.environ.get(
            "MINDSYNC_REMOTE_ROOT",
            "/home/aditya/.openclaw/workspace/gbrain",
        )
        self.ssh_connect_timeout: int = int(os.environ.get("MINDSYNC_SSH_TIMEOUT", "3"))
        self.focus_stale_seconds: int = int(os.environ.get("MINDSYNC_FOCUS_STALE_SECS", "7200"))
        self.vps_cache_ttl_seconds: float = float(os.environ.get("MINDSYNC_VPS_CACHE_TTL", "30"))
        self.lock_timeout_seconds: float = float(os.environ.get("MINDSYNC_LOCK_TIMEOUT", "5"))

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.compiled_truth_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
