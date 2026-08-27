from __future__ import annotations

import asyncio
import sys
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from coding_agent.agent_loop import AgentRunner
from coding_agent.context import ContextManager
from coding_agent.events import EventBus, EventStore
from coding_agent.model import ModelResponse, ToolCall
from coding_agent.permissions import PermissionPolicy
from coding_agent.session import AgentState, AgentStatus
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import (
    ToolRegistry,
    Workspace,
    register_filesystem_tools,
    register_shell_tools,
)


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = deque(responses)
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        self.seen_messages.append(messages)
        if not self.responses:
            raise AssertionError("Scripted model ran out of responses")
        return self.responses.popleft()


async def _approve_all(*_: Any) -> bool:
    return True


def _make_runner(
    tmp_path: Path,
    model: ScriptedModel,
    *,
    approval_handler=_approve_all,
) -> tuple[AgentRunner, EventStore]:
    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    register_filesystem_tools(registry, workspace)
    register_shell_tools(registry, workspace, default_timeout=10)
    store = EventStore(tmp_path / ".events", "session")
    runner = AgentRunner(
        model_client=model,
        context_manager=ContextManager(),
        registry=registry,
        tool_executor=ToolExecutor(registry),
        permission_policy=PermissionPolicy(),
        event_bus=EventBus(store),
        approval_handler=approval_handler,
    )
    return runner, store


def test_agent_reads_edits_verifies_and_finishes(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    verify_command = (
        f'"{sys.executable}" -c "from pathlib import Path; '
        "assert Path('sample.py').read_text() == 'value = 2\\n'\""
    )
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("1", "read_file", {"path": "sample.py"})]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "2",
                        "apply_patch",
                        {
                            "path": "sample.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "3",
                        "run_command",
                        {"command": verify_command, "purpose": "verify"},
                    )
                ]
            ),
            ModelResponse(content="Changed the value and verified it."),
        ]
    )
    runner, store = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=10)

    result = asyncio.run(runner.run_turn(state, "Change value to 2 and verify it"))

    assert result.status is AgentStatus.COMPLETED
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "value = 2\n"
    assert state.verification_is_fresh is True
    assert "sample.py" in state.changed_files
    assert any(event["type"] == "tool_result" for event in store.load())


def test_agent_does_not_finish_with_stale_verification(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("1", "read_file", {"path": "sample.py"})]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "2",
                        "apply_patch",
                        {
                            "path": "sample.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                    )
                ]
            ),
            ModelResponse(content="Done without testing."),
            ModelResponse(content="Still done."),
            ModelResponse(content="No verification available."),
        ]
    )
    runner, _ = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=10)

    result = asyncio.run(runner.run_turn(state, "Change value to 2"))

    assert result.status is AgentStatus.PARTIAL
    assert "verification" in result.message.lower()


def test_agent_can_suspend_and_resume_approval(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    verify_command = (
        f'"{sys.executable}" -c "from pathlib import Path; '
        "assert Path('sample.py').read_text() == 'value = 2\\n'\""
    )
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("1", "read_file", {"path": "sample.py"})]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "2",
                        "apply_patch",
                        {
                            "path": "sample.py",
                            "old_text": "value = 1",
                            "new_text": "value = 2",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "3",
                        "run_command",
                        {"command": verify_command, "purpose": "verify"},
                    )
                ]
            ),
            ModelResponse(content="Verified."),
        ]
    )
    runner, _ = _make_runner(tmp_path, model, approval_handler=None)
    state = AgentState.create(session_id="session", max_steps=10)

    waiting_for_edit = asyncio.run(runner.run_turn(state, "Change value to 2"))
    assert waiting_for_edit.status is AgentStatus.WAITING_APPROVAL
    assert state.pending_approval is not None

    waiting_for_command = asyncio.run(runner.resume_approval(state, approved=True))
    assert waiting_for_command.status is AgentStatus.WAITING_APPROVAL

    completed = asyncio.run(runner.resume_approval(state, approved=True))
    assert completed.status is AgentStatus.COMPLETED
