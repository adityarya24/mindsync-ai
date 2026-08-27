"""Tests for safe MindSync setup and doctor behavior."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import mindsync.config as config_mod
import mindsync.memory as memory_mod
import mindsync.onboarding as onboarding
import mindsync.orchestration as orchestration
import mindsync.storage as storage


class FakeCliRunner:
    def __init__(self) -> None:
        self.configured: set[str] = set()
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, resolved: str, args: list[str]) -> onboarding.CommandResult:
        name = Path(resolved).name
        self.calls.append((name, list(args)))
        if args[:2] == ["mcp", "list"]:
            if "--json" in args:
                return onboarding.CommandResult(
                    0,
                    json.dumps([{"name": item} for item in sorted(self.configured)]),
                )
            return onboarding.CommandResult(0, "\n".join(sorted(self.configured)))
        if args[:2] == ["mcp", "remove"]:
            self.configured.discard("mindsync")
            return onboarding.CommandResult(0)
        if args[:2] == ["mcp", "add"]:
            self.configured.add("mindsync")
            return onboarding.CommandResult(0)
        return onboarding.CommandResult(1, stderr=f"unexpected call for {name}: {args}")


def _resolver(name: str) -> str:
    return str(Path("fake-bin") / name)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDSYNC_HOME", str(tmp_path / "mindsync-home"))
    settings = config_mod.Settings()
    config_mod.settings = settings
    storage.settings = settings
    orchestration.settings = settings
    memory_mod.settings = settings
    memory_mod._close_local_db()
    settings.ensure_dirs()
    return settings


def test_registration_commands_identify_caller_and_use_module_entrypoint():
    for cli in ("codex", "claude", "gemini", "grok"):
        args = onboarding.registration_args(cli, "python-test")
        assert "mindsync" in args
        assert f"MINDSYNC_CALLER_CLI={cli}" in args
        assert args[-3:] == ["python-test", "-m", "mindsync.server"]
    generic = onboarding.registration_args("windsurf", "python-test")
    assert generic[:4] == ["mcp", "add", "--scope", "user"]
    assert "MINDSYNC_CALLER_CLI=windsurf" in generic
    assert generic[-3:] == ["python-test", "-m", "mindsync.server"]


def test_colored_cli_output_is_detected_and_pending_approval_is_reported():
    def colored_list(resolved: str, args: list[str]) -> onboarding.CommandResult:
        return onboarding.CommandResult(
            0,
            "\x1b[33m○ mindsync: not loaded (needs approval)\x1b[0m",
        )

    status = onboarding.cli_status(
        "gemini",
        runner=colored_list,
        resolver=_resolver,
    )
    assert status["configured"] is True
    assert "approval" in status["detail"]


def test_setup_dry_run_is_non_mutating_and_reports_unsupported(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    runner = FakeCliRunner()

    result = onboarding.setup(
        mode="auto",
        cli_names=["codex", "agy"],
        dry_run=True,
        runner=runner,
        resolver=_resolver,
        user_home=tmp_path / "user",
        policy_file=settings.orchestration_file,
        python_exe="python-test",
    )

    assert not settings.orchestration_file.exists()
    assert result["actions"][0]["action"] == "would_configure"
    assert result["actions"][1]["action"] == "worker_only"
    assert "Gemini/Antigravity family" in result["actions"][1]["detail"]
    assert result["actions"][2]["action"] == "would_configure"
    assert result["actions"][2]["cli"] == "codex-hook"
    assert not (tmp_path / "user" / ".codex" / "hooks.json").exists()
    assert not any(args[:2] == ["mcp", "add"] for _, args in runner.calls)


def test_setup_writes_opencode_jsonc_without_guessing_mcp_add(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    user_home = tmp_path / "user"
    config = onboarding.opencode_config_path(user_home)
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "default_agent": "vidur",
                "mcp": {
                    "gws-marketing": {
                        "type": "local",
                        "command": ["gws"],
                        "enabled": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    runner = FakeCliRunner()
    result = onboarding.setup(
        cli_names=["opencode"],
        runner=runner,
        resolver=_resolver,
        user_home=user_home,
        policy_file=settings.orchestration_file,
        python_exe="python-test",
        install_hooks=False,
    )
    assert result["actions"][0]["action"] == "configured"
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["default_agent"] == "vidur"
    assert data["mcp"]["gws-marketing"]["command"] == ["gws"]
    mindsync = data["mcp"]["mindsync"]
    assert mindsync["type"] == "local"
    assert mindsync["command"] == ["python-test", "-m", "mindsync.server"]
    assert mindsync["environment"]["MINDSYNC_CALLER_CLI"] == "opencode"
    assert not any(args[:2] == ["mcp", "add"] for _, args in runner.calls)

    again = onboarding.setup(
        cli_names=["opencode"],
        runner=runner,
        resolver=_resolver,
        user_home=user_home,
        policy_file=settings.orchestration_file,
        python_exe="python-test",
        install_hooks=False,
    )
    assert again["actions"][0]["action"] == "already_configured"


def test_setup_registers_command_cli_and_cursor_idempotently(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    runner = FakeCliRunner()
    user_home = tmp_path / "user"

    first = onboarding.setup(
        mode="suggest",
        cli_names=["codex", "cursor"],
        runner=runner,
        resolver=_resolver,
        user_home=user_home,
        policy_file=settings.orchestration_file,
        python_exe="python-test",
    )
    assert first["ok"] is True
    assert orchestration.load_policy().mode == "suggest"
    assert [item["action"] for item in first["actions"]] == [
        "configured",
        "configured",
        "configured",
    ]
    assert first["actions"][2]["cli"] == "codex-hook"
    hooks = json.loads(Path(first["actions"][2]["path"]).read_text(encoding="utf-8"))
    assert onboarding._hooks_cover_mindsync(hooks)
    cursor_data = json.loads(
        onboarding.cursor_config_path(user_home).read_text(encoding="utf-8")
    )
    cursor_server = cursor_data["mcpServers"]["mindsync"]
    assert cursor_server["command"] == "python-test"
    assert cursor_server["env"]["MINDSYNC_CALLER_CLI"] == "cursor"

    second = onboarding.setup(
        mode="suggest",
        cli_names=["codex", "cursor"],
        runner=runner,
        resolver=_resolver,
        user_home=user_home,
        policy_file=settings.orchestration_file,
        python_exe="python-test",
    )
    assert [item["action"] for item in second["actions"]] == [
        "already_configured",
        "already_configured",
        "already_configured",
    ]


def test_cursor_force_preserves_other_servers_and_creates_backup(tmp_path):
    user_home = tmp_path / "user"
    path = onboarding.cursor_config_path(user_home)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}, "mindsync": {"command": "old"}}}),
        encoding="utf-8",
    )

    result = onboarding._write_cursor_config(
        user_home=user_home,
        force=True,
        dry_run=False,
        python_exe="new-python",
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"]["command"] == "other"
    assert data["mcpServers"]["mindsync"]["command"] == "new-python"
    assert result["backup"] and Path(result["backup"]).is_file()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_cursor_config_and_backup_are_never_left_group_or_world_readable(tmp_path):
    user_home = tmp_path / "user"
    path = onboarding.cursor_config_path(user_home)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcpServers": {"other": {"env": {"TOKEN": "secret"}}}}),
        encoding="utf-8",
    )
    path.chmod(0o600)

    result = onboarding._write_cursor_config(
        user_home=user_home,
        force=True,
        dry_run=False,
        python_exe="python",
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(result["backup"]).stat().st_mode) == 0o600


def test_setup_stops_when_list_command_fails(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)

    def failed_list(resolved: str, args: list[str]) -> onboarding.CommandResult:
        return onboarding.CommandResult(1, stderr="cannot inspect config")

    result = onboarding.setup(
        cli_names=["codex"],
        runner=failed_list,
        resolver=_resolver,
        user_home=tmp_path / "user",
        policy_file=settings.orchestration_file,
    )
    assert result["ok"] is False
    assert result["actions"][0] == {
        "cli": "codex",
        "action": "error",
        "detail": "cannot inspect config",
    }


def test_doctor_reports_hosts_policy_and_worker_inventory(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    orchestration.save_policy(orchestration.OrchestrationPolicy(mode="auto"))
    runner = FakeCliRunner()
    runner.configured.add("mindsync")

    report = onboarding.doctor(
        runner=runner,
        resolver=_resolver,
        user_home=tmp_path / "user",
        policy_file=settings.orchestration_file,
    )

    assert report["ok"] is True
    assert report["policy"]["mode"] == "auto"
    assert "codex" in report["configured_hosts"]
    assert any(worker["available"] for worker in report["workers"])
    assert report["available_worker_families"]["gemini-antigravity"] == ["agy", "gemini"]


def test_doctor_fails_when_no_host_is_configured(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    runner = FakeCliRunner()

    report = onboarding.doctor(
        runner=runner,
        resolver=_resolver,
        user_home=tmp_path / "user",
        policy_file=settings.orchestration_file,
    )
    assert report["ok"] is False
    assert "No supported CLI" in report["issues"][0]


def test_doctor_reports_invalid_cursor_json(tmp_path):
    user_home = tmp_path / "user"
    path = onboarding.cursor_config_path(user_home)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    status = onboarding.cli_status(
        "cursor",
        runner=FakeCliRunner(),
        resolver=_resolver,
        user_home=user_home,
    )
    assert status["configured"] is False
    assert "Invalid JSON" in status["detail"]


def test_setup_skips_codex_hooks_when_disabled_or_codex_missing(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    user_home = tmp_path / "user"

    skipped = onboarding.setup(
        cli_names=["cursor"],
        runner=FakeCliRunner(),
        resolver=_resolver,
        user_home=user_home,
        policy_file=settings.orchestration_file,
        python_exe="python-test",
        install_hooks=True,
    )
    assert all(item["cli"] != "codex-hook" for item in skipped["actions"])
    assert not onboarding.codex_hooks_path(user_home).exists()

    opted_out = onboarding.setup(
        cli_names=["codex"],
        runner=FakeCliRunner(),
        resolver=_resolver,
        user_home=user_home,
        policy_file=settings.orchestration_file,
        python_exe="python-test",
        install_hooks=False,
    )
    assert all(item["cli"] != "codex-hook" for item in opted_out["actions"])
    assert not onboarding.codex_hooks_path(user_home).exists()


def test_codex_hooks_merge_preserves_existing_entries(tmp_path):
    user_home = tmp_path / "user"
    path = onboarding.codex_hooks_path(user_home)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "echo keep-me"}
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = onboarding._write_codex_hooks(
        user_home=user_home, force=False, dry_run=False
    )
    assert result["action"] == "configured"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert onboarding._hooks_cover_mindsync(data)
    assert any(
        hook.get("command") == "echo keep-me"
        for group in data["hooks"]["Stop"]
        for hook in group.get("hooks", [])
    )
    assert result["backup"] and Path(result["backup"]).is_file()


def test_bundled_codex_hooks_match_repo_example():
    example = json.loads(
        (Path(__file__).resolve().parents[1] / ".codex" / "hooks.json").read_text(
            encoding="utf-8"
        )
    )
    assert example == onboarding.bundled_codex_hooks_config()


def test_doctor_reports_memory_and_missing_hooks(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    orchestration.save_policy(orchestration.OrchestrationPolicy(mode="auto"))
    runner = FakeCliRunner()
    runner.configured.add("mindsync")
    workspace = tmp_path / "not-a-git-repo"
    workspace.mkdir()

    report = onboarding.doctor(
        runner=runner,
        resolver=_resolver,
        user_home=tmp_path / "user",
        policy_file=settings.orchestration_file,
        cwd=workspace,
    )

    assert report["ok"] is True
    assert report["memory"]["db_open"] is True
    assert report["memory"]["sessions"] == 0
    assert report["memory"]["git_project"] is None
    assert report["memory"]["codex_hooks"]["configured"] is False


def test_doctor_sees_user_level_codex_hooks(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)
    user_home = tmp_path / "user"
    onboarding._write_codex_hooks(user_home=user_home, force=False, dry_run=False)

    report = onboarding.doctor(
        runner=FakeCliRunner(),
        resolver=_resolver,
        user_home=user_home,
        policy_file=settings.orchestration_file,
        cwd=tmp_path,
    )
    assert report["memory"]["codex_hooks"]["configured"] is True
    assert report["memory"]["codex_hooks"]["user_path"] == str(
        onboarding.codex_hooks_path(user_home)
    )
