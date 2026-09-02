"""Derived, redacted views over the append-only Agent event stream."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


ERROR_TYPES = {
    "run_failed",
    "run_budget_exhausted",
    "tool_protocol_error",
    "model_error",
    "verification_required",
    "stuck_terminal",
    "algorithm_report_failed",
}


@dataclass(slots=True)
class StepFrame:
    step: int
    turn_id: str
    started_sequence: int = 0
    finished_sequence: int = 0
    context_build: dict[str, Any] = field(default_factory=dict)
    model_request: dict[str, Any] = field(default_factory=dict)
    assistant_response: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "turn_id": self.turn_id,
            "started_sequence": self.started_sequence,
            "finished_sequence": self.finished_sequence,
            "context_build": self.context_build,
            "model_request": self.model_request,
            "assistant_response": self.assistant_response,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "verification": self.verification,
            "errors": self.errors,
            "events": self.events,
            "duration_ms": round(self.duration_ms, 3),
        }


def build_step_frames(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group redacted event records by ``turn_id + step``.

    The function accepts already redacted/compact records so callers cannot
    accidentally expose provider private reasoning or protected file content.
    """
    frames: dict[tuple[str, int], StepFrame] = {}
    current_step: dict[str, int] = {}
    timestamps: dict[tuple[str, int], list[datetime]] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        turn_id = str(event.get("turn_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "step_started":
            try:
                step = max(0, int(payload.get("step") or 0))
            except (TypeError, ValueError):
                step = current_step.get(turn_id, 0) + 1
            current_step[turn_id] = step
        else:
            step = current_step.get(turn_id, 0)
        key = (turn_id, step)
        frame = frames.setdefault(key, StepFrame(step=step, turn_id=turn_id))
        sequence = _sequence(event)
        if frame.started_sequence == 0:
            frame.started_sequence = sequence
        frame.finished_sequence = max(frame.finished_sequence, sequence)
        compact = {key: event[key] for key in ("sequence", "timestamp", "type", "payload") if key in event}
        frame.events.append(compact)
        timestamp = _timestamp(event.get("timestamp"))
        if timestamp is not None:
            timestamps.setdefault(key, []).append(timestamp)
        if event_type == "context_built":
            frame.context_build = dict(payload)
        elif event_type == "model_started":
            frame.model_request = dict(payload)
        elif event_type == "assistant_response":
            frame.assistant_response = dict(payload)
            calls = payload.get("tool_calls")
            if isinstance(calls, list):
                frame.tool_calls.extend(call for call in calls if isinstance(call, dict))
        elif event_type == "tool_started":
            frame.tool_calls.append(dict(payload))
        elif event_type == "tool_result":
            frame.tool_results.append(dict(payload))
            # A failed tool result is the first observable failure even when
            # the loop later emits a generic ``run_failed`` event.  Mark it
            # here so the debugger can jump to the actual origin instead of
            # only highlighting the final cascade error.
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            if result and not bool(result.get("ok")):
                frame.errors.append(
                    {
                        "type": "tool_result_failed",
                        "payload": {
                            "name": payload.get("name", ""),
                            "code": result.get("code", ""),
                            "message": str(result.get("message") or "")[:1000],
                        },
                        "sequence": sequence,
                    }
                )
        elif event_type == "verification_recorded":
            frame.verification.append(dict(payload))
        if event_type in ERROR_TYPES or event_type.endswith("_failed"):
            frame.errors.append({"type": event_type, "payload": dict(payload), "sequence": sequence})
    output: list[dict[str, Any]] = []
    for key, frame in sorted(frames.items(), key=lambda item: (item[0][0], item[0][1])):
        points = timestamps.get(key, [])
        if len(points) >= 2:
            frame.duration_ms = max(0.0, (max(points) - min(points)).total_seconds() * 1000)
        output.append(frame.to_dict())
    return output


def _sequence(event: dict[str, Any]) -> int:
    try:
        return max(0, int(event.get("sequence") or 0))
    except (TypeError, ValueError):
        return 0


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
