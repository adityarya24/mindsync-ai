"""Shared MindSync home isolation for hermetic tests."""

from __future__ import annotations

from pathlib import Path

import mindsync.config as config_mod
import mindsync.memory as memory_mod
import mindsync.orchestration as orchestration_mod
import mindsync.storage as storage_mod


def rebind_settings(home: Path) -> config_mod.Settings:
    """Point every module-level settings singleton at one fresh home."""
    settings = config_mod.Settings()
    config_mod.settings = settings
    storage_mod.settings = settings
    orchestration_mod.settings = settings
    memory_mod.settings = settings
    settings.ensure_dirs()
    return settings


def isolate_mindsync_home(
    tmp_path: Path,
    monkeypatch,
    *,
    dispatch_home: bool = True,
    codex_home: bool = False,
) -> Path:
    """Isolate MINDSYNC_HOME and rebind settings so live policy cannot leak in."""
    ms_home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(ms_home))
    if dispatch_home:
        dispatch_dir = tmp_path / "dispatch-home"
        dispatch_dir.mkdir(exist_ok=True)
        monkeypatch.setenv("AGENT_DISPATCH_HOME", str(dispatch_dir))
    if codex_home:
        codex_dir = tmp_path / "codex-home"
        codex_dir.mkdir(exist_ok=True)
        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    rebind_settings(ms_home)
    return ms_home
