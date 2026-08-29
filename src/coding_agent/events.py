from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .redaction import Redactor


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
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: uuid4().hex)
    causation_id: str | None = None
    integrity_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


EventListener = Callable[[AgentEvent], None | Awaitable[None]]


class EventStore:
    """Append-only JSONL storage for a single session."""

    def __init__(
        self,
        root: Path,
        session_id: str,
        *,
        redactor: Redactor | None = None,
        secret_values: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{session_id}.jsonl"
        self.last_load_diagnostics: list[dict[str, Any]] = []
        self.redactor = redactor or Redactor(secret_values)

    def append(self, event: AgentEvent) -> None:
        safe_data = self.redactor.redact(event.to_dict())
        safe_data.pop("integrity_sha256", None)
        digest = _integrity_digest(safe_data)
        safe_data["integrity_sha256"] = digest
        event.integrity_sha256 = digest
        line = json.dumps(safe_data, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.write("\n")

    def load(self) -> list[dict[str, Any]]:
        self.last_load_diagnostics = []
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        non_empty_lines = [
            number for number, line in enumerate(lines, start=1) if line.strip()
        ]
        last_non_empty_line = non_empty_lines[-1] if non_empty_lines else 0
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                if line_number == last_non_empty_line:
                    self.last_load_diagnostics.append(
                        {
                            "code": "TRAILING_EVENT_CORRUPT",
                            "line": line_number,
                            "message": str(exc),
                        }
                    )
                    continue
                raise ValueError(
                    f"Invalid session event at line {line_number}: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"Invalid session event at line {line_number}: expected object"
                )
            stored_digest = parsed.get("integrity_sha256")
            if stored_digest:
                unsigned = dict(parsed)
                unsigned.pop("integrity_sha256", None)
                if stored_digest != _integrity_digest(unsigned):
                    if line_number == last_non_empty_line:
                        self.last_load_diagnostics.append(
                            {
                                "code": "TRAILING_EVENT_INTEGRITY_FAILED",
                                "line": line_number,
                            }
                        )
                        continue
                    raise ValueError(
                        f"Invalid session event integrity at line {line_number}"
                    )
            else:
                self.last_load_diagnostics.append(
                    {"code": "LEGACY_EVENT_UNSIGNED", "line": line_number}
                )
            events.append(self.redactor.redact(parsed))
        return events

    def redacted_event(self, event: AgentEvent) -> AgentEvent:
        """Return a safe copy for UI listeners while keeping live state private."""
        return replace(event, payload=self.redactor.redact(event.payload))


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
            if not event.event_id:
                event.event_id = uuid4().hex
            self.store.append(event)

        safe_event = self.store.redacted_event(event)
        for listener in tuple(self._listeners):
            result = listener(safe_event)
            if inspect.isawaitable(result):
                await result
        return event

    def _load_last_sequence(self) -> int:
        events = self.store.load()
        return int(events[-1].get("sequence", 0)) if events else 0


def _integrity_digest(data: dict[str, Any]) -> str:
    canonical = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    import hashlib

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
