"""Tests for opt-in job completion sink delivery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mindsync.bus.models import Event, EventType
from mindsync.dispatch import store
from mindsync.dispatch.publish import _CONTEXT_END, _CONTEXT_START
from mindsync.dispatch.sink import (
    _delivered_path,
    _outbox_path,
    deliver_completion_event,
    drain_completion_outbox,
    public_completion_projection,
)
from mindsync.orchestration import OrchestrationPolicy, save_policy
from tests.isolation_helpers import isolate_mindsync_home


def _sink_script(tmp_path: Path) -> Path:
    script = tmp_path / "mock_sink.py"
    script.write_text(
        "import json, os, sys\n"
        "data = json.loads(sys.stdin.read())\n"
        "out = os.environ['MOCK_SINK_OUT']\n"
        "with open(out, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(data) + '\\n')\n",
        encoding="utf-8",
    )
    return script


def test_sink_success_is_idempotent_and_omits_private_fields(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    out_file = tmp_path / "sink.jsonl"
    monkeypatch.setenv("MOCK_SINK_OUT", str(out_file))
    save_policy(
        OrchestrationPolicy(
            completionSinkCmd=[sys.executable, str(_sink_script(tmp_path))]
        )
    )
    meta = store.create_job(
        agent="test",
        prompt="implement the public task",
        cwd=str(tmp_path),
        task_prompt="implement the public task",
    )
    store.update_job(
        meta["id"],
        {
            "status": "done",
            "pullRequest": {"url": "https://github.com/o/r/pull/9"},
            "stdout": "TOP_SECRET_STDOUT",
            "stderr": "TOP_SECRET_STDERR",
        },
    )
    event = Event(
        event_type=EventType.JOB_COMPLETED,
        agent_name="dispatch",
        event_id="aa" * 16,
        payload={"job_id": meta["id"], "status": "done"},
    )

    deliver_completion_event(event)
    deliver_completion_event(event)

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    delivered = json.loads(lines[0])
    assert delivered["event_id"] == "aa" * 16
    assert delivered["job_id"] == meta["id"]
    assert delivered["status"] == "done"
    assert delivered["public_task_summary"] == "implement the public task"
    assert delivered["pr_url"] == "https://github.com/o/r/pull/9"
    assert "TOP_SECRET" not in json.dumps(delivered)
    assert set(delivered) <= {
        "event_id",
        "job_id",
        "status",
        "summary",
        "public_task_summary",
        "pr_url",
    }


def test_sink_failure_does_not_mark_delivered(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    fail = tmp_path / "fail.py"
    fail.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    save_policy(OrchestrationPolicy(completionSinkCmd=[sys.executable, str(fail)]))
    event = Event(
        event_type=EventType.JOB_FAILED,
        agent_name="dispatch",
        event_id="bb" * 16,
        payload={"job_id": "no-such-job", "status": "failed"},
    )

    deliver_completion_event(event)

    assert not _delivered_path().is_file()
    outbox = json.loads(_outbox_path().read_text(encoding="utf-8"))
    assert outbox[0]["event_id"] == "bb" * 16


def test_sink_timeout_degrades_without_raising(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    save_policy(OrchestrationPolicy(completionSinkCmd=[sys.executable, "-c", "pass"]))
    event = Event(
        event_type=EventType.JOB_COMPLETED,
        agent_name="dispatch",
        event_id="cc" * 16,
        payload={"job_id": "x", "status": "done"},
    )

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 5.0))

    monkeypatch.setattr(subprocess, "run", boom)
    deliver_completion_event(event)
    assert not _delivered_path().is_file()
    assert json.loads(_outbox_path().read_text(encoding="utf-8"))[0]["event_id"] == "cc" * 16


def test_malformed_sink_config_is_a_noop(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    from mindsync.orchestration import policy_path

    policy_path().write_text(
        json.dumps({"mode": "auto", "completionSinkCmd": "echo hi"}) + "\n",
        encoding="utf-8",
    )
    event = Event(
        event_type=EventType.JOB_COMPLETED,
        agent_name="dispatch",
        event_id="dd" * 16,
        payload={"job_id": "x", "status": "done"},
    )
    deliver_completion_event(event)
    assert not _delivered_path().is_file()


def test_empty_sink_config_leaves_behavior_unchanged(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    event = Event(
        event_type=EventType.JOB_COMPLETED,
        agent_name="dispatch",
        event_id="ee" * 16,
        payload={"job_id": "x", "status": "done"},
    )
    deliver_completion_event(event)
    assert not _delivered_path().is_file()


def test_projection_strips_injected_memory_and_never_copies_logs():
    prompt = (
        f"{_CONTEXT_START}\nSECRET_CHECKPOINT_BODY\n{_CONTEXT_END}\n"
        "fix the flaky test"
    )
    event = Event(
        event_type=EventType.JOB_COMPLETED,
        agent_name="dispatch",
        event_id="ff" * 16,
        payload={"job_id": "20260101-abc", "status": "done", "stdout": "SECRET_STDOUT"},
    )
    meta = {
        "id": "20260101-abc",
        "status": "done",
        "taskPrompt": prompt,
        "prompt": prompt,
        "stdout": "SECRET_STDOUT",
        "stderr": "SECRET_STDERR",
    }
    projection = public_completion_projection(event, meta)
    blob = json.dumps(projection)
    assert "SECRET_" not in blob
    assert projection["public_task_summary"] == "fix the flaky test"
    assert "stdout" not in projection


def test_failed_sink_resends_once_on_drain_after_restart(tmp_path, monkeypatch):
    isolate_mindsync_home(tmp_path, monkeypatch, dispatch_home=True)
    fail = tmp_path / "fail.py"
    fail.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    save_policy(OrchestrationPolicy(completionSinkCmd=[sys.executable, str(fail)]))
    event = Event(
        event_type=EventType.JOB_FAILED,
        agent_name="dispatch",
        event_id="11" * 16,
        payload={"job_id": "no-such-job", "status": "failed"},
    )
    deliver_completion_event(event)
    assert not _delivered_path().is_file()

    out_file = tmp_path / "sink.jsonl"
    monkeypatch.setenv("MOCK_SINK_OUT", str(out_file))
    save_policy(
        OrchestrationPolicy(
            completionSinkCmd=[sys.executable, str(_sink_script(tmp_path))]
        )
    )
    drain_completion_outbox()
    drain_completion_outbox()

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "11" * 16
    assert json.loads(_delivered_path().read_text(encoding="utf-8")) == ["11" * 16]
    assert json.loads(_outbox_path().read_text(encoding="utf-8")) == []
