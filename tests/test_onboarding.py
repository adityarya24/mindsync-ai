"""Tests for safe MindSync setup and doctor behavior."""

from __future__ import annotations

import json
from pathlib import Path

import mindsync.config as config_mod
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
    settings.ensure_dirs()
    return settings


def test_registration_commands_identify_caller_and_use_module_entrypoint():
    for cli in ("codex", "claude", "gemini", "grok"):
        args = onboarding.registration_args(cli, "python-test")
        assert "mindsync" in args
        assert f"MINDSYNC_CALLER_CLI={cli}" in args
        assert args[-3:] == ["python-test", "-m", "mindsync.server"]


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
    assert not any(args[:2] == ["mcp", "add"] for _, args in runner.calls)


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
    assert [item["action"] for item in first["actions"]] == ["configured", "configured"]
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


def test_setup_stops_when_list_command_fails(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)

    def failed_list(resolved: str, args: list[str]) -> onboarding.CommandResult:
        return onboarding.CommandResult(1, stderr="cannot inspect config")

    result = onboarding.setup(
        cli_names=["codex"],
        runner=failed_list,
        resolver=_resolver,
        policy_file=settings.orchestration_file,
    )
    assert result["ok"] is False
    assert result["actions"] == [
        {"cli": "codex", "action": "error", "detail": "cannot inspect config"}
    ]


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
