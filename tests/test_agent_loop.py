from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from time import perf_counter
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
from coding_agent.tools.base import ToolResult, ToolRisk, ToolSpec
from coding_agent.verification_config import VerificationConfig, VerificationRule


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


class CancellationResistantModel:
    """Simulate a transport cleanup path that temporarily ignores cancellation."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
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
            while not self.release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    self.cancel_seen.set()
            return ModelResponse(content="released")
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
    async def observed_hook(_: dict[str, Any]) -> None:
        return None

    async def pre_hook(_: str, __: dict[str, Any]) -> None:
        return None

    async def post_hook(_: str, __: dict[str, Any], ___: ToolResult) -> None:
        return None

    runner.tool_executor.hooks.verification.append(observed_hook)
    runner.tool_executor.hooks.pre.append(pre_hook)
    runner.tool_executor.hooks.post.append(post_hook)
    state = AgentState.create(session_id="session", max_steps=10)

    result = asyncio.run(runner.run_turn(state, "Change value to 2 and verify it"))

    assert result.status is AgentStatus.COMPLETED
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "value = 2\n"
    assert state.verification_is_fresh is True
    assert "sample.py" in state.changed_files
    event_types = [event["type"] for event in store.load()]
    assert "checkpoint_created" in event_types
    assert "tool_result" in event_types
    assert "tool_output_delta" in event_types
    delta = next(event for event in store.load() if event["type"] == "tool_output_delta")
    assert delta["payload"]["coalesced"] is True
    spans = [event for event in store.load() if event["type"] == "span_finished"]
    assert {event["payload"]["kind"] for event in spans} >= {
        "context_build",
        "model_request",
        "approval_wait",
        "hook_pipeline",
        "hook",
    }
    hook_span = next(
        event
        for event in spans
        if event["payload"]["kind"] == "hook"
        and event["payload"]["lifecycle"] == "OnVerification"
    )
    assert hook_span["payload"]["hook"] == "observed_hook"
    assert hook_span["payload"]["duration_ms"] >= 0
    hook_lifecycles = {
        event["payload"]["lifecycle"]
        for event in spans
        if event["payload"]["kind"] == "hook"
    }
    assert {"Pre/PostToolUse", "OnVerification"} <= hook_lifecycles
    assert all(event["payload"]["duration_ms"] >= 0 for event in spans)


def test_agent_accepts_only_scope_selected_custom_verification(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    verify_command = (
        "python -c \"from pathlib import Path; "
        "assert Path('sample.py').read_text() == 'value = 2\\n'\""
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("read", "read_file", {"path": "sample.py"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "edit",
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
                        "verify",
                        "run_command",
                        {"command": verify_command, "purpose": "verify"},
                    )
                ]
            ),
            ModelResponse(content="Changed and verified the value."),
        ]
    )
    runner, _ = _make_runner(tmp_path, model)
    config = VerificationConfig(
        rules=(
            VerificationRule(
                commands=(verify_command,),
                task_profiles=("project",),
                paths=("sample.py",),
            ),
        )
    )
    runner.verification_config = config
    runner.context_manager = ContextManager(verification_config=config)
    state = AgentState.create(session_id="session", max_steps=8, mode="act")

    result = asyncio.run(runner.run_turn(state, "Change sample.py to value 2"))

    assert result.status is AgentStatus.COMPLETED
    assert state.verification_evidence[-1]["accepted"] is True
    assert state.verification_evidence[-1]["source"] == "project_inferred"


def test_agent_rejects_noop_patch_before_checkpoint_or_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sample.py"
    original = "typedef long ll;\n"
    path.write_text(original, encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "sample.py"})]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        "noop",
                        "apply_patch",
                        {
                            "path": "sample.py",
                            "old_text": "typedef long ll;",
                            "new_text": "typedef long ll;",
                        },
                    )
                ]
            ),
            ModelResponse(content="No change was applied."),
        ]
    )
    runner, store = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=4)

    result = asyncio.run(runner.run_turn(state, "Inspect the typedef"))
    events = store.load()

    assert result.status is AgentStatus.COMPLETED
    assert path.read_text(encoding="utf-8") == original
    assert state.changed_files == set()
    assert state.last_mutation_sequence == 0
    assert not any(event["type"] == "checkpoint_created" for event in events)
    noop_result = next(
        event
        for event in events
        if event["type"] == "tool_result" and event["payload"]["id"] == "noop"
    )
    assert noop_result["payload"]["result"]["code"] == "NO_CHANGES"
    assert noop_result["payload"]["result"]["metadata"].get("mutated_files") is None


def test_deepseek_reasoning_state_is_replayed_to_next_tool_round_only_in_memory(
    tmp_path: Path,
) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=[ToolCall("1", "read_file", {"path": "sample.py"})],
                reasoning_content="private provider protocol state",
            ),
            ModelResponse(content="The file contains value = 1."),
        ]
    )
    runner, store = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=3, mode="ask")

    result = asyncio.run(runner.run_turn(state, "Read sample.py"))

    assert result.status is AgentStatus.COMPLETED
    second_request = model.seen_messages[1]
    tool_call_message = next(
        message for message in second_request if message.get("tool_calls")
    )
    assert tool_call_message["reasoning_content"] == (
        "private provider protocol state"
    )
    assert tool_call_message["tool_calls"][0] == {
        "id": "1",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path": "sample.py"}',
        },
    }
    persisted = (tmp_path / ".events" / "session.jsonl").read_text(
        encoding="utf-8"
    )
    assert "private provider protocol state" not in persisted
    assert all(
        "reasoning_content" not in event.get("payload", {})
        for event in store.load()
    )


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


def test_completed_session_accepts_a_second_user_turn(tmp_path: Path) -> None:
    model = ScriptedModel(
        [ModelResponse(content="first answer"), ModelResponse(content="second answer")]
    )
    runner, store = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=2, mode="ask")

    first = asyncio.run(runner.run_turn(state, "first question"))
    first_turn_id = state.turn_id
    second = asyncio.run(runner.run_turn(state, "second question"))

    assert first.status is AgentStatus.COMPLETED
    assert second.status is AgentStatus.COMPLETED
    assert state.turn_id != first_turn_id
    assert [
        event["payload"]["message"]
        for event in store.load()
        if event["type"] == "turn_started"
    ] == ["first question", "second question"]
    assert [message["content"] for message in state.messages if message["role"] == "user"] == [
        "first question",
        "second question",
    ]


def test_unexpected_model_error_finishes_turn_instead_of_silently_dying(
    tmp_path: Path,
) -> None:
    class ExplodingModel:
        async def complete(self, **_: Any) -> ModelResponse:
            raise RuntimeError("unexpected test failure")

    runner, store = _make_runner(tmp_path, ExplodingModel())  # type: ignore[arg-type]
    state = AgentState.create(session_id="session", max_steps=2, mode="ask")

    result = asyncio.run(runner.run_turn(state, "trigger failure"))

    assert result.status is AgentStatus.FAILED
    assert result.message.startswith("UNEXPECTED_AGENT_ERROR: RuntimeError")
    event_types = [event["type"] for event in store.load()]
    assert "run_failed" in event_types
    assert event_types[-1] == "turn_finished"


def test_agent_emits_context_compaction_event(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResponse(content="done")])
    runner, store = _make_runner(tmp_path, model)
    runner.context_manager = ContextManager(max_messages=1)
    state = AgentState.create(session_id="session", max_steps=2)
    state.messages = [{"role": "user", "content": str(index)} for index in range(4)]
    asyncio.run(runner.run_turn(state))
    event_types = [event["type"] for event in store.load()]
    assert "context_built" in event_types
    assert "context_compacted" in event_types


def test_agent_recovers_from_incomplete_tool_history_without_provider_400(
    tmp_path: Path,
) -> None:
    class StrictProtocolModel(ScriptedModel):
        async def complete(self, **kwargs: Any) -> ModelResponse:
            messages = kwargs["messages"]
            assert not any(message.get("tool_calls") for message in messages)
            assert not any(message.get("role") == "tool" for message in messages)
            return await super().complete(**kwargs)

    model = StrictProtocolModel([ModelResponse(content="recovered")])
    runner, _ = _make_runner(tmp_path, model)
    state = AgentState.create(session_id="session", max_steps=2, mode="ask")
    state.messages = [
        {"role": "user", "content": "inspect two files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                ToolCall("call-1", "read_file", {"path": "a.py"}).to_message_dict(),
                ToolCall("call-2", "read_file", {"path": "b.py"}).to_message_dict(),
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_file",
            "content": "partial result before cancellation",
        },
    ]

    result = asyncio.run(runner.run_turn(state, "continue safely"))

    assert result.status is AgentStatus.COMPLETED
    assert result.message == "recovered"


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
    assert [event["type"] for event in events][-2:] == ["session_summary_failed", "turn_finished"]
    assert events[-2]["payload"]["raw_events_preserved"] is True


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


def test_redacted_recovered_approval_never_executes_with_placeholder(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    runner, store = _make_runner(tmp_path, ScriptedModel([]), approval_handler=None)
    state = AgentState.create(session_id="session", max_steps=2)
    state.status = AgentStatus.WAITING_APPROVAL
    state.pending_approval = {
        "call": {
            "id": "write-1",
            "name": "write_file",
            "arguments": {
                "path": "sample.py",
                "content": "[REDACTED]",
            },
        },
        "remaining": [],
        "redacted": True,
    }

    result = asyncio.run(runner.resume_approval(state, approved=True))

    assert result.status is AgentStatus.WAITING_APPROVAL
    assert "REAPPROVAL_REQUIRED" in result.message
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "value = 1\n"
    assert "recovery_reapproval_required" in [event["type"] for event in store.load()]


def test_recovered_approval_can_resume_once_without_replaying_prior_events(tmp_path: Path) -> None:
    runner, store = _make_runner(
        tmp_path,
        ScriptedModel(
            [ModelResponse(content="write applied"), ModelResponse(content="still pending")]
        ),
        approval_handler=None,
    )
    state = AgentState.create(session_id="session", max_steps=2)
    state.restore_from_events(
        [
            {
                "type": "turn_started",
                "turn_id": "turn-1",
                "sequence": 1,
                "payload": {"message": "create output.txt"},
            },
            {
                "type": "approval_requested",
                "turn_id": "turn-1",
                "sequence": 2,
                "payload": {
                    "id": "write-1",
                    "name": "write_file",
                    "arguments": {"path": "output.txt", "content": "hello\n"},
                    "reason": "write requires approval",
                },
            },
        ]
    )

    result = asyncio.run(runner.resume_approval(state, approved=True))

    assert result.status is AgentStatus.PARTIAL
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "hello\n"
    results = [
        event
        for event in store.load()
        if event["type"] == "tool_result"
        and event["payload"].get("id") == "write-1"
    ]
    assert len(results) == 1


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


def test_cancellation_resistant_model_cannot_freeze_turn_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coding_agent.agent_loop.OPERATION_CANCEL_GRACE_SECONDS", 0.05
    )

    async def scenario() -> tuple[AgentRunResult, bool, bool]:
        model = CancellationResistantModel()
        runner, _ = _make_runner(tmp_path, model)  # type: ignore[arg-type]
        state = AgentState.create(session_id="session", max_steps=5)
        task = asyncio.create_task(runner.run_turn(state, "wait forever"))
        await asyncio.wait_for(model.started.wait(), timeout=1)
        runner.cancellation.cancel("test_cancel")
        result = await asyncio.wait_for(asyncio.shield(task), timeout=1)
        cancel_seen = model.cancel_seen.is_set()
        still_cleaning = not model.closed.is_set()
        model.release.set()
        await asyncio.wait_for(model.closed.wait(), timeout=1)
        return result, cancel_seen, still_cleaning

    result, cancel_seen, still_cleaning = asyncio.run(scenario())

    assert result.status is AgentStatus.CANCELLED
    assert cancel_seen is True
    assert still_cleaning is True


def test_model_wait_emits_periodic_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coding_agent.agent_loop.CONTROL_PROGRESS_INTERVAL_SECONDS", 0.01
    )

    async def scenario() -> list[dict[str, Any]]:
        model = BlockingModel(delay=0.04)
        runner, store = _make_runner(tmp_path, model)  # type: ignore[arg-type]
        state = AgentState.create(session_id="session", max_steps=5)
        await runner.run_turn(state, "brief wait")
        return store.load()

    events = asyncio.run(scenario())
    progress = [event for event in events if event["type"] == "model_progress"]

    assert progress
    assert progress[0]["payload"]["elapsed_seconds"] >= 0.0


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


def test_recovery_start_preserves_persisted_run_budget(tmp_path: Path) -> None:
    model = ScriptedModel([])
    runner, _ = _make_runner(tmp_path, model)
    runner.run_budget = RunBudget(max_seconds=20.0, token_limit=200, max_steps=5)
    state = AgentState.create(session_id="session", max_steps=5)
    state.run_budget = {
        "started_at": "2026-08-30T10:00:00+00:00",
        "elapsed_seconds": 7.5,
        "consumed_tokens": 120,
        "max_seconds": 20.0,
        "token_limit": 200,
        "max_steps": 5,
    }

    asyncio.run(runner._start_run_controls(state))

    assert runner.run_budget.active is True
    assert runner.run_budget.consumed_tokens == 120
    assert runner.run_budget.elapsed_seconds >= 7.5


def test_session_token_budget_stops_before_next_model_request(tmp_path: Path) -> None:
    model = ScriptedModel([])
    runner, store = _make_runner(tmp_path, model)
    runner.run_budget = RunBudget(session_token_limit=10, max_steps=5)
    state = AgentState.create(session_id="session", max_steps=5)
    state.token_usage = {"total_tokens": 10}

    result = asyncio.run(runner.run_turn(state, "continue within session"))

    assert result.status is AgentStatus.PARTIAL
    assert result.message.startswith("SESSION_TOKEN_BUDGET_EXHAUSTED")
    assert len(model.seen_messages) == 0
    assert any(
        event["type"] == "run_budget_exhausted"
        and event["payload"]["code"] == "SESSION_TOKEN_BUDGET_EXHAUSTED"
        for event in store.load()
    )


def test_independent_read_calls_run_in_parallel_and_results_keep_call_order(tmp_path: Path) -> None:
    async def scenario() -> tuple[float, list[dict[str, Any]]]:
        registry = ToolRegistry()

        async def delayed_read(arguments: dict[str, Any]) -> ToolResult:
            await asyncio.sleep(float(arguments["delay"]))
            return ToolResult.success(str(arguments["value"]))

        registry.register(
            ToolSpec(
                "delayed_read",
                "Read-only test tool",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "delay": {"type": "number"},
                        "value": {"type": "string"},
                    },
                    "required": ["path", "delay", "value"],
                    "additionalProperties": False,
                },
                ToolRisk.READ,
                delayed_read,
            )
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall("slow", "delayed_read", {"path": "slow", "delay": 0.18, "value": "slow"}),
                        ToolCall("fast", "delayed_read", {"path": "fast", "delay": 0.02, "value": "fast"}),
                    ]
                ),
                ModelResponse(content="done"),
            ]
        )
        store = EventStore(tmp_path / ".events", "parallel")
        runner = AgentRunner(
            model_client=model,
            context_manager=ContextManager(),
            registry=registry,
            tool_executor=ToolExecutor(registry),
            permission_policy=PermissionPolicy(),
            event_bus=EventBus(store),
        )
        started = perf_counter()
        result = await runner.run_turn(AgentState.create(session_id="parallel"), "read both")
        assert result.status is AgentStatus.COMPLETED
        return perf_counter() - started, store.load()

    elapsed, events = asyncio.run(scenario())
    assert elapsed < 0.30
    results = [event["payload"]["id"] for event in events if event["type"] == "tool_result"]
    assert results == ["slow", "fast"]
    requested = [event["payload"].get("execution") for event in events if event["type"] == "tool_requested"]
    assert requested == ["parallel_read", "parallel_read"]
