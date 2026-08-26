"""Tests for the native OpenAI Codex hook adapter (Phase 3B)."""

from __future__ import annotations

import inspect
import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mindsync import codex_hook

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_CONFIG = REPO_ROOT / ".codex" / "hooks.json"

# Strings that must never leave this hook: Codex sends them, the lifecycle must
# never see them, and neither stdout nor stderr may echo them back.
CANARIES = {
    "transcript_path": "/tmp/CANARY-TRANSCRIPT-PATH/session.jsonl",
    "last_assistant_message": "CANARY-ASSISTANT-MESSAGE",
    "model": "CANARY-MODEL",
    "prompt": "CANARY-PROMPT",
    "tool_output": "CANARY-TOOL-OUTPUT",
    "turn_id": "CANARY-TURN",
    "stop_hook_active": True,
    "reason": "CANARY-REASON",
    "totally_unknown_key": "CANARY-UNKNOWN",
}


class _FakeStdin:
    """Minimal stand-in for sys.stdin exposing a bytes buffer."""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> str:
        return self.buffer.read(size).decode("utf-8", errors="replace")


class FakeLifecycle:
    """Records core calls so tests can assert exactly what crossed the boundary."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.start_result: object = {"context": "MINDSYNC-CONTEXT", "warnings": []}
        self.errors: dict[str, Exception] = {}

    def _record(self, name: str, args: tuple, kwargs: dict):
        self.calls.append((name, args, kwargs))
        if name in self.errors:
            raise self.errors[name]

    def start_standalone_session(self, *args, **kwargs):
        self._record("start", args, kwargs)
        return self.start_result

    def checkpoint_standalone_session(self, *args, **kwargs):
        self._record("checkpoint", args, kwargs)

    def end_standalone_session(self, *args, **kwargs):
        self._record("end", args, kwargs)

    def named(self, name: str) -> list[tuple[tuple, dict]]:
        return [(args, kwargs) for call, args, kwargs in self.calls if call == name]


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycle:
    fake = FakeLifecycle()
    monkeypatch.setattr(codex_hook, "_lifecycle", lambda: fake)
    monkeypatch.delenv(codex_hook._MEMORY_MODE_ENV, raising=False)
    return fake


def run_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict | None = None,
    *,
    raw: bytes | None = None,
) -> tuple[int, str, str]:
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", _FakeStdin(data))
    code = codex_hook.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def payload_for(event: str, session_dir: Path | str = "/workspace", **extra) -> dict:
    """A realistic Codex payload: allowlisted keys plus every canary key."""
    body = {
        "session_id": "codex-session-1",
        "cwd": str(session_dir),
        "hook_event_name": event,
        **CANARIES,
    }
    body.update(extra)
    return body


def assert_no_canaries(*blobs: str) -> None:
    for blob in blobs:
        for value in CANARIES.values():
            if isinstance(value, str):
                assert value not in blob, f"canary {value!r} leaked into {blob!r}"


# --------------------------------------------------------------------------
# SessionStart
# --------------------------------------------------------------------------


def test_session_start_returns_codex_additional_context(monkeypatch, capsys, lifecycle):
    code, out, err = run_hook(
        monkeypatch, capsys, payload_for("SessionStart", source="startup")
    )

    assert code == 0
    assert err == ""
    parsed = json.loads(out)
    assert parsed == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "MINDSYNC-CONTEXT",
        }
    }

    (args, kwargs), = lifecycle.named("start")
    assert args == ("codex", "codex-session-1", "/workspace")
    assert kwargs == {
        "source": "startup",
        "memory_mode": "auto",
        "memory_project": None,
    }


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_session_start_passes_every_documented_source(
    monkeypatch, capsys, lifecycle, source
):
    run_hook(monkeypatch, capsys, payload_for("SessionStart", source=source))
    (_, kwargs), = lifecycle.named("start")
    assert kwargs["source"] == source


def test_session_start_drops_unrecognized_source(monkeypatch, capsys, lifecycle):
    code, out, err = run_hook(
        monkeypatch, capsys, payload_for("SessionStart", source="CANARY-MODEL")
    )

    assert code == 0
    (_, kwargs), = lifecycle.named("start")
    assert "source" not in kwargs  # the core's own default applies
    assert "unrecognized SessionStart source" in err
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_start_omits_output_when_core_returns_no_context(
    monkeypatch, capsys, lifecycle
):
    lifecycle.start_result = {"context": None, "warnings": []}
    code, out, err = run_hook(monkeypatch, capsys, payload_for("SessionStart"))

    assert (code, out, err) == (0, "", "")


def test_session_start_accepts_object_style_result(monkeypatch, capsys, lifecycle):
    class Result:
        context = "OBJECT-CONTEXT"
        warnings = ["object warning"]

    lifecycle.start_result = Result()
    code, out, err = run_hook(monkeypatch, capsys, payload_for("SessionStart"))

    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "OBJECT-CONTEXT"
    assert "object warning" in err


def test_session_start_truncates_oversized_context(monkeypatch, capsys, lifecycle):
    lifecycle.start_result = {"context": "x" * 50_000, "warnings": []}
    code, out, err = run_hook(monkeypatch, capsys, payload_for("SessionStart"))

    assert code == 0
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert len(context) == codex_hook._MAX_CONTEXT_CHARS
    assert context.endswith(codex_hook._TRUNCATION_MARKER)
    assert "truncated" in err


def test_session_start_missing_session_id_is_non_fatal(monkeypatch, capsys, lifecycle):
    body = payload_for("SessionStart")
    del body["session_id"]
    code, out, err = run_hook(monkeypatch, capsys, body)

    assert code == 0
    assert out == ""
    assert lifecycle.calls == []
    assert "no session_id" in err


# --------------------------------------------------------------------------
# Memory mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [(None, "auto"), ("auto", "auto"), ("explicit", "explicit"), ("off", "off"),
     ("  OFF  ", "off")],
)
def test_memory_mode_comes_from_env_with_auto_default(
    monkeypatch, capsys, lifecycle, env_value, expected
):
    if env_value is not None:
        monkeypatch.setenv(codex_hook._MEMORY_MODE_ENV, env_value)
    run_hook(monkeypatch, capsys, payload_for("SessionStart"))

    (_, kwargs), = lifecycle.named("start")
    assert kwargs["memory_mode"] == expected


def test_unknown_memory_mode_fails_closed(monkeypatch, capsys, lifecycle):
    monkeypatch.setenv(codex_hook._MEMORY_MODE_ENV, "of")
    code, _, err = run_hook(monkeypatch, capsys, payload_for("SessionStart"))

    assert code == 0
    (_, kwargs), = lifecycle.named("start")
    assert kwargs["memory_mode"] == "off"
    assert "session memory disabled" in err


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["SessionStart", "Stop", "SessionEnd"])
def test_no_payload_canary_reaches_core_or_output(monkeypatch, capsys, lifecycle, event):
    code, out, err = run_hook(monkeypatch, capsys, payload_for(event))

    assert code == 0
    assert_no_canaries(out, err, repr(lifecycle.calls))


def test_hook_calls_bind_to_the_real_core_signatures(monkeypatch, capsys, lifecycle):
    """Every call this hook makes must still fit the public lifecycle API."""
    core = pytest.importorskip("mindsync.standalone_lifecycle")
    functions = {
        "start": core.start_standalone_session,
        "checkpoint": core.checkpoint_standalone_session,
        "end": core.end_standalone_session,
    }
    for event in ("SessionStart", "Stop", "SessionEnd"):
        run_hook(monkeypatch, capsys, payload_for(event, source="resume"))

    assert {name for name, _, _ in lifecycle.calls} == set(functions)
    for name, args, kwargs in lifecycle.calls:
        inspect.signature(functions[name]).bind(*args, **kwargs)


def test_allowlist_keeps_only_four_fields():
    fields = codex_hook._allowlisted_fields(payload_for("Stop"))

    assert set(fields) == {"session_id", "cwd", "hook_event_name", "source"}
    assert_no_canaries(repr(fields))


def test_allowlist_drops_non_string_values():
    fields = codex_hook._allowlisted_fields(
        {"session_id": 42, "cwd": ["/x"], "hook_event_name": "Stop", "source": None}
    )

    assert fields == {
        "session_id": None,
        "cwd": None,
        "hook_event_name": "Stop",
        "source": None,
    }


def test_allowlist_bounds_and_flattens_values():
    fields = codex_hook._allowlisted_fields(
        {"session_id": "a" * 5_000, "hook_event_name": "Ses\nsion\tStart"}
    )

    assert len(fields["session_id"]) == codex_hook._MAX_SESSION_ID_CHARS
    assert "\n" not in fields["hook_event_name"]


# --------------------------------------------------------------------------
# Stop / changed files
# --------------------------------------------------------------------------


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


requires_git = pytest.mark.skipif(not _git_available(), reason="git is not installed")


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "hook@test.invalid"],
        ["config", "user.name", "Hook Test"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "seed.txt"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    return repo


def test_stop_always_emits_continue_json(monkeypatch, capsys, lifecycle):
    code, out, err = run_hook(monkeypatch, capsys, payload_for("Stop"))

    assert code == 0
    assert err == ""
    assert json.loads(out) == {"continue": True}
    (args, kwargs), = lifecycle.named("checkpoint")
    assert args == ("codex", "codex-session-1")
    assert kwargs == {"status": "active", "files_changed": None}


@requires_git
def test_stop_reports_repo_relative_changed_files(monkeypatch, capsys, lifecycle, tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
    nested = repo / "pkg"
    nested.mkdir()
    (nested / "new.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "pkg/new.py"], check=True, capture_output=True
    )

    code, out, _ = run_hook(monkeypatch, capsys, payload_for("Stop", repo))

    assert code == 0
    assert json.loads(out) == {"continue": True}
    (_, kwargs), = lifecycle.named("checkpoint")
    files = kwargs["files_changed"]
    assert sorted(files) == ["pkg/new.py", "seed.txt"]
    assert all(not Path(f).is_absolute() for f in files)


@requires_git
def test_stop_handles_renames_by_recording_the_new_path(
    monkeypatch, capsys, lifecycle, tmp_path
):
    repo = _init_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "mv", "seed.txt", "renamed.txt"],
        check=True,
        capture_output=True,
    )

    run_hook(monkeypatch, capsys, payload_for("Stop", repo))

    (_, kwargs), = lifecycle.named("checkpoint")
    assert kwargs["files_changed"] == ["renamed.txt"]


@requires_git
def test_stop_handles_awkward_filenames(monkeypatch, capsys, lifecycle, tmp_path):
    repo = _init_repo(tmp_path)
    names = ["a file with spaces.txt", "quote'name.txt", "üñí-çodé.txt", "dash-lead.txt"]
    if sys.platform != "win32":
        names += ['quo"te.txt', "new\nline.txt", "star*name.txt"]
    for name in names:
        (repo / name).write_text("x\n", encoding="utf-8")

    run_hook(monkeypatch, capsys, payload_for("Stop", repo))

    (_, kwargs), = lifecycle.named("checkpoint")
    files = kwargs["files_changed"]
    assert sorted(files) == sorted(names)
    # -z output is raw, so nothing arrives C-quoted.
    assert not any(f.startswith('"') for f in files)


@requires_git
def test_stop_caps_changed_files_at_fifty(monkeypatch, capsys, lifecycle, tmp_path):
    repo = _init_repo(tmp_path)
    for index in range(75):
        (repo / f"file-{index:03d}.txt").write_text("x\n", encoding="utf-8")

    run_hook(monkeypatch, capsys, payload_for("Stop", repo))

    (_, kwargs), = lifecycle.named("checkpoint")
    assert len(kwargs["files_changed"]) == codex_hook._MAX_FILES


def test_stop_outside_a_repo_is_non_fatal(monkeypatch, capsys, lifecycle, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    code, out, err = run_hook(monkeypatch, capsys, payload_for("Stop", plain))

    assert code == 0
    assert json.loads(out) == {"continue": True}
    assert err == ""
    (_, kwargs), = lifecycle.named("checkpoint")
    assert kwargs["files_changed"] is None


def test_stop_with_missing_git_binary_is_non_fatal(monkeypatch, capsys, lifecycle, tmp_path):
    def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(codex_hook.subprocess, "run", boom)
    code, out, _ = run_hook(monkeypatch, capsys, payload_for("Stop", tmp_path))

    assert code == 0
    assert json.loads(out) == {"continue": True}
    (_, kwargs), = lifecycle.named("checkpoint")
    assert kwargs["files_changed"] is None


def test_stop_with_nonexistent_cwd_skips_git(monkeypatch, capsys, lifecycle, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("git must not run for a missing directory")

    monkeypatch.setattr(codex_hook.subprocess, "run", boom)
    code, out, _ = run_hook(
        monkeypatch, capsys, payload_for("Stop", tmp_path / "gone")
    )

    assert code == 0
    assert json.loads(out) == {"continue": True}


def test_git_is_invoked_by_argv_without_a_shell(monkeypatch, capsys, lifecycle, tmp_path):
    seen: list[dict] = []

    class Result:
        returncode = 0
        stdout = "true\n"

    def fake_run(args, **kwargs):
        seen.append({"args": args, **kwargs})
        return Result()

    monkeypatch.setattr(codex_hook.subprocess, "run", fake_run)
    run_hook(monkeypatch, capsys, payload_for("Stop", tmp_path))

    assert seen, "expected git to be probed"
    for call in seen:
        assert isinstance(call["args"], list)
        assert call["args"][0] == "git"
        assert all(isinstance(part, str) for part in call["args"])
        assert call.get("shell", False) is False
        assert call["timeout"] == codex_hook._GIT_TIMEOUT_SECONDS
        assert call["stdin"] is subprocess.DEVNULL
        for name in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
            assert name not in call["env"]


def test_git_status_failure_warns_but_checkpoints(monkeypatch, capsys, lifecycle, tmp_path):
    class Result:
        def __init__(self, code: int, out: str) -> None:
            self.returncode = code
            self.stdout = out

    def fake_run(args, **kwargs):
        if "rev-parse" in args:
            return Result(0, "true\n")
        return Result(128, "")

    monkeypatch.setattr(codex_hook.subprocess, "run", fake_run)
    code, out, err = run_hook(monkeypatch, capsys, payload_for("Stop", tmp_path))

    assert code == 0
    assert json.loads(out) == {"continue": True}
    assert "changed-file detection degraded" in err
    (_, kwargs), = lifecycle.named("checkpoint")
    assert kwargs["files_changed"] is None


def test_porcelain_parser_rejects_escaping_paths():
    parsed = codex_hook._parse_porcelain_z(
        "M  ../outside.txt\0M  /etc/passwd\0M  ok.txt\0"
    )

    assert parsed == ["ok.txt"]


def test_porcelain_parser_deduplicates_and_skips_garbage():
    parsed = codex_hook._parse_porcelain_z("M  a.txt\0\0xx\0M  a.txt\0?? b.txt\0")

    assert parsed == ["a.txt", "b.txt"]


# --------------------------------------------------------------------------
# SessionEnd
# --------------------------------------------------------------------------


def test_session_end_closes_the_session_quietly(monkeypatch, capsys, lifecycle):
    code, out, err = run_hook(monkeypatch, capsys, payload_for("SessionEnd"))

    assert (code, out, err) == (0, "", "")
    (args, kwargs), = lifecycle.named("end")
    assert args == ("codex", "codex-session-1")
    assert kwargs == {"status": "completed"}


def test_session_end_does_no_subprocess_work_and_is_fast(
    monkeypatch, capsys, lifecycle, tmp_path
):
    def boom(*args, **kwargs):
        raise AssertionError("SessionEnd must not shell out")

    monkeypatch.setattr(codex_hook.subprocess, "run", boom)
    start = time.perf_counter()
    code, _, _ = run_hook(monkeypatch, capsys, payload_for("SessionEnd", tmp_path))
    elapsed = time.perf_counter() - start

    assert code == 0
    assert elapsed < 3.0
    assert len(lifecycle.named("end")) == 1


def test_session_end_backend_failure_is_non_fatal(monkeypatch, capsys, lifecycle):
    lifecycle.errors["end"] = RuntimeError("db locked")
    code, out, err = run_hook(monkeypatch, capsys, payload_for("SessionEnd"))

    assert code == 0
    assert out == ""
    assert "session memory end degraded" in err


# --------------------------------------------------------------------------
# Malformed input and backend failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [b"", b"   ", b"not json at all", b"[]", b'"a string"', b"null", b"{"],
)
def test_malformed_payloads_exit_zero_with_one_warning(
    monkeypatch, capsys, lifecycle, raw
):
    code, out, err = run_hook(monkeypatch, capsys, raw=raw)

    assert code == 0
    assert out == ""
    assert len(err.splitlines()) == 1
    assert lifecycle.calls == []


def test_oversized_stdin_is_rejected(monkeypatch, capsys, lifecycle):
    body = json.dumps(payload_for("SessionStart", pad="A" * (2 << 20))).encode("utf-8")
    assert len(body) > codex_hook._MAX_STDIN_BYTES

    code, out, err = run_hook(monkeypatch, capsys, raw=body)

    assert code == 0
    assert out == ""
    assert "exceeded" in err
    assert lifecycle.calls == []


def test_unknown_event_is_ignored(monkeypatch, capsys, lifecycle):
    code, out, err = run_hook(monkeypatch, capsys, payload_for("PreToolUse"))

    assert code == 0
    assert out == ""
    assert "unsupported hook event" in err
    assert lifecycle.calls == []


def test_missing_event_name_is_ignored(monkeypatch, capsys, lifecycle):
    body = payload_for("Stop")
    del body["hook_event_name"]
    code, out, err = run_hook(monkeypatch, capsys, body)

    assert code == 0
    assert "unsupported hook event" in err
    assert lifecycle.calls == []


@pytest.mark.parametrize(
    ("event", "call", "expected_stdout"),
    [
        ("SessionStart", "start", ""),
        ("Stop", "checkpoint", '{"continue": true}'),
        ("SessionEnd", "end", ""),
    ],
)
def test_backend_errors_exit_zero_with_event_valid_stdout(
    monkeypatch, capsys, lifecycle, event, call, expected_stdout
):
    lifecycle.errors[call] = RuntimeError("backend exploded")
    code, out, err = run_hook(monkeypatch, capsys, payload_for(event))

    assert code == 0
    assert out.strip() == expected_stdout
    assert "degraded" in err
    assert len(err.splitlines()) == 1


def test_missing_core_module_degrades(monkeypatch, capsys):
    def missing():
        raise ImportError("No module named 'mindsync.standalone_lifecycle'")

    monkeypatch.setattr(codex_hook, "_lifecycle", missing)
    code, out, err = run_hook(monkeypatch, capsys, payload_for("Stop"))

    assert code == 0
    assert json.loads(out) == {"continue": True}
    assert "degraded" in err


def test_unexpected_handler_crash_still_answers_stop(monkeypatch, capsys, lifecycle):
    def explode(fields, warnings):
        raise KeyError("unexpected")

    monkeypatch.setitem(codex_hook._HANDLERS, "Stop", explode)
    code, out, err = run_hook(monkeypatch, capsys, payload_for("Stop"))

    assert code == 0
    assert json.loads(out) == {"continue": True}
    assert "hook degraded" in err


def test_warnings_are_one_bounded_stderr_line(monkeypatch, capsys, lifecycle):
    lifecycle.start_result = {
        "context": "CTX",
        "warnings": ["w" * 400, "second\nwarning\r\nwith newlines", "third"],
    }
    code, out, err = run_hook(monkeypatch, capsys, payload_for("SessionStart"))

    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "CTX"
    assert len(err.splitlines()) == 1
    assert len(err) <= codex_hook._MAX_WARNING_CHARS + 40
    assert err.startswith("mindsync-codex-hook: ")


def test_malformed_core_warnings_are_ignored(monkeypatch, capsys, lifecycle):
    lifecycle.start_result = {"context": "CTX", "warnings": "not-a-list"}
    code, out, err = run_hook(monkeypatch, capsys, payload_for("SessionStart"))

    assert code == 0
    assert err == ""
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "CTX"


def test_oversized_stdout_is_dropped_rather_than_truncated(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"{}"))
    codex_hook._emit_stdout("x" * (codex_hook._MAX_STDOUT_BYTES + 1))

    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# Repo-local Codex config
# --------------------------------------------------------------------------


def test_hooks_config_registers_exactly_the_three_events():
    config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))

    assert set(config["hooks"]) == {"SessionStart", "Stop", "SessionEnd"}


def test_hooks_config_uses_the_console_script_on_every_platform():
    config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))

    for matchers in config["hooks"].values():
        for matcher in matchers:
            for hook in matcher["hooks"]:
                assert hook["type"] == "command"
                assert hook["command"] == "mindsync-codex-hook"
                assert hook["commandWindows"] == "mindsync-codex-hook"
                assert hook["timeout"] == 3


def test_hooks_config_session_start_matcher_and_context_limit():
    config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
    matcher, = config["hooks"]["SessionStart"]

    assert matcher["matcher"] == "startup|resume|clear|compact"
    hook, = matcher["hooks"]
    assert hook["additionalContextLimit"] == 8000
    assert hook["additionalContextLimit"] == codex_hook._MAX_CONTEXT_CHARS


def test_hooks_config_stays_repo_local():
    """The repo config must not reach into a user's ~/.codex setup."""
    text = HOOKS_CONFIG.read_text(encoding="utf-8")

    assert "notify" not in text
    assert "config.toml" not in text
    assert "~" not in text
