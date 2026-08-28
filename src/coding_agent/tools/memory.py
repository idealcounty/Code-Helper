from __future__ import annotations

from typing import Any

from ..memory import MEMORY_CATEGORIES, MemoryStore
from ..session import AgentState
from .base import ToolError, ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry


def register_memory_tools(
    registry: ToolRegistry,
    store: MemoryStore,
    state: AgentState,
) -> None:
    async def search_memory(arguments: dict[str, Any]) -> ToolResult:
        category = arguments.get("category")
        memories = store.search(
            str(arguments.get("query", "")),
            category=category,
            limit=int(arguments.get("max_results", 6)),
        )
        return ToolResult.success(
            f"Found {len(memories)} relevant project memories.",
            data={"memories": [item.to_dict() for item in memories]},
        )

    async def remember(arguments: dict[str, Any]) -> ToolResult:
        try:
            memory = store.remember(
                category=str(arguments["category"]),
                content=str(arguments["content"]),
                keywords=arguments.get("keywords", []),
                importance=arguments.get("importance", 3),
                source_session_id=state.session_id,
                source_turn_id=state.turn_id,
                memory_id=arguments.get("memory_id"),
            )
        except ValueError as exc:
            raise ToolError("INVALID_ARGUMENTS", str(exc)) from exc
        return ToolResult.success(
            "Project memory saved.",
            data={"memory": memory.to_dict()},
            metadata={"memory_updated": True},
        )

    async def forget(arguments: dict[str, Any]) -> ToolResult:
        memory_id = str(arguments["memory_id"])
        if not store.forget(memory_id):
            raise ToolError("MEMORY_NOT_FOUND", f"Unknown project memory: {memory_id}")
        return ToolResult.success(
            "Project memory forgotten.",
            data={"memory_id": memory_id},
            metadata={"memory_updated": True},
        )

    category_schema = {"type": "string", "enum": sorted(MEMORY_CATEGORIES)}
    registry.register(
        ToolSpec(
            name="search_project_memory",
            description="Search durable project facts, decisions, preferences, and pending tasks saved across conversations.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": ""},
                    "category": category_schema,
                    "max_results": {"type": "integer", "default": 6},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=ToolRisk.READ,
            handler=search_memory,
        )
    )
    registry.register(
        ToolSpec(
            name="remember_project_memory",
            description="Persist one user-confirmed project fact, decision, preference, or follow-up task for future conversations. Do not save guesses or transient details.",
            parameters={
                "type": "object",
                "properties": {
                    "category": category_schema,
                    "content": {"type": "string"},
                    "keywords": {"type": "array"},
                    "importance": {"type": "integer", "default": 3},
                    "memory_id": {"type": "string", "description": "Existing id when correcting a memory."},
                },
                "required": ["category", "content"],
                "additionalProperties": False,
            },
            risk=ToolRisk.WRITE,
            handler=remember,
        )
    )
    registry.register(
        ToolSpec(
            name="forget_project_memory",
            description="Deactivate one durable project memory by id after the user asks to forget or correct it.",
            parameters={
                "type": "object",
                "properties": {"memory_id": {"type": "string"}},
                "required": ["memory_id"],
                "additionalProperties": False,
            },
            risk=ToolRisk.DESTRUCTIVE,
            handler=forget,
        )
    )
