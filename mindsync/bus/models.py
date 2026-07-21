"""Data models for AgentRelay Event Bus."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import uuid
from datetime import datetime, timezone
from typing import Any, Optional


class EventType(str, Enum):
    TASK_CREATED = "task.created"
    JOB_STARTED = "job.started"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    FOCUS_CHANGED = "focus.changed"
    MEMORY_UPDATED = "memory.updated"
    CONFLICT_DETECTED = "conflict.detected"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    event_type: str | EventType
    agent_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=_utc_now_iso)
    seq: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.event_type, Enum):
            self.event_type = str(self.event_type.value)

    def to_dict(self) -> dict[str, Any]:
        ev_type = (
            str(self.event_type.value)
            if isinstance(self.event_type, Enum)
            else str(self.event_type)
        )
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": ev_type,
            "agent_name": self.agent_name,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        return cls(
            seq=int(data.get("seq", 0)),
            event_id=str(data.get("event_id") or uuid.uuid4().hex),
            event_type=str(data.get("event_type", "")),
            agent_name=str(data.get("agent_name", "")),
            payload=dict(data.get("payload") or {}),
            timestamp=str(data.get("timestamp") or _utc_now_iso()),
            correlation_id=data.get("correlation_id"),
        )


@dataclass
class Message:
    sender: str
    recipient: str
    content: dict[str, Any] | str
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=_utc_now_iso)
    correlation_id: Optional[str] = None
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            seq=int(data.get("seq", 0)),
            message_id=str(data.get("message_id") or uuid.uuid4().hex),
            sender=str(data.get("sender", "")),
            recipient=str(data.get("recipient", "")),
            content=data.get("content", ""),
            timestamp=str(data.get("timestamp") or _utc_now_iso()),
            correlation_id=data.get("correlation_id"),
        )


@dataclass
class Identity:
    agent_name: str
    role: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "role": self.role,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Identity:
        return cls(
            agent_name=str(data.get("agent_name", "")),
            role=str(data.get("role", "")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class PolicyRule:
    rule_id: str
    action: str
    effect: str = "allow"
    conditions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "action": self.action,
            "effect": self.effect,
            "conditions": self.conditions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PolicyRule:
        return cls(
            rule_id=str(data.get("rule_id", "")),
            action=str(data.get("action", "")),
            effect=str(data.get("effect", "allow")),
            conditions=dict(data.get("conditions") or {}),
        )
