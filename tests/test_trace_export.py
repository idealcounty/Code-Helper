from datetime import UTC, datetime

from coding_agent.trace_export import (
    _non_negative_number,
    _parse_timestamp,
    _relative_us,
    _safe_span_args,
    _span_name,
    build_trace,
)


def test_build_trace_pairs_spans_and_keeps_only_safe_metadata() -> None:
    events = [
        {
            "type": "span_started",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "timestamp": "2026-08-30T10:00:00+00:00",
            "payload": {
                "span_id": "span-1",
                "kind": "hook",
                "lifecycle": "PreToolUse",
                "hook": "policy",
                "secret": "must-not-export",
            },
        },
        {
            "type": "span_finished",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "timestamp": "2026-08-30T10:00:00.125+00:00",
            "payload": {"span_id": "span-1", "kind": "hook", "duration_ms": 125},
        },
    ]

    report = build_trace(events)

    assert report["metadata"] == {
        "source": "code-helper",
        "schema_version": 1,
        "session_id": "session-1",
        "span_count": 1,
    }
    item = report["traceEvents"][0]
    assert item["name"] == "hook:PreToolUse/policy"
    assert item["ph"] == "X"
    assert item["ts"] == 0.0
    assert item["dur"] == 125000.0
    assert item["args"] == {
        "kind": "hook",
        "lifecycle": "PreToolUse",
        "hook": "policy",
        "span_id": "span-1",
    }


def test_build_trace_marks_unfinished_and_orphan_spans() -> None:
    events = [
        {
            "type": "span_started",
            "session_id": "session-2",
            "turn_id": "turn-2",
            "timestamp": "2026-08-30T10:00:01+00:00",
            "payload": {"span_id": "open", "kind": "model_request", "step": 2},
        },
        {
            "type": "span_finished",
            "session_id": "session-2",
            "turn_id": "turn-2",
            "timestamp": "2026-08-30T10:00:02+00:00",
            "payload": {"span_id": "orphan", "kind": "context_build"},
        },
    ]

    items = build_trace(events)["traceEvents"]

    assert len(items) == 2
    assert all(item["ph"] == "i" for item in items)
    assert all(item["args"]["incomplete"] is True for item in items)
    assert {item["args"]["span_id"] for item in items} == {"open", "orphan"}


def test_trace_helpers_handle_invalid_values_and_safe_metadata() -> None:
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("invalid") is None
    naive = _parse_timestamp("2026-01-01T00:00:00")
    assert naive is not None and naive.tzinfo is UTC
    assert _relative_us(None, None, 7) == 7.0
    assert _relative_us(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC), 0) == 0.0
    assert _non_negative_number(True) is None
    assert _non_negative_number("bad") is None
    assert _non_negative_number(-1) is None
    assert _non_negative_number("2.5") == 2.5
    assert _span_name({"kind": "tool", "lifecycle": "pre"}) == "tool:pre"
    assert _span_name({}) == "span"
    assert _safe_span_args({"kind": "tool", "secret": "no"}, "id") == {"kind": "tool", "span_id": "id"}


def test_build_trace_skips_malformed_events_and_derives_duration() -> None:
    report = build_trace(
        [
            "malformed",
            {"type": "ignored", "session_id": "s", "payload": "bad"},
            {"type": "span_started", "session_id": "s", "payload": {}},
            {"type": "span_finished", "session_id": "s", "payload": {}},
            {"type": "span_started", "session_id": "s", "turn_id": "t", "event_id": "fallback", "payload": {}},
            {"type": "span_finished", "session_id": "s", "turn_id": "t", "timestamp": "2026-01-01T00:00:01Z", "payload": {"span_id": "fallback"}},
            {"type": "span_started", "session_id": "s", "turn_id": "t2", "timestamp": "2026-01-01T00:00:00Z", "payload": {"span_id": "derived", "kind": "tool"}},
            {"type": "span_finished", "session_id": "s", "turn_id": "t2", "timestamp": "2026-01-01T00:00:00.010Z", "payload": {"span_id": "derived", "duration_ms": "bad"}},
        ]
    )
    assert report["metadata"]["session_id"] == "s"
    assert len(report["traceEvents"]) == 2
    assert all(item["ph"] == "X" for item in report["traceEvents"])
    assert {item["dur"] for item in report["traceEvents"]} == {0.0, 10000.0}
