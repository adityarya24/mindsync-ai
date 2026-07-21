"""EventBus engine for AgentRelay Event Bus in MindSync."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from mindsync.bus.models import Event, _utc_now_iso
from mindsync.storage import file_lock


class EventBus:
    def __init__(
        self,
        events_file: Optional[Path] = None,
        subscriptions_file: Optional[Path] = None,
    ) -> None:
        self._events_file = events_file
        self._subscriptions_file = subscriptions_file

    @property
    def events_file(self) -> Path:
        if self._events_file is not None:
            return self._events_file
        from mindsync.config import settings
        return settings.events_file

    @property
    def subscriptions_file(self) -> Path:
        if self._subscriptions_file is not None:
            return self._subscriptions_file
        from mindsync.config import settings
        return settings.subscriptions_file

    def _ensure_dir(self, file_path: Path) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)

    def _set_file_permissions(self, file_path: Path) -> None:
        try:
            file_path.chmod(0o600)
        except OSError:
            pass

    def _get_next_seq(self) -> int:
        if not self.events_file.exists():
            return 1
        max_seq = 0
        try:
            with open(self.events_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        seq = int(data.get("seq", 0))
                        if seq > max_seq:
                            max_seq = seq
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
        except OSError:
            pass
        return max_seq + 1

    def publish_event(self, event: Event) -> Event:
        self._ensure_dir(self.events_file)
        with file_lock("events"):
            seq = self._get_next_seq()
            event.seq = seq
            if not event.timestamp:
                event.timestamp = _utc_now_iso()
            event_dict = event.to_dict()
            with open(self.events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event_dict, ensure_ascii=False) + "\n")
            self._set_file_permissions(self.events_file)
        return event

    def poll_events(
        self,
        since_seq: int = 0,
        event_types: Optional[list[str]] = None,
        limit: int = 50,
        agent_name: Optional[str] = None,
    ) -> list[Event]:
        if not self.events_file.exists():
            return []

        filter_types: Optional[set[str]] = None
        if event_types is not None and len(event_types) > 0:
            filter_types = {str(t.value if hasattr(t, "value") else t) for t in event_types}
        elif agent_name:
            sub_types = self.get_subscription(agent_name)
            if sub_types:
                filter_types = set(sub_types)

        events: list[Event] = []
        try:
            with open(self.events_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        seq = int(data.get("seq", 0))
                        if seq <= since_seq:
                            continue
                        ev_type = str(data.get("event_type", ""))
                        if filter_types is not None and ev_type not in filter_types:
                            continue
                        events.append(Event.from_dict(data))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
        except OSError:
            pass

        if limit > 0:
            events = events[:limit]
        return events

    def subscribe(
        self,
        agent_name: str,
        event_types: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        self._ensure_dir(self.subscriptions_file)
        norm_types = (
            [str(t.value if hasattr(t, "value") else t) for t in event_types]
            if event_types is not None
            else []
        )
        with file_lock("subscriptions"):
            subs: dict[str, Any] = {}
            if self.subscriptions_file.exists():
                try:
                    with open(self.subscriptions_file, "r", encoding="utf-8") as f:
                        subs = json.load(f)
                except (json.JSONDecodeError, OSError):
                    subs = {}

            entry = {
                "agent_name": agent_name,
                "event_types": norm_types,
                "updated_at": _utc_now_iso(),
            }
            subs[agent_name] = entry

            with open(self.subscriptions_file, "w", encoding="utf-8") as f:
                json.dump(subs, f, indent=2, ensure_ascii=False)
            self._set_file_permissions(self.subscriptions_file)

        return entry

    def get_subscription(self, agent_name: str) -> Optional[list[str]]:
        if not self.subscriptions_file.exists():
            return None
        try:
            with open(self.subscriptions_file, "r", encoding="utf-8") as f:
                subs = json.load(f)
                if isinstance(subs, dict) and agent_name in subs:
                    return subs[agent_name].get("event_types")
        except (json.JSONDecodeError, OSError):
            pass
        return None


default_bus = EventBus()


def publish_event(event: Event) -> Event:
    return default_bus.publish_event(event)


def poll_events(
    since_seq: int = 0,
    event_types: Optional[list[str]] = None,
    limit: int = 50,
    agent_name: Optional[str] = None,
) -> list[Event]:
    return default_bus.poll_events(
        since_seq=since_seq, event_types=event_types, limit=limit, agent_name=agent_name
    )


def subscribe(agent_name: str, event_types: Optional[list[str]] = None) -> dict[str, Any]:
    return default_bus.subscribe(agent_name=agent_name, event_types=event_types)
