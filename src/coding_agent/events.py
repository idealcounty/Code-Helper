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

CURRENT_EVENT_SCHEMA_VERSION = 1
MIN_SUPPORTED_EVENT_SCHEMA_VERSION = 1


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
    schema_version: int = CURRENT_EVENT_SCHEMA_VERSION
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
        max_storage_bytes: int | None = 100_000_000,
        max_session_files: int | None = 256,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.path = self.root / f"{session_id}.jsonl"
        self.max_storage_bytes = max_storage_bytes
        self.max_session_files = max_session_files
        self.last_prune_diagnostics: list[dict[str, Any]] = []
        self.last_load_diagnostics: list[dict[str, Any]] = []
        self.redactor = redactor or Redactor(secret_values)
        self._prune_session_store()

    def append(self, event: AgentEvent) -> None:
        _validate_live_event(event, expected_session_id=self.session_id)
        safe_data = self.redactor.redact(event.to_dict())
        safe_data.pop("integrity_sha256", None)
        digest = _integrity_digest(safe_data)
        safe_data["integrity_sha256"] = digest
        event.integrity_sha256 = digest
        line = json.dumps(safe_data, ensure_ascii=False, default=str)
        self._prune_session_store(incoming_bytes=len(line.encode("utf-8")) + 1)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.write("\n")

    def _prune_session_store(self, *, incoming_bytes: int = 0) -> int:
        """Prune only historical sessions; never remove the active session file."""
        self.last_prune_diagnostics = []
        candidates = sorted(
            (
                path
                for path in self.root.glob("*.jsonl")
                if path.is_file() and path.resolve() != self.path.resolve()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        current_size = self.path.stat().st_size if self.path.exists() else 0
        total = current_size + sum(path.stat().st_size for path in candidates)
        removed = 0
        while candidates and (
            (self.max_storage_bytes is not None and total + incoming_bytes > self.max_storage_bytes)
            or (
                self.max_session_files is not None
                and len(candidates) + 1 > self.max_session_files
            )
        ):
            oldest = candidates.pop(0)
            try:
                size = oldest.stat().st_size
                oldest.unlink()
            except OSError as exc:
                self.last_prune_diagnostics.append(
                    {"code": "SESSION_PRUNE_FAILED", "path": oldest.name, "message": str(exc)}
                )
                continue
            total -= size
            removed += 1
            self.last_prune_diagnostics.append(
                {"code": "SESSION_PRUNED", "path": oldest.name, "bytes": size}
            )
        return removed

    def load(self) -> list[dict[str, Any]]:
        self.last_load_diagnostics = []
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        max_sequence = 0
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
            schema_version = parsed.get("schema_version")
            if schema_version is None:
                self.last_load_diagnostics.append(
                    {
                        "code": "LEGACY_EVENT_SCHEMA_ASSUMED",
                        "line": line_number,
                        "schema_version": MIN_SUPPORTED_EVENT_SCHEMA_VERSION,
                    }
                )
            elif (
                isinstance(schema_version, bool)
                or not isinstance(schema_version, int)
                or schema_version < MIN_SUPPORTED_EVENT_SCHEMA_VERSION
            ):
                raise ValueError(
                    f"Unsupported session event schema at line {line_number}: "
                    f"{schema_version!r}"
                )
            elif schema_version > CURRENT_EVENT_SCHEMA_VERSION:
                self.last_load_diagnostics.append(
                    {
                        "code": "UNSUPPORTED_EVENT_SCHEMA",
                        "line": line_number,
                        "schema_version": schema_version,
                        "supported_max": CURRENT_EVENT_SCHEMA_VERSION,
                    }
                )
                raise ValueError(
                    f"Unsupported session event schema at line {line_number}: "
                    f"{schema_version} > {CURRENT_EVENT_SCHEMA_VERSION}"
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
            raw_type = parsed.get("type")
            if not isinstance(raw_type, str) or not raw_type.strip():
                raise ValueError(
                    f"Invalid session event type at line {line_number}: "
                    "expected non-empty string"
                )
            # Older JSONL writers did not always persist identity metadata.
            # Normalize those records in memory so a new process can continue
            # the same sequence and causation chain without rewriting history.
            raw_sequence = parsed.get("sequence")
            if raw_sequence is None or (
                isinstance(raw_sequence, int)
                and not isinstance(raw_sequence, bool)
                and raw_sequence <= 0
            ):
                max_sequence += 1
                parsed["sequence"] = max_sequence
                self.last_load_diagnostics.append(
                    {
                        "code": "LEGACY_EVENT_SEQUENCE_ASSUMED",
                        "line": line_number,
                        "sequence": max_sequence,
                    }
                )
            elif isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int):
                raise ValueError(
                    f"Invalid session event sequence at line {line_number}: "
                    f"{raw_sequence!r}"
                )
            else:
                max_sequence = max(max_sequence, raw_sequence)

            raw_event_id = parsed.get("event_id")
            if not isinstance(raw_event_id, str) or not raw_event_id.strip():
                parsed["event_id"] = "legacy-" + _integrity_digest(
                    {"session_id": self.session_id, "line": line_number, "event": parsed}
                )
                self.last_load_diagnostics.append(
                    {"code": "LEGACY_EVENT_ID_DERIVED", "line": line_number}
                )
            if schema_version is None:
                parsed["schema_version"] = MIN_SUPPORTED_EVENT_SCHEMA_VERSION
            # Early writers occasionally omitted envelope fields while still
            # recording a valid event type. Fill only fields whose safe value
            # is implied by this EventStore; never infer a missing event type.
            raw_session_id = parsed.get("session_id")
            if not isinstance(raw_session_id, str) or not raw_session_id.strip():
                parsed["session_id"] = self.session_id
                self.last_load_diagnostics.append(
                    {"code": "LEGACY_EVENT_SESSION_ID_ASSUMED", "line": line_number}
                )
            elif raw_session_id != self.session_id:
                raise ValueError(
                    f"Invalid session event session_id at line {line_number}: "
                    f"expected {self.session_id!r}, got {raw_session_id!r}"
                )
            raw_turn_id = parsed.get("turn_id")
            if not isinstance(raw_turn_id, str) or not raw_turn_id.strip():
                parsed["turn_id"] = "legacy-turn-" + _integrity_digest(
                    {"session_id": self.session_id, "path": self.path.name}
                )[:16]
                self.last_load_diagnostics.append(
                    {"code": "LEGACY_EVENT_TURN_ID_ASSUMED", "line": line_number}
                )
            if "payload" not in parsed:
                parsed["payload"] = {}
                self.last_load_diagnostics.append(
                    {"code": "LEGACY_EVENT_PAYLOAD_ASSUMED", "line": line_number}
                )
            elif not isinstance(parsed["payload"], dict):
                raise ValueError(
                    f"Invalid session event payload at line {line_number}: expected object"
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
        self._sequence, self._last_event_id = self._load_tail_metadata()
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

    @property
    def sequence(self) -> int:
        return self._sequence

    async def publish(self, event: AgentEvent) -> AgentEvent:
        async with self._lock:
            self._sequence += 1
            event.sequence = self._sequence
            if not event.event_id:
                event.event_id = uuid4().hex
            # Link events by default so recovery diagnostics can follow one
            # durable causal chain. Explicit IDs remain available for fan-in.
            if event.causation_id is None and self._last_event_id:
                event.causation_id = self._last_event_id
            self.store.append(event)
            self._last_event_id = event.event_id

        safe_event = self.store.redacted_event(event)
        for listener in tuple(self._listeners):
            result = listener(safe_event)
            if inspect.isawaitable(result):
                await result
        return event

    def _load_tail_metadata(self) -> tuple[int, str | None]:
        path = self.store.path
        if not path.exists():
            return 0, None
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                end = handle.tell()
                window = min(end, 64 * 1024)
                handle.seek(end - window)
                data = handle.read(window)
            lines = data.splitlines()
            if end > window and lines:
                lines = lines[1:]  # The first line may start in the middle.
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    tail = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(tail, dict):
                    continue
                sequence = tail.get("sequence")
                if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0:
                    return sequence, str(tail.get("event_id") or "") or None
        except OSError:
            return 0, None

        # Legacy stores without sequence metadata need the normalizer in load().
        events = self.store.load()
        if not events:
            return 0, None
        tail = events[-1]
        return int(tail.get("sequence", 0)), str(tail.get("event_id") or "") or None


def _integrity_digest(data: dict[str, Any]) -> str:
    canonical = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    import hashlib

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_live_event(event: AgentEvent, *, expected_session_id: str) -> None:
    """Reject malformed current events before they can enter durable history."""
    if not isinstance(event.type, str) or not event.type.strip():
        raise ValueError("Event type must be a non-empty string")
    if event.session_id != expected_session_id:
        raise ValueError(
            "Event session_id does not match its EventStore: "
            f"expected {expected_session_id!r}, got {event.session_id!r}"
        )
    if not isinstance(event.turn_id, str) or not event.turn_id.strip():
        raise ValueError("Event turn_id must be a non-empty string")
    if not isinstance(event.payload, dict):
        raise ValueError("Event payload must be an object")
