from __future__ import annotations

import asyncio
import json
from pathlib import Path

from coding_agent.model import ModelResponse, ToolCall
from coding_agent.session import AgentState, AgentStatus
from coding_agent.stuck_detector import StuckDetector

from test_agent_loop import ScriptedModel, _make_runner


def test_repeated_edit_failure_gets_one_recovery_turn(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("value = 2\n", encoding="utf-8")
    stale_patch = {
        "path": "sample.py",
        "old_text": "value = 1",
        "new_text": "value = 3",
    }
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read-1", "read_file", {"path": "sample.py"})]),
            ModelResponse(tool_calls=[ToolCall("edit-1", "apply_patch", stale_patch)]),
            ModelResponse(tool_calls=[ToolCall("edit-2", "apply_patch", stale_patch)]),
            ModelResponse(tool_calls=[ToolCall("edit-3", "apply_patch", stale_patch)]),
            ModelResponse(tool_calls=[ToolCall("read-2", "read_file", {"path": "sample.py"})]),
            ModelResponse(content="The requested value is already present; no write is needed."),
        ]
    )
    runner, store = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=8, mode="act")

    result = asyncio.run(runner.run_turn(state, "Set value to 2"))

    assert result.status is AgentStatus.COMPLETED
    assert path.read_text(encoding="utf-8") == "value = 2\n"
    events = store.load()
    recovery = [event for event in events if event["type"] == "stuck_recovery"]
    assert len(recovery) == 1
    assert "Do not repeat the identical tool call" in recovery[0]["payload"]["message"]
    assert any(
        message.get("role") == "system"
        and "Re-read the target file" in message.get("content", "")
        for message in model.seen_messages[-1]
    )


def test_repeated_successful_action_gets_bounded_recovery_turn(tmp_path: Path) -> None:
    signature = json.dumps(
        {"name": "apply_patch", "arguments": {"path": "sample.py"}},
        sort_keys=True,
    )
    actions = [{"signature": signature, "result_code": "OK"}] * 3

    hint = StuckDetector().recovery_hint(actions)

    assert hint is not None
    assert "identical tool call" in hint


def test_duplicate_successful_write_is_reported_without_reapplying(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sample.py"
    path.write_text("value = 1\n", encoding="utf-8")
    patch = {
        "path": "sample.py",
        "old_text": "value = 1",
        "new_text": "value = 2",
    }
    model = ScriptedModel([])
    runner, store = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=8, mode="act")
    state.recent_actions.append(
        {
            "signature": json.dumps(
                {"name": "apply_patch", "arguments": patch}, sort_keys=True
            ),
            "result_code": "OK",
        }
    )

    asyncio.run(
        runner._handle_tool_calls(
            state, [ToolCall("edit-2", "apply_patch", patch)]
        )
    )

    assert path.read_text(encoding="utf-8") == "value = 1\n"
    duplicate = [
        event
        for event in store.load()
        if event["type"] == "tool_result"
        and event["payload"]["result"]["code"] == "DUPLICATE_TOOL_CALL"
    ]
    assert len(duplicate) == 1
