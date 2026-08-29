from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from coding_agent.agent_loop import AgentRunResult, AgentRunner
from coding_agent.budget import RunBudget
from coding_agent.checkpoints import CheckpointManager
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


class BlockingModel:
    def __init__(self, delay: float = 60.0) -> None:
        self.delay = delay
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
            return ModelResponse(content="too late")
        finally:
            self.closed.set()


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
        checkpoint_manager=CheckpointManager(workspace),
    )
    return runner, store


def test_agent_reads_edits_verifies_and_finishes(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(
        "from pathlib import Path\n\n"
        "def test_value():\n"
        "    assert Path('sample.py').read_text() == 'value = 2\\n'\n",
        encoding="utf-8",
    )
    verify_command = "python -m pytest -q test_sample.py"
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
    event_types = [event["type"] for event in store.load()]
    assert "checkpoint_created" in event_types
    assert "tool_result" in event_types


def test_live_reducer_and_event_replay_have_matching_projection(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResponse(content="done")])
    runner, store = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=2)

    result = asyncio.run(runner.run_turn(state, "finish safely"))
    recovered = AgentState.create(session_id="session", max_steps=2)
    recovered.restore_from_events(store.load())

    assert result.status is AgentStatus.COMPLETED
    assert recovered.status is state.status
    assert recovered.turn_id == state.turn_id
    assert recovered.step == state.step
    assert recovered.current_objective == state.current_objective
    assert recovered.messages == state.messages
    assert recovered.token_usage == state.token_usage
    assert recovered.tool_stats == state.tool_stats
    stable_budget_keys = {
        "max_seconds",
        "max_steps",
        "token_limit",
        "consumed_tokens",
        "consumed_steps",
        "exhausted_code",
    }
    assert {
        key: recovered.run_budget.get(key) for key in stable_budget_keys
    } == {key: state.run_budget.get(key) for key in stable_budget_keys}


def test_agent_emits_context_compaction_event(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResponse(content="done")])
    runner, store = _make_runner(tmp_path, model)
    runner.context_manager = ContextManager(max_messages=1)
    state = AgentState.create(session_id="session", max_steps=2)
    state.messages = [{"role": "user", "content": str(index)} for index in range(4)]
    asyncio.run(runner.run_turn(state))
    assert "context_compacted" in [event["type"] for event in store.load()]


def test_summary_failure_preserves_completed_turn_and_raw_events(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResponse(content="done")])
    runner, store = _make_runner(tmp_path, model)

    def broken_summary(*_: Any) -> dict[str, Any]:
        raise OSError("summary storage unavailable")

    runner.turn_summarizer = broken_summary
    state = AgentState.create(session_id="session", max_steps=2)
    result = asyncio.run(runner.run_turn(state, "finish safely"))
    events = store.load()

    assert result.status is AgentStatus.COMPLETED
    assert [event["type"] for event in events][-2:] == ["turn_finished", "session_summary_failed"]
    assert events[-1]["payload"]["raw_events_preserved"] is True


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


def test_agent_rejects_echo_as_fake_verification(tmp_path: Path) -> None:
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
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "3",
                        "run_command",
                        {"command": "echo ok", "purpose": "verify"},
                    )
                ]
            ),
            ModelResponse(content="Done."),
            ModelResponse(content="Still done."),
            ModelResponse(content="No stronger verification."),
        ]
    )
    runner, store = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=10)

    result = asyncio.run(runner.run_turn(state, "Change value to 2"))

    assert result.status is AgentStatus.PARTIAL
    assert state.verification_is_fresh is False
    assert state.last_successful_verification_sequence == 0
    assert state.verification_evidence[-1]["accepted"] is False
    assert "not verification" in state.verification_evidence[-1]["reason"]
    assert "verification_recorded" in [event["type"] for event in store.load()]


def test_agent_accepts_user_requested_custom_verification(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    command = "python -c \"from pathlib import Path; assert Path('sample.py').exists()\""
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
                        {"command": command, "purpose": "verify"},
                    )
                ]
            ),
            ModelResponse(content="Verified with the requested command."),
        ]
    )
    runner, _ = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=10)

    result = asyncio.run(
        runner.run_turn(state, f"Change value to 2, then run `{command}`")
    )

    assert result.status is AgentStatus.COMPLETED
    assert state.verification_evidence[-1]["accepted"] is True
    assert state.verification_evidence[-1]["source"] == "user_requested"


def test_agent_can_suspend_and_resume_approval(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(
        "from pathlib import Path\n\n"
        "def test_value():\n"
        "    assert Path('sample.py').read_text() == 'value = 2\\n'\n",
        encoding="utf-8",
    )
    verify_command = "python -m pytest -q test_sample.py"
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


def test_shared_cancellation_interrupts_active_model_request(tmp_path: Path) -> None:
    async def scenario() -> tuple[AgentRunResult, list[dict[str, Any]], bool]:
        model = BlockingModel()
        runner, store = _make_runner(tmp_path, model)  # type: ignore[arg-type]
        state = AgentState.create(session_id="session", max_steps=5)
        task = asyncio.create_task(runner.run_turn(state, "wait forever"))
        await asyncio.wait_for(model.started.wait(), timeout=1)
        runner.cancellation.cancel("test_cancel")
        result = await asyncio.wait_for(task, timeout=1)
        return result, store.load(), model.closed.is_set()

    result, events, closed = asyncio.run(scenario())

    assert result.status is AgentStatus.CANCELLED
    assert result.message == "RUN_CANCELLED: test_cancel"
    assert closed is True
    event_types = [event["type"] for event in events]
    assert "run_cancelled" in event_types
    assert event_types[-1] == "turn_finished"


def test_wall_time_budget_interrupts_active_operation(tmp_path: Path) -> None:
    async def scenario() -> tuple[AgentRunResult, list[dict[str, Any]], bool]:
        model = BlockingModel()
        runner, store = _make_runner(tmp_path, model)  # type: ignore[arg-type]
        runner.run_budget = RunBudget(max_seconds=0.05, max_steps=5)
        state = AgentState.create(session_id="session", max_steps=5)
        result = await asyncio.wait_for(runner.run_turn(state, "slow task"), timeout=1)
        return result, store.load(), model.closed.is_set()

    result, events, closed = asyncio.run(scenario())

    assert result.status is AgentStatus.PARTIAL
    assert result.message.startswith("TIME_BUDGET_EXHAUSTED")
    assert closed is True
    exhausted = next(event for event in events if event["type"] == "run_budget_exhausted")
    assert exhausted["payload"]["code"] == "TIME_BUDGET_EXHAUSTED"


def test_token_budget_stops_before_requested_tools_execute(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("1", "read_file", {"path": "missing.py"})],
                usage={"total_tokens": 10},
            )
        ]
    )
    runner, store = _make_runner(tmp_path, model)
    runner.run_budget = RunBudget(token_limit=10, max_steps=5)
    state = AgentState.create(session_id="session", max_steps=5)

    result = asyncio.run(runner.run_turn(state, "do not exceed budget"))

    assert result.status is AgentStatus.PARTIAL
    assert result.message.startswith("TOKEN_BUDGET_EXHAUSTED")
    assert state.run_budget["consumed_tokens"] == 10
    event_types = [event["type"] for event in store.load()]
    assert "tool_started" not in event_types
    assert "run_budget_exhausted" in event_types
