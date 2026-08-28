import asyncio
from pathlib import Path


from coding_agent.session import AgentState
from coding_agent.skills import SkillLibrary
from coding_agent.tools import ToolRegistry
from coding_agent.tools.plan import register_plan_tools
from coding_agent.tools.skills import register_skill_tools
from coding_agent.tool_executor import ToolExecutor
from coding_agent.hooks import HookManager
from coding_agent.tools.base import ToolResult


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
