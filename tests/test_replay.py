from __future__ import annotations

from coding_agent.replay import build_step_frames


def test_step_frames_group_events_and_mark_errors() -> None:
    events = [
        {"turn_id": "turn", "sequence": 1, "timestamp": "2026-08-31T00:00:00+00:00", "type": "step_started", "payload": {"step": 1}},
        {"turn_id": "turn", "sequence": 2, "timestamp": "2026-08-31T00:00:00.100000+00:00", "type": "context_built", "payload": {"estimated_chars": 12}},
        {"turn_id": "turn", "sequence": 3, "timestamp": "2026-08-31T00:00:01+00:00", "type": "tool_result", "payload": {"name": "read_file", "result": {"ok": True}}},
        {"turn_id": "turn", "sequence": 4, "timestamp": "2026-08-31T00:00:02+00:00", "type": "run_failed", "payload": {"code": "ERR", "message": "failed"}},
    ]
    frames = build_step_frames(events)
    assert len(frames) == 1
    assert frames[0]["step"] == 1
    assert frames[0]["context_build"]["estimated_chars"] == 12
    assert frames[0]["tool_results"][0]["name"] == "read_file"
    assert frames[0]["errors"][0]["type"] == "run_failed"
    assert frames[0]["duration_ms"] == 2000.0


def test_failed_tool_result_is_marked_as_root_cause() -> None:
    events = [
        {"turn_id": "turn", "sequence": 1, "type": "step_started", "payload": {"step": 1}},
        {
            "turn_id": "turn",
            "sequence": 2,
            "type": "tool_result",
            "payload": {
                "name": "run_command",
                "result": {"ok": False, "code": "COMMAND_FAILED", "message": "exit 1"},
            },
        },
    ]
    frames = build_step_frames(events)
    assert frames[0]["errors"][0]["type"] == "tool_result_failed"
    assert frames[0]["errors"][0]["sequence"] == 2
