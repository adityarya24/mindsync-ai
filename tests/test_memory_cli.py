"""CLI tests for `mindsync memory` inspection and prune commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.memory as memory_mod
from mindsync.manage import main as manage_main
from mindsync.memory import (
    _close_local_db,
    memory_checkpoint,
    session_end,
    session_start,
)


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _close_local_db()
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    memory_mod.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    yield home
    _close_local_db()


def _seed_project(project_key: str) -> tuple[str, str]:
    ended = session_start(project_key=project_key, agent="agent-a")
    memory_checkpoint(ended, decisions=["seed decision"])
    session_end(ended)
    active = session_start(project_key=project_key, agent="agent-b")
    return ended, active


def test_memory_stats_json(capsys: pytest.CaptureFixture[str]):
    _seed_project("cli-stats")
    assert manage_main(["memory", "stats", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_sessions"] == 2
    assert report["active_sessions"] == 1
    assert report["projects"][0]["project_key"] == "cli-stats"


def test_memory_stats_human_readable(capsys: pytest.CaptureFixture[str]):
    _seed_project("cli-stats-human")
    assert manage_main(["memory", "stats"]) == 0
    out = capsys.readouterr().out
    assert "2 sessions (1 active)" in out
    assert "cli-stats-human" in out


def test_memory_list_and_show(capsys: pytest.CaptureFixture[str]):
    ended, active = _seed_project("cli-list")
    assert manage_main(["memory", "list", "--project", "cli-list"]) == 0
    out = capsys.readouterr().out
    assert active in out
    assert ended in out

    assert manage_main(["memory", "show", ended]) == 0
    shown = capsys.readouterr().out
    assert "seed decision" in shown

    assert manage_main(["memory", "show", ended, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == ended
    assert payload["checkpoints"][0]["decisions"] == ["seed decision"]


def test_memory_list_json(capsys: pytest.CaptureFixture[str]):
    _seed_project("cli-list-json")
    assert manage_main(["memory", "list", "--limit", "1", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 1
    assert entries[0]["project_key"] == "cli-list-json"


def test_memory_prune_dry_run_is_default_and_yes_deletes(
    capsys: pytest.CaptureFixture[str],
):
    ended, active = _seed_project("cli-prune")

    assert manage_main(["memory", "prune", "--project", "cli-prune"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert ended in out
    assert memory_mod.memory_stats()["total_sessions"] == 2

    assert manage_main(["memory", "prune", "--project", "cli-prune", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" not in out
    report = memory_mod.memory_stats()
    assert report["total_sessions"] == 1


def test_memory_prune_json_output(capsys: pytest.CaptureFixture[str]):
    ended, _active = _seed_project("cli-prune-json")
    assert manage_main(["memory", "prune", "--project", "cli-prune-json", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["candidates"] == 1
    assert result["session_ids"] == [ended]


def test_memory_show_unknown_session_returns_error(
    capsys: pytest.CaptureFixture[str],
):
    assert manage_main(["memory", "show", "0" * 32]) == 2
    err = capsys.readouterr().err
    assert "Unknown session" in err


def test_memory_prune_rejects_bad_age(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as excinfo:
        manage_main(["memory", "prune", "--older-than-days", "0"])
    assert excinfo.value.code == 2
