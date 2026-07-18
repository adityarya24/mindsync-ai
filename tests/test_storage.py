import json
import os
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.storage as storage


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    return config_mod.settings


def test_state_roundtrip(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    with storage.locked_state() as state:
        state["active_project"] = "mindsync-mcp"
        state["agents_focus"]["agent-b"] = {
            "project": "mindsync-mcp",
            "focus": "tests",
            "timestamp": "2026-07-16T00:00:00+00:00",
        }
    loaded = storage.load_state()
    assert loaded["active_project"] == "mindsync-mcp"
    assert "agent-b" in loaded["agents_focus"]
    assert settings.state_file.exists()


def test_queue_enqueue_and_claim(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    storage.enqueue_fact({"entity": "a", "attribute": "b", "text": "one"})
    storage.enqueue_fact({"entity": "c", "attribute": "d", "text": "two"})
    facts = storage.read_queue()
    assert len(facts) == 2
    spool_id, claimed, malformed_count = storage.claim_offline_queue()
    assert len(claimed) == 2
    assert malformed_count == 0
    assert len(storage.read_queue()) == 0
    storage.requeue_failed_facts(spool_id, [claimed[1]], [])
    left = storage.read_queue()
    assert len(left) == 1
    assert left[0]["entity"] == "c"


def test_audit_append(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    storage.log_audit("agent-b", "test", "hello")
    lines = settings.audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["agent"] == "agent-b"
    assert rec["action"] == "test"


def test_publish_rejects_empty_staging(tmp_path, monkeypatch):
    """Blocker #1: a successful-but-empty pull must never erase the
    existing compiled-truth. publish_compiled_truth must abort before
    touching dest_dir when the staging dir has no *.md files."""
    settings = _isolate(tmp_path, monkeypatch)
    settings.compiled_truth_dir.mkdir(parents=True, exist_ok=True)
    existing = settings.compiled_truth_dir / "keep-me.md"
    existing.write_text("must survive", encoding="utf-8")

    empty_staging = tmp_path / "empty-staging"
    empty_staging.mkdir()

    with pytest.raises(ValueError):
        storage.publish_compiled_truth(empty_staging)

    # Existing truth must be completely untouched.
    assert existing.exists()
    assert existing.read_text(encoding="utf-8") == "must survive"
    assert list(settings.compiled_truth_dir.glob("*.md")) == [existing]


def test_publish_forced_swap_and_rollback_failure(tmp_path, monkeypatch):
    """Blocker #2: if the atomic swap fails AND the rollback also fails,
    publish_compiled_truth must raise a clear error rather than silently
    leaving compiled-truth missing, and must not destroy the surviving
    old/new data (needed for manual recovery)."""
    settings = _isolate(tmp_path, monkeypatch)
    settings.compiled_truth_dir.mkdir(parents=True, exist_ok=True)
    old_file = settings.compiled_truth_dir / "old-entity.md"
    old_file.write_text("old content", encoding="utf-8")

    staging_dir = tmp_path / "incoming"
    staging_dir.mkdir()
    (staging_dir / "new-entity.md").write_text("new content", encoding="utf-8")

    real_rename = os.rename

    def fake_rename(src, dst):
        s = str(src)
        # Force-fail both the swap-in (temp_dest -> dest_dir, "truth-new-")
        # and the rollback (backup_dest -> dest_dir, "truth-old-"), while
        # letting the initial dest_dir -> backup_dest rename succeed.
        if "truth-new-" in s or "truth-old-" in s:
            raise OSError(f"forced rename failure for {s}")
        return real_rename(src, dst)

    monkeypatch.setattr(storage.os, "rename", fake_rename)

    with pytest.raises(OSError) as excinfo:
        storage.publish_compiled_truth(staging_dir)

    msg = str(excinfo.value)
    assert "rollback failed" in msg.lower()

    # Worst case: compiled-truth is missing...
    assert not settings.compiled_truth_dir.exists()

    # ...but nothing was silently destroyed. Both the pre-publish backup
    # (old truth) and the staged new truth survive on disk for recovery.
    home_entries = list(settings.home.iterdir())
    backups = [p for p in home_entries if p.name.startswith("truth-old-")]
    staged_new = [p for p in home_entries if p.name.startswith("truth-new-")]
    assert len(backups) == 1
    assert len(staged_new) == 1
    assert (backups[0] / "old-entity.md").read_text(encoding="utf-8") == "old content"
    assert (staged_new[0] / "new-entity.md").read_text(encoding="utf-8") == "new content"


def test_publish_success_leaves_no_temp_dirs_behind(tmp_path, monkeypatch):
    """Sanity check that the happy path still cleans up temp/backup dirs
    and doesn't regress after the rollback-hardening changes."""
    settings = _isolate(tmp_path, monkeypatch)
    settings.compiled_truth_dir.mkdir(parents=True, exist_ok=True)
    (settings.compiled_truth_dir / "old-entity.md").write_text("old", encoding="utf-8")

    staging_dir = tmp_path / "incoming"
    staging_dir.mkdir()
    (staging_dir / "new-entity.md").write_text("new content", encoding="utf-8")

    storage.publish_compiled_truth(staging_dir)

    assert (settings.compiled_truth_dir / "new-entity.md").read_text(encoding="utf-8") == "new content"
    assert not (settings.compiled_truth_dir / "old-entity.md").exists()
    leftovers = [
        p.name for p in settings.home.iterdir()
        if p.name.startswith("truth-old-") or p.name.startswith("truth-new-")
    ]
    assert leftovers == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_ensure_dirs_recursively_migrates_compiled_truth_perms(tmp_path, monkeypatch):
    """Blocker #4: existing (not just newly written) files nested inside
    compiled-truth must be migrated to safe perms, recursively."""
    settings = _isolate(tmp_path, monkeypatch)

    nested_dir = settings.compiled_truth_dir / "legacy-subdir"
    nested_dir.mkdir(parents=True, exist_ok=True)
    nested_file = nested_dir / "legacy.md"
    nested_file.write_text("legacy content", encoding="utf-8")
    top_file = settings.compiled_truth_dir / "top.md"
    top_file.write_text("top content", encoding="utf-8")

    # Simulate pre-hardening loose permissions on already-existing files.
    nested_file.chmod(0o644)
    nested_dir.chmod(0o755)
    top_file.chmod(0o644)

    settings.ensure_dirs()

    assert (nested_file.stat().st_mode & 0o777) == 0o600
    assert (nested_dir.stat().st_mode & 0o777) == 0o700
    assert (top_file.stat().st_mode & 0o777) == 0o600
