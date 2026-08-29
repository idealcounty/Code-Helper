from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from coding_agent.config import AppConfig
from coding_agent.events import AgentEvent
from coding_agent.runtime import AgentRuntime, create_runtime
from coding_agent.session import AgentStatus


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        api_key="deterministic-recovery-test",
        base_url="https://example.invalid/v1",
        max_steps=4,
        request_timeout=2,
        command_timeout=2,
        run_timeout=10,
        token_budget=1_000,
        user_memory_dir=tmp_path.parent / "recovery-user-memory",
    )


def _runtime(tmp_path: Path, session_id: str | None = None) -> AgentRuntime:
    return create_runtime(
        config=_config(tmp_path),
        workspace_path=tmp_path,
        mode="act",
        session_id=session_id,
    )


def _publish(runtime: AgentRuntime, event_type: str, payload: dict[str, Any]) -> None:
    asyncio.run(
        runtime.event_bus.publish(
            AgentEvent(
                type=event_type,
                session_id=runtime.state.session_id,
                turn_id=runtime.state.turn_id,
                payload=payload,
            )
        )
    )


def _restart(runtime: AgentRuntime) -> AgentRuntime:
    recovered = _runtime(
        runtime.workspace.root,
        session_id=runtime.state.session_id,
    )
    recovered.state.restore_from_events(recovered.event_store.load())
    return recovered


def _assistant_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _start_turn(runtime: AgentRuntime, objective: str, call: dict[str, Any]) -> None:
    _publish(runtime, "turn_started", {"message": objective})
    _publish(runtime, "assistant_response", {"content": "", "tool_calls": [call]})


def test_recovery_matrix_waiting_for_approval_does_not_execute(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    call = _assistant_call("approval-1", "apply_patch", {"path": "app.py"})
    _start_turn(runtime, "approve the edit", call)
    _publish(
        runtime,
        "approval_requested",
        {
            "id": "approval-1",
            "name": "apply_patch",
            "arguments": {"path": "app.py"},
            "reason": "write requires approval",
            "remaining": [],
        },
    )

    recovered = _restart(runtime)

    assert recovered.state.status is AgentStatus.WAITING_APPROVAL
    assert recovered.state.pending_approval is not None
    assert recovered.state.interrupted_tool_calls == []
    assert not (tmp_path / "app.py").exists()
    assert not any(event["type"] == "tool_result" for event in recovered.event_store.load())


def test_recovery_matrix_file_written_before_result_is_not_replayed(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 2\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    call = _assistant_call(
        "write-1",
        "apply_patch",
        {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
    )
    _start_turn(runtime, "apply the edit", call)
    _publish(runtime, "checkpoint_created", {"path": "app.py", "existed": True})
    _publish(runtime, "tool_started", {"id": "write-1", "name": "apply_patch", "arguments": call["arguments"]})

    recovered = _restart(runtime)

    assert recovered.state.status is AgentStatus.EXECUTING_TOOL
    assert [item["id"] for item in recovered.state.interrupted_tool_calls] == ["write-1"]
    assert recovered.state.completed_tool_call_ids == set()
    assert path.read_text(encoding="utf-8") == "value = 2\n"
    assert len([event for event in recovered.event_store.load() if event["type"] == "tool_result"]) == 0


def test_recovery_matrix_running_command_is_not_replayed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    call = _assistant_call("command-1", "run_command", {"command": "python build.py"})
    _start_turn(runtime, "run the build", call)
    _publish(runtime, "tool_started", {"id": "command-1", "name": "run_command", "arguments": call["arguments"]})

    recovered = _restart(runtime)

    assert recovered.state.status is AgentStatus.EXECUTING_TOOL
    assert recovered.state.interrupted_tool_calls[0]["id"] == "command-1"
    assert recovered.state.interrupted_tool_calls[0]["code"] == "INTERRUPTED_UNKNOWN"
    assert not any(event["type"] == "tool_result" for event in recovered.event_store.load())


def test_recovery_matrix_completed_verification_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 2\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    call = _assistant_call(
        "write-2",
        "apply_patch",
        {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
    )
    _start_turn(runtime, "edit and verify", call)
    _publish(runtime, "tool_started", {"id": "write-2", "name": "apply_patch", "arguments": call["arguments"]})
    _publish(
        runtime,
        "tool_result",
        {
            "id": "write-2",
            "name": "apply_patch",
            "arguments": call["arguments"],
            "result": {
                "ok": True,
                "code": "OK",
                "metadata": {"mutated_files": ["app.py"], "duration_ms": 4},
            },
        },
    )
    _publish(
        runtime,
        "verification_recorded",
        {
            "evidence": {
                "command": "python -m pytest -q",
                "accepted": True,
                "finished_sequence": 4,
            }
        },
    )
    _publish(runtime, "turn_finished", {"status": "completed", "message": "verified"})

    recovered = _restart(runtime)

    assert recovered.state.status is AgentStatus.COMPLETED
    assert recovered.state.changed_files == {"app.py"}
    assert recovered.state.verification_is_fresh is True
    assert recovered.state.last_successful_verification_sequence > 0
    assert recovered.state.interrupted_tool_calls == []
    assert len([event for event in recovered.event_store.load() if event["type"] == "tool_result"]) == 1
