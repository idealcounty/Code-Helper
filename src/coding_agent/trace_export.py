"""Convert persisted Agent spans into a portable Chrome Trace document.

The exporter deliberately consumes the redacted event projection rather than
the live runtime state.  The resulting JSON can be opened by Perfetto or
Chrome's ``chrome://tracing`` without introducing an external tracing SDK.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable


def build_trace(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build a Chrome Trace Event JSON object from Agent span events.

    Only span metadata is exported.  Arbitrary event payloads are never copied
    into the trace, which keeps the export bounded and avoids reintroducing
    redacted prompts, command output, or file contents.
    """
    normalized: list[tuple[int, dict[str, Any], datetime | None]] = []
    session_id = ""
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        if not session_id:
            session_id = str(event.get("session_id") or "")
        normalized.append((index, event, _parse_timestamp(event.get("timestamp"))))

    timestamps = [timestamp for _, _, timestamp in normalized if timestamp is not None]
    origin = min(timestamps) if timestamps else None
    starts: dict[str, tuple[int, dict[str, Any], datetime | None]] = {}
    trace_events: list[dict[str, Any]] = []

    for index, event, timestamp in normalized:
        event_type = event.get("type")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event_type == "span_started":
            span_id = str(payload.get("span_id") or event.get("event_id") or "")
            if span_id:
                starts[span_id] = (index, event, timestamp)
            continue
        if event_type != "span_finished":
            continue

        span_id = str(payload.get("span_id") or "")
        if not span_id:
            continue
        start = starts.pop(span_id, None)
        if start is None:
            # A finished span without a persisted start is incomplete; expose
            # it as an instant marker instead of inventing a duration.
            trace_events.append(
                _instant_event(
                    name=_span_name(payload),
                    timestamp=_relative_us(timestamp, origin, index),
                    turn_id=event.get("turn_id"),
                    args={"span_id": span_id, "incomplete": True},
                )
            )
            continue

        _, started_event, started_at = start
        started_payload = started_event.get("payload") or {}
        if not isinstance(started_payload, dict):
            started_payload = {}
        duration = _non_negative_number(payload.get("duration_ms"))
        if duration is None and started_at is not None and timestamp is not None:
            duration = max(0.0, (timestamp - started_at).total_seconds() * 1000)
        if duration is None:
            duration = 0.0
        trace_events.append(
            {
                "name": _span_name(started_payload or payload),
                "cat": "code-helper",
                "ph": "X",
                "ts": _relative_us(started_at, origin, index),
                "dur": round(duration * 1000, 3),
                "pid": "code-helper",
                "tid": str(started_event.get("turn_id") or event.get("turn_id") or ""),
                "args": _safe_span_args(started_payload, span_id),
            }
        )

    # Keep an unfinished span visible after a crash or interrupted session.
    for span_id, (_, event, timestamp) in starts.items():
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        trace_events.append(
            _instant_event(
                name=_span_name(payload),
                timestamp=_relative_us(timestamp, origin, 0),
                turn_id=event.get("turn_id"),
                args={**_safe_span_args(payload, span_id), "incomplete": True},
            )
        )

    trace_events.sort(key=lambda item: (float(item.get("ts", 0)), str(item.get("name", ""))))
    return {
        "traceEvents": trace_events,
        "displayTimeUnit": "ms",
        "metadata": {
            "source": "code-helper",
            "schema_version": 1,
            "session_id": session_id,
            "span_count": len(trace_events),
        },
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _relative_us(timestamp: datetime | None, origin: datetime | None, fallback: int) -> float:
    if timestamp is None or origin is None:
        return float(fallback)
    return round(max(0.0, (timestamp - origin).total_seconds() * 1_000_000), 3)


def _non_negative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _span_name(payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind") or "span")
    lifecycle = str(payload.get("lifecycle") or "")
    hook = str(payload.get("hook") or "")
    suffix = "/".join(item for item in (lifecycle, hook) if item)
    return f"{kind}:{suffix}" if suffix else kind


def _safe_span_args(payload: dict[str, Any], span_id: str) -> dict[str, Any]:
    allowed = ("kind", "step", "lifecycle", "hook", "tool", "tool_call_id")
    args = {key: payload[key] for key in allowed if key in payload}
    args["span_id"] = span_id
    return args


def _instant_event(
    *, name: str, timestamp: float, turn_id: Any, args: dict[str, Any]
) -> dict[str, Any]:
    return {
        "name": name,
        "cat": "code-helper",
        "ph": "i",
        "s": "t",
        "ts": timestamp,
        "pid": "code-helper",
        "tid": str(turn_id or ""),
        "args": args,
    }
