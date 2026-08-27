from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AgentEvent:
    type: str
    session_id: str
    turn_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


EventListener = Callable[[AgentEvent], None | Awaitable[None]]


class EventStore:
    """Append-only JSONL storage for a single session."""

    def __init__(self, root: Path, session_id: str) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{session_id}.jsonl"

    def append(self, event: AgentEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.write("\n")

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid session event at line {line_number}: {exc}"
                    ) from exc
        return events


class EventBus:
    """Publish the same event to durable storage and UI listeners."""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._listeners: list[EventListener] = []
        self._sequence = self._load_last_sequence()
        self._lock = asyncio.Lock()

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def create_queue(self) -> tuple[asyncio.Queue[AgentEvent], Callable[[], None]]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

        async def enqueue(event: AgentEvent) -> None:
            await queue.put(event)

        return queue, self.subscribe(enqueue)

    async def publish(self, event: AgentEvent) -> AgentEvent:
        async with self._lock:
            self._sequence += 1
            event.sequence = self._sequence
            self.store.append(event)

        for listener in tuple(self._listeners):
            result = listener(event)
            if inspect.isawaitable(result):
                await result
        return event

    def _load_last_sequence(self) -> int:
        events = self.store.load()
        return int(events[-1].get("sequence", 0)) if events else 0

