import asyncio
from pathlib import Path


from coding_agent.session import AgentState
from coding_agent.skills import SkillLibrary
from coding_agent.tools import ToolRegistry, Workspace
from coding_agent.tools.plan import register_plan_tools
from coding_agent.tools.skills import register_skill_tools
from coding_agent.tool_executor import ToolExecutor
from coding_agent.hooks import HookDecision, HookManager
from coding_agent.tools.base import ToolResult
from coding_agent.context import ContextManager
from coding_agent.verifier import CompletionStatus, Verifier
from coding_agent.model import ModelResponse


def test_skill_library_lists_and_loads_safely(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bug-fix"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "description: Fix defects\nwhen_to_use: A failing test\n\n# Steps\n", encoding="utf-8"
    )
    library = SkillLibrary(tmp_path)
    assert library.list_summaries()[0].description == "Fix defects"
    summary, content = library.load("bug-fix") or (None, "")
    assert summary is not None and "# Steps" in content
    assert library.load("../bug-fix") is None


def test_plan_tool_updates_state_and_rejects_multiple_active_steps() -> None:
    state = AgentState.create()
    registry = ToolRegistry()
    register_plan_tools(registry, state)
    executor = ToolExecutor(registry)
    result = asyncio.run(executor.execute(
        "update_plan",
        {"steps": [{"step": "Inspect", "status": "in_progress"}]},
    ))
    assert result.ok and state.plan[0]["status"] == "in_progress"
    invalid = asyncio.run(executor.execute(
        "update_plan",
        {"steps": [
            {"step": "A", "status": "in_progress"},
            {"step": "B", "status": "in_progress"},
        ]},
    ))
    assert invalid.code == "INVALID_ARGUMENTS"


def test_skill_tools_are_read_only_and_load_content(tmp_path: Path) -> None:
    skill_dir = tmp_path / "review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("description: Review\n", encoding="utf-8")
    registry = ToolRegistry()
    register_skill_tools(registry, SkillLibrary(tmp_path))
    executor = ToolExecutor(registry)
    result = asyncio.run(executor.execute("load_skill", {"name": "review"}))
    assert result.ok and result.data["content"]


def test_tool_executor_runs_pre_and_post_hooks() -> None:
    state = AgentState.create()
    registry = ToolRegistry()
    register_plan_tools(registry, state)
    calls: list[str] = []

    async def pre(name: str, _: dict) -> None:
        calls.append(f"pre:{name}")

    async def post(name: str, _: dict, result: ToolResult) -> ToolResult:
        calls.append(f"post:{name}")
        result.metadata["hooked"] = True
        return result

    executor = ToolExecutor(registry, HookManager(pre=[pre], post=[post]))
    result = asyncio.run(executor.execute("update_plan", {"steps": [{"step": "x"}]}))
    assert result.ok and result.metadata["hooked"]
    assert calls == ["pre:update_plan", "post:update_plan"]


def test_pre_hook_can_deny_without_bypassing_executor() -> None:
    registry = ToolRegistry()
    register_plan_tools(registry, AgentState.create())

    def deny(_: str, __: dict) -> HookDecision:
        return HookDecision(allow=False, reason="policy test", hook="deny")

    executor = ToolExecutor(registry, HookManager(pre=[deny]))
    result = asyncio.run(
        executor.execute("update_plan", {"steps": [{"step": "must not run"}]})
    )

    assert result.ok is False
    assert result.code == "HOOK_DENIED"


def test_lifecycle_hooks_return_structured_decisions() -> None:
    async def verification(_: dict) -> HookDecision:
        return HookDecision(additional_context="inspect the failed test", hook="verification")

    async def task_end(_: dict) -> None:
        return None

    manager = HookManager(verification=[verification], task_end=[task_end])
    decisions = asyncio.run(manager.on_verification({"accepted": False}))
    finished = asyncio.run(manager.on_task_end({"status": "partial"}))

    assert decisions[0].additional_context == "inspect the failed test"
    assert decisions[0].hook == "verification"
    assert finished[0].allow is True


def test_state_restores_conversation_and_plan_from_events() -> None:
    state = AgentState.create(session_id="session-1")
    state.restore_from_events([
        {"type": "turn_started", "turn_id": "turn-1", "payload": {"message": "Fix it"}},
        {"type": "plan_updated", "turn_id": "turn-1", "payload": {"plan": [{"step": "Inspect", "status": "completed"}]}},
        {
            "type": "context_compacted",
            "turn_id": "turn-1",
            "payload": {
                "summary": "old context",
                "summary_meta": {"version": 1, "covered_event_sequence": 7},
            },
        },
        {"type": "repair_attempt", "turn_id": "turn-1", "payload": {"attempt": 2}},
        {"type": "turn_finished", "turn_id": "turn-1", "payload": {"status": "completed", "token_usage": {"total_tokens": 4}}},
    ])
    assert state.turn_id == "turn-1"
    assert state.messages[0]["content"] == "Fix it"
    assert state.plan[0]["status"] == "completed"
    assert state.token_usage["total_tokens"] == 4
    assert state.context_summary == "old context"
    assert state.context_summary_meta["covered_event_sequence"] == 7
    assert state.repair_attempts == 2
    state.restore_from_events([])
    assert state.messages == [] and state.plan == []


def test_context_manager_bounds_history() -> None:
    state = AgentState.create()
    state.messages = [{"role": "user", "content": str(index)} for index in range(5)]
    context = ContextManager(max_messages=2).build(state, [])
    assert context.messages[1]["content"].startswith("Context summary v1")
    assert "Objective" in context.messages[1]["content"]
    assert context.context_summary_meta["version"] == 1
    assert [item["content"] for item in context.messages[-2:]] == ["3", "4"]
    assert context.truncated and state.context_summary


def test_tool_executor_persists_full_output_reference(tmp_path: Path) -> None:
    from coding_agent.tools.shell import register_shell_tools
    registry = ToolRegistry()
    register_shell_tools(registry, Workspace(tmp_path))
    executor = ToolExecutor(registry, result_store=tmp_path / ".code-helper" / "tool-results")
    result = asyncio.run(executor.execute("run_command", {"command": "python -c \"print('x' * 13000)\"", "purpose": "inspect"}))
    assert result.ok and result.data.get("result_reference")
    assert list((tmp_path / ".code-helper" / "tool-results").glob("*.json"))


def test_verifier_stops_after_bounded_repair_attempts() -> None:
    state = AgentState.create()
    state.changed_files.add("app.py")
    state.repair_attempts = state.max_repair_attempts
    decision = Verifier().evaluate(state, ModelResponse(content="done"))
    assert decision.status is CompletionStatus.PARTIAL
    assert "repair attempts" in decision.reason
