from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.agent_loop import AgentRunner
from coding_agent.context import ContextManager
from coding_agent.events import EventBus, EventStore
from coding_agent.permissions import PermissionPolicy
from coding_agent.profiles import classify_task, resolve_profile
from coding_agent.session import AgentState
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry
from coding_agent.tools.base import ToolResult, ToolRisk, ToolSpec
from coding_agent.model import ModelResponse


def test_profile_classifier_is_conservative() -> None:
    assert classify_task("修复项目中的读取 bug") == "project"
    assert classify_task("LeetCode 输入输出，分析时间复杂度并做随机测试") == "algorithm"
    assert resolve_profile("algorithm", "任何内容").name == "algorithm"


def test_algorithm_profile_narrows_tools_and_disables_repo_map(tmp_path: Path) -> None:
    state = AgentState.create(task_profile="algorithm")
    state.task_profile = "algorithm"
    context = ContextManager(workspace=None).build(state, [])
    assert "Algorithm profile" in context.messages[0]["content"]

    registry = ToolRegistry()

    async def handler(_: dict) -> ToolResult:
        return ToolResult.success("ok")

    for name in ("read_file", "get_repo_map"):
        registry.register(
            ToolSpec(
                name,
                name,
                {"type": "object", "properties": {}},
                ToolRisk.READ,
                handler,
            )
        )
    runner = AgentRunner(
        model_client=object(),
        context_manager=ContextManager(),
        registry=registry,
        tool_executor=ToolExecutor(registry),
        permission_policy=PermissionPolicy(),
        event_bus=EventBus(EventStore(tmp_path, "profile-test")),
    )
    schemas = runner._allowed_tool_schemas("act", "algorithm")
    names = {item["function"]["name"] for item in schemas}
    assert names == {"read_file"}


def test_auto_profile_selection_is_event_sourced(tmp_path: Path) -> None:
    class FinalAnswerModel:
        async def complete(self, **_: object) -> ModelResponse:
            return ModelResponse(content="algorithm answer")

    store = EventStore(tmp_path, "profile-run")
    registry = ToolRegistry()
    state = AgentState.create(session_id="profile-run", task_profile="auto")
    runner = AgentRunner(
        model_client=FinalAnswerModel(),
        context_manager=ContextManager(),
        registry=registry,
        tool_executor=ToolExecutor(registry),
        permission_policy=PermissionPolicy(),
        event_bus=EventBus(store),
    )

    result = asyncio.run(
        runner.run_turn(state, "LeetCode 输入输出，分析时间复杂度并做随机测试")
    )

    assert result.status.value == "completed"
    assert state.task_profile == "algorithm"
    event = next(item for item in store.load() if item["type"] == "task_profile_selected")
    assert event["payload"]["profile"] == "algorithm"
