"""Tests for AgentRelay Event Bus integration in MindSync."""

from pathlib import Path

import mindsync.config as config_mod
import mindsync.server as server
import mindsync.storage as storage
from mindsync.bus import (
    Event,
    EventType,
    Identity,
    Message,
    PolicyRule,
    poll_events as bus_poll_events,
    publish_event as bus_publish_event,
    subscribe as bus_subscribe,
)


def _isolate(tmp_path: Path, monkeypatch):
    home = tmp_path / "mindsync-home"
    monkeypatch.setenv("MINDSYNC_HOME", str(home))
    config_mod.settings = config_mod.Settings()
    storage.settings = config_mod.settings
    server.settings = config_mod.settings
    config_mod.settings.ensure_dirs()
    return config_mod.settings


def test_event_bus_models():
    # Verify EventType values
    assert EventType.TASK_CREATED == "task.created"
    assert EventType.JOB_STARTED == "job.started"
    assert EventType.JOB_COMPLETED == "job.completed"
    assert EventType.JOB_FAILED == "job.failed"
    assert EventType.FOCUS_CHANGED == "focus.changed"
    assert EventType.MEMORY_UPDATED == "memory.updated"
    assert EventType.CONFLICT_DETECTED == "conflict.detected"

    # Test Event model
    ev = Event(
        event_type=EventType.TASK_CREATED,
        agent_name="Satyaki",
        payload={"task_id": "123"},
        correlation_id="corr-1",
    )
    ev_dict = ev.to_dict()
    assert ev_dict["event_type"] == "task.created"
    assert ev_dict["agent_name"] == "Satyaki"
    assert ev_dict["payload"] == {"task_id": "123"}
    assert ev_dict["correlation_id"] == "corr-1"

    ev_restored = Event.from_dict(ev_dict)
    assert ev_restored.event_type == "task.created"
    assert ev_restored.agent_name == "Satyaki"
    assert ev_restored.payload == {"task_id": "123"}

    # Test Message model
    msg = Message(sender="Satyaki", recipient="Abhimanyu", content="hello")
    msg_dict = msg.to_dict()
    assert msg_dict["sender"] == "Satyaki"
    assert msg_dict["recipient"] == "Abhimanyu"
    msg_restored = Message.from_dict(msg_dict)
    assert msg_restored.sender == "Satyaki"

    # Test Identity model
    ident = Identity(agent_name="Satyaki", role="warrior")
    ident_dict = ident.to_dict()
    assert ident_dict["agent_name"] == "Satyaki"
    ident_restored = Identity.from_dict(ident_dict)
    assert ident_restored.role == "warrior"

    # Test PolicyRule model
    rule = PolicyRule(rule_id="r1", action="read", effect="allow")
    rule_dict = rule.to_dict()
    assert rule_dict["rule_id"] == "r1"
    rule_restored = PolicyRule.from_dict(rule_dict)
    assert rule_restored.action == "read"


def test_publish_poll_and_sequences(tmp_path, monkeypatch):
    settings = _isolate(tmp_path, monkeypatch)

    ev1 = bus_publish_event(
        Event(event_type=EventType.TASK_CREATED, agent_name="Satyaki", payload={"num": 1})
    )
    ev2 = bus_publish_event(
        Event(event_type=EventType.JOB_STARTED, agent_name="Satyaki", payload={"num": 2})
    )
    ev3 = bus_publish_event(
        Event(event_type=EventType.JOB_COMPLETED, agent_name="Satyaki", payload={"num": 3})
    )

    assert ev1.seq == 1
    assert ev2.seq == 2
    assert ev3.seq == 3

    assert settings.events_file.exists()

    # Poll all
    all_events = bus_poll_events(since_seq=0)
    assert len(all_events) == 3
    assert [e.seq for e in all_events] == [1, 2, 3]

    # Poll since seq 1
    since1 = bus_poll_events(since_seq=1)
    assert len(since1) == 2
    assert [e.seq for e in since1] == [2, 3]

    # Poll with event_types filter
    filtered = bus_poll_events(since_seq=0, event_types=["job.started"])
    assert len(filtered) == 1
    assert filtered[0].event_type == "job.started"

    # Poll with limit
    limited = bus_poll_events(since_seq=0, limit=2)
    assert len(limited) == 2


def test_subscribe_and_agent_polling(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    bus_subscribe("Satyaki", ["focus.changed"])

    bus_publish_event(
        Event(event_type=EventType.FOCUS_CHANGED, agent_name="Satyaki", payload={"focus": "f1"})
    )
    bus_publish_event(
        Event(event_type=EventType.MEMORY_UPDATED, agent_name="Satyaki", payload={"mem": "m1"})
    )

    # Polling for Satyaki uses subscribed event_types ("focus.changed")
    satyaki_events = bus_poll_events(agent_name="Satyaki")
    assert len(satyaki_events) == 1
    assert satyaki_events[0].event_type == "focus.changed"


def test_server_mcp_event_tools(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    # 1. Publish event via server tool
    pub_res = server.publish_event(
        agent_name="Satyaki",
        event_type="task.created",
        payload={"task": "deploy"},
        correlation_id="c123",
    )
    assert pub_res["ok"] is True
    assert pub_res["seq"] == 1
    assert pub_res["event"]["event_type"] == "task.created"

    # 2. Subscribe via server tool
    sub_res = server.subscribe_events("Satyaki", ["task.created"])
    assert sub_res["ok"] is True
    assert sub_res["event_types"] == ["task.created"]

    # 3. Poll via server tool
    poll_res = server.poll_events("Satyaki", since_seq=0)
    assert poll_res["ok"] is True
    assert poll_res["count"] == 1
    assert poll_res["events"][0]["payload"] == {"task": "deploy"}


def test_server_auto_emit_focus_changed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    res = server.update_focus("Satyaki", "proj-a", "main", "refactoring bus")
    assert res["ok"] is True

    events = bus_poll_events(since_seq=0)
    assert len(events) == 1
    assert events[0].event_type == "focus.changed"
    assert events[0].agent_name == "Satyaki"
    assert events[0].payload["project"] == "proj-a"
    assert events[0].payload["focus"] == "refactoring bus"


def test_server_auto_emit_memory_updated(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    res = server.queue_durable_fact("Satyaki", "entity_x", "attr_y", "some durable fact text")
    assert res["ok"] is True

    events = bus_poll_events(since_seq=0)
    assert len(events) == 1
    assert events[0].event_type == "memory.updated"
    assert events[0].agent_name == "Satyaki"
    assert events[0].payload["entity"] == "entity_x"
    assert events[0].payload["attribute"] == "attr_y"
    assert events[0].payload["text"] == "some durable fact text"
