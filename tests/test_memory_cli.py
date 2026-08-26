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


def test_memory_recall_cli_passes_explicit_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    captured: dict[str, object] = {}

    def fake_recall(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"matches": [{"text": "local memory"}]}

    monkeypatch.setattr(memory_mod, "memory_recall", fake_recall)
    assert manage_main(
        [
            "memory",
            "recall",
            "--project",
            "alpha",
            "--query",
            "database",
            "--limit",
            "3",
            "--min-similarity",
            "0.7",
            "--model",
            "embed-local",
        ]
    ) == 0
    assert captured == {
        "project_key": "alpha",
        "query": "database",
        "limit": 3,
        "min_similarity": 0.7,
        "model": "embed-local",
    }
    assert json.loads(capsys.readouterr().out)["matches"][0]["text"] == "local memory"


def test_memory_consolidation_cli_is_preview_first_and_explicit_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    calls: list[tuple[str, object]] = []

    def fake_preview(**kwargs: object) -> dict[str, object]:
        calls.append(("preview", kwargs))
        return {"proposal_id": "a" * 32, "status": "pending"}

    def fake_apply(proposal_id: str) -> dict[str, object]:
        calls.append(("apply", proposal_id))
        return {"proposal_id": proposal_id, "status": "applied"}

    monkeypatch.setattr(memory_mod, "memory_consolidate_preview", fake_preview)
    monkeypatch.setattr(memory_mod, "memory_consolidation_apply", fake_apply)

    assert manage_main(["memory", "consolidate", "--project", "alpha"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "pending"
    assert calls[0][0] == "preview"

    assert manage_main(["memory", "apply", "a" * 32]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert calls[1] == ("apply", "a" * 32)


def test_memory_proposals_cli_lists_review_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    captured: dict[str, object] = {}

    def fake_list(**kwargs: object) -> list[dict[str, object]]:
        captured.update(kwargs)
        return [{"proposal_id": "a" * 32, "status": "pending"}]

    monkeypatch.setattr(memory_mod, "memory_consolidation_list", fake_list)
    assert manage_main(
        [
            "memory",
            "proposals",
            "--project",
            "alpha",
            "--status",
            "pending",
            "--limit",
            "7",
        ]
    ) == 0
    assert captured == {"project_key": "alpha", "status": "pending", "limit": 7}
    assert json.loads(capsys.readouterr().out)[0]["status"] == "pending"


def test_memory_cli_surfaces_runtime_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(
        memory_mod,
        "memory_recall",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("sqlite-vec unavailable")),
    )

    assert manage_main(
        ["memory", "recall", "--project", "alpha", "--query", "cue"]
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error: sqlite-vec unavailable" in captured.err
