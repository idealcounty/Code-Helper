from __future__ import annotations

import json

from coding_agent.session import AgentState, AgentStatus


def _event(event_type: str, sequence: int, payload: dict, **extra: object) -> dict:
    return {
        "type": event_type,
        "session_id": "session",
        "turn_id": "turn-1",
        "sequence": sequence,
        "event_id": extra.pop("event_id", f"event-{sequence}"),
        "schema_version": 1,
        "payload": payload,
    }


def test_reducer_restores_pending_approval_without_replaying_tools() -> None:
    state = AgentState.create(session_id="session")
    state.restore_from_events(
        [
            _event("turn_started", 1, {"message": "edit app.py"}),
            _event(
                "approval_requested",
                2,
                {
                    "id": "write-1",
                    "name": "apply_patch",
                    "arguments": {"path": "app.py"},
                    "reason": "write requires approval",
                    "remaining": [{"id": "verify-1", "name": "run_command", "arguments": {}}],
                },
            ),
        ]
    )

    assert state.status is AgentStatus.WAITING_APPROVAL
    assert state.pending_approval == {
        "call": {
            "id": "write-1",
            "name": "apply_patch",
            "arguments": {"path": "app.py"},
        },
        "remaining": [{"id": "verify-1", "name": "run_command", "arguments": {}}],
        "reason": "write requires approval",
    }
    assert state.interrupted_tool_calls == []


def test_reducer_marks_redacted_approval_as_requires_reissue() -> None:
    state = AgentState.create(session_id="session")
    state.restore_from_events(
        [
            _event("turn_started", 1, {"message": "run with a secret"}),
            _event(
                "approval_requested",
                2,
                {
                    "id": "cmd-1",
                    "name": "run_command",
                    "arguments": {"command": "[REDACTED]"},
                    "reason": "command requires approval",
                },
            ),
        ]
    )

    assert state.pending_approval is not None
    assert state.pending_approval["redacted"] is True


def test_reducer_marks_started_without_result_and_never_replays_it() -> None:
    state = AgentState.create(session_id="session")
    events = [
        _event("turn_started", 1, {"message": "run a command"}),
        _event(
            "tool_started",
            2,
            {"id": "cmd-1", "name": "run_command", "arguments": {"command": "make"}},
        ),
    ]
    state.restore_from_events(events)

    assert state.interrupted_tool_calls[0]["code"] == "INTERRUPTED_UNKNOWN"
    assert state.interrupted_tool_calls[0]["id"] == "cmd-1"
    assert state.recovery_warnings[0]["code"] == "INTERRUPTED_UNKNOWN"
    assert state.status is AgentStatus.EXECUTING_TOOL


def test_reducer_deduplicates_tool_result_by_call_id() -> None:
    state = AgentState.create(session_id="session")
    result = {
        "ok": True,
        "code": "OK",
        "metadata": {"mutated_files": ["app.py"], "duration_ms": 4},
    }
    events = [
        _event("turn_started", 1, {"message": "edit"}),
        _event("tool_started", 2, {"id": "write-1", "name": "write_file", "arguments": {}}),
        _event(
            "tool_result",
            3,
            {"id": "write-1", "name": "write_file", "arguments": {}, "result": result},
        ),
        _event(
            "tool_result",
            4,
            {"id": "write-1", "name": "write_file", "arguments": {}, "result": result},
        ),
    ]
    state.restore_from_events(events)

    assert len([item for item in state.messages if item["role"] == "tool"]) == 1
    assert state.tool_stats["write_file"]["calls"] == 1
    assert state.changed_files == {"app.py"}
    assert state.completed_tool_call_ids == {"write-1"}


def test_recovery_matrix_preserves_write_and_verification_facts() -> None:
    state = AgentState.create(session_id="session")
    state.restore_from_events(
        [
            _event("turn_started", 1, {"message": "edit and verify"}),
            _event(
                "tool_started",
                2,
                {"id": "write-1", "name": "write_file", "arguments": {"path": "app.py"}},
            ),
            _event(
                "tool_result",
                3,
                {
                    "id": "write-1",
                    "name": "write_file",
                    "arguments": {"path": "app.py"},
                    "result": {
                        "ok": True,
                        "code": "OK",
                        "metadata": {"mutated_files": ["app.py"]},
                    },
                },
            ),
            _event(
                "verification_recorded",
                4,
                {
                    "evidence": {
                        "command": "python -m pytest -q",
                        "accepted": True,
                    }
                },
            ),
        ]
    )

    assert state.interrupted_tool_calls == []
    assert state.changed_files == {"app.py"}
    assert state.last_mutation_sequence == 3
    assert state.last_successful_verification_sequence == 4
    assert state.verification_is_fresh is True


def test_reducer_ignores_duplicate_event_id() -> None:
    state = AgentState.create(session_id="session")
    event = _event("assistant_response", 2, {"content": "hello"}, event_id="same")
    state.restore_from_events(
        [_event("turn_started", 1, {"message": "hi"}), event, dict(event)]
    )

    assert [item["content"] for item in state.messages if item["role"] == "assistant"] == [
        "hello"
    ]
