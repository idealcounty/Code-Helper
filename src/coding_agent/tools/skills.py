from __future__ import annotations

from coding_agent.skills import SkillLibrary

from .base import ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry


def register_skill_tools(registry: ToolRegistry, library: SkillLibrary) -> None:
    registry.register(ToolSpec(
        name="list_skills",
        description="List available project skills with descriptions and trigger conditions.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.READ,
        handler=_list_skills(library),
    ))
    registry.register(ToolSpec(
        name="load_skill",
        description="Load the full instructions for one named project skill after deciding it applies.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        risk=ToolRisk.READ,
        handler=_load_skill(library),
    ))


def _list_skills(library: SkillLibrary):
    async def handler(_: dict) -> ToolResult:
        skills = [item.to_dict() for item in library.list_summaries()]
        return ToolResult.success(f"Found {len(skills)} project skill(s).", data={"skills": skills})

    return handler


def _load_skill(library: SkillLibrary):
    async def handler(arguments: dict) -> ToolResult:
        loaded = library.load(arguments["name"])
        if loaded is None:
            return ToolResult.failure("SKILL_NOT_FOUND", f"Skill not found: {arguments['name']}")
        summary, content = loaded
        return ToolResult.success(
            f"Loaded skill {summary.name}.",
            data={"skill": summary.to_dict(), "content": content},
        )

    return handler
