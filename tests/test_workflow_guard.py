from __future__ import annotations

import asyncio

from coding_agent.config import AppConfig
from coding_agent.runtime import create_workflow_guard
from coding_agent.session import AgentState
from coding_agent.runtime import create_runtime


def _decision(state: AgentState, tool: str):
    guard = create_workflow_guard(state)
    return asyncio.run(guard(tool, {"path": "app.py"}))


def test_add_feature_requires_a_plan_before_write() -> None:
    state = AgentState.create()
    state.workflow_name = "add-feature"
    state.workflow_stage = "inspect"

    decision = _decision(state, "write_file")

    assert decision.allow is False
    assert decision.code == "WORKFLOW_PLAN_REQUIRED"


def test_add_feature_requires_one_in_progress_step_before_write() -> None:
    state = AgentState.create()
    state.workflow_name = "add-feature"
    state.workflow_stage = "plan"
    state.plan = [{"step": "实现功能", "status": "completed", "acceptance": "测试通过"}]

    decision = _decision(state, "apply_patch")

    assert decision.allow is False
    assert decision.code == "WORKFLOW_STEP_REQUIRED"


def test_code_review_is_read_only() -> None:
    state = AgentState.create()
    state.workflow_name = "code-review"
    state.workflow_stage = "inspect"

    decision = _decision(state, "write_file")

    assert decision.allow is False
    assert decision.code == "WORKFLOW_DENIED"


def test_bug_fix_allows_write_to_reach_existing_permission_policy() -> None:
    state = AgentState.create()
    state.workflow_name = "bug-fix"
    state.workflow_stage = "implement"

    decision = _decision(state, "apply_patch")

    assert decision is None or decision.allow is True


def test_finished_workflow_rejects_new_write() -> None:
    state = AgentState.create()
    state.workflow_name = "bug-fix"
    state.workflow_stage = "finish"

    decision = _decision(state, "write_file")

    assert decision.allow is False
    assert decision.code == "WORKFLOW_FINISHED"


def test_runtime_registers_guard_before_write_handler(tmp_path) -> None:
    runtime = create_runtime(
        config=AppConfig(api_key="test-key"),
        workspace_path=tmp_path,
        mode="act",
        model_client=object(),
    )
    runtime.state.workflow_name = "code-review"
    runtime.state.workflow_stage = "inspect"

    result = asyncio.run(runtime.tool_executor.execute(
        "write_file", {"path": "should-not-exist.txt", "content": "blocked"}
    ))

    assert result.ok is False
    assert result.code == "WORKFLOW_DENIED"
    assert not (tmp_path / "should-not-exist.txt").exists()
