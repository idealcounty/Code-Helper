from __future__ import annotations

import asyncio

from coding_agent.session import AgentState
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry
from coding_agent.tools.plan import register_plan_tools


def _executor() -> tuple[ToolExecutor, AgentState]:
    state = AgentState.create()
    registry = ToolRegistry()
    register_plan_tools(registry, state)
    return ToolExecutor(registry), state


def test_update_plan_preserves_valid_acceptance() -> None:
    executor, state = _executor()
    result = asyncio.run(executor.execute(
        "update_plan",
        {"steps": [{"step": "实现负数处理", "status": "in_progress", "acceptance": "回归测试通过"}]},
    ))

    assert result.ok
    assert state.plan == [{
        "step": "实现负数处理",
        "status": "in_progress",
        "acceptance": "回归测试通过",
    }]


def test_update_plan_rejects_empty_or_overlong_acceptance() -> None:
    executor, _ = _executor()
    for acceptance in ("", "x" * 301):
        result = asyncio.run(executor.execute(
            "update_plan",
            {"steps": [{"step": "实现功能", "acceptance": acceptance}]},
        ))
        assert result.code == "INVALID_ARGUMENTS"


def test_update_plan_rejects_non_string_acceptance() -> None:
    executor, _ = _executor()
    result = asyncio.run(executor.execute(
        "update_plan",
        {"steps": [{"step": "实现功能", "acceptance": 1}]},
    ))
    assert result.code == "INVALID_ARGUMENTS"


def test_update_plan_schema_describes_acceptance_criteria() -> None:
    state = AgentState.create()
    registry = ToolRegistry()
    register_plan_tools(registry, state)
    schema = registry.get("update_plan").parameters
    step_schema = schema["properties"]["steps"]["items"]
    assert step_schema["properties"]["acceptance"]["maxLength"] == 300
