from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


@dataclass
class JsonlFileSink:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class EventBus:
    def __init__(self, sinks: list[EventSink] | None = None):
        self.sinks = sinks or []
        self.last_event: dict[str, Any] | None = None

    def add_sink(self, sink: EventSink) -> None:
        self.sinks.append(sink)

    def emit(self, *, type: str, payload: dict[str, Any]) -> None:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": type,
            "payload": payload,
        }
        self.last_event = event
        for s in self.sinks:
            s.emit(event)
