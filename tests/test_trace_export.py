from coding_agent.trace_export import build_trace


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
