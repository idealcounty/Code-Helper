from __future__ import annotations

from typing import Any

from ..memory import MEMORY_CATEGORIES, MemoryStore
from ..memory_summary import SessionSummaryStore
from ..session import AgentState
from ..user_memory import UserMemoryService
from .base import ToolError, ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry


def register_memory_tools(
    registry: ToolRegistry,
    store: MemoryStore,
    state: AgentState,
    summary_store: SessionSummaryStore | None = None,
) -> None:
    async def search(arguments: dict[str, Any]) -> ToolResult:
        found = store.search_detailed(
            str(arguments.get("query", "")),
            category=arguments.get("category"),
            limit=int(arguments.get("max_results", 6)),
            file_path=arguments.get("file_path"),
            symbol=arguments.get("symbol"),
            source_session_id=arguments.get("source_session_id"),
        )
        serialized = [{**item, "memory": item["memory"].to_dict()} for item in found]
        return ToolResult.success(f"Found {len(found)} relevant project memories.", data={"memories": serialized})

    async def remember(arguments: dict[str, Any]) -> ToolResult:
        try:
            memory = store.remember(
                category=str(arguments["category"]), content=str(arguments["content"]),
                keywords=arguments.get("keywords", []), importance=arguments.get("importance", 3),
                source_session_id=state.session_id, source_turn_id=state.turn_id,
                memory_id=arguments.get("memory_id"), subject=str(arguments.get("subject", "")),
                file_paths=arguments.get("file_paths", []), symbols=arguments.get("symbols", []),
            )
        except ValueError as exc:
            raise ToolError("INVALID_ARGUMENTS", str(exc)) from exc
        return ToolResult.success("Project memory saved.", data={"memory": memory.to_dict()}, metadata={"memory_updated": True})

    async def forget(arguments: dict[str, Any]) -> ToolResult:
        memory_id = str(arguments["memory_id"])
        if not store.forget(memory_id):
            raise ToolError("MEMORY_NOT_FOUND", f"Unknown project memory: {memory_id}")
        return ToolResult.success("Project memory forgotten.", data={"memory_id": memory_id}, metadata={"memory_updated": True})

    async def list_candidates(arguments: dict[str, Any]) -> ToolResult:
        candidates = [] if summary_store is None else summary_store.candidates(
            status=str(arguments.get("status", "pending")), limit=int(arguments.get("max_results", 50))
        )
        return ToolResult.success(f"Found {len(candidates)} memory candidates.", data={"candidates": candidates})

    async def confirm_candidate(arguments: dict[str, Any]) -> ToolResult:
        candidate = None if summary_store is None else summary_store.confirm(str(arguments["candidate_id"]), store)
        if candidate is None:
            raise ToolError("MEMORY_CANDIDATE_NOT_FOUND", "Unknown or resolved memory candidate")
        return ToolResult.success("Memory candidate confirmed and saved.", data={"candidate": candidate}, metadata={"memory_updated": True})

    async def reject_candidate(arguments: dict[str, Any]) -> ToolResult:
        candidate = None if summary_store is None else summary_store.reject(str(arguments["candidate_id"]))
        if candidate is None:
            raise ToolError("MEMORY_CANDIDATE_NOT_FOUND", "Unknown or resolved memory candidate")
        return ToolResult.success("Memory candidate rejected.", data={"candidate": candidate})

    category = {"type": "string", "enum": sorted(MEMORY_CATEGORIES)}
    specs = [
        ToolSpec(name="search_project_memory", description="Search project memory with optional file, symbol, category, and source-session filters. Results expose conflicts and repository evidence.", parameters={"type": "object", "properties": {"query": {"type": "string", "default": ""}, "category": category, "max_results": {"type": "integer", "default": 6}, "file_path": {"type": "string"}, "symbol": {"type": "string"}, "source_session_id": {"type": "string"}}, "required": ["query"], "additionalProperties": False}, risk=ToolRisk.READ, handler=search),
        ToolSpec(name="remember_project_memory", description="Persist one user-confirmed project memory. Use subject to make contradictory records visible.", parameters={"type": "object", "properties": {"category": category, "content": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}, "importance": {"type": "integer", "default": 3}, "memory_id": {"type": "string"}, "subject": {"type": "string"}, "file_paths": {"type": "array", "items": {"type": "string"}}, "symbols": {"type": "array", "items": {"type": "string"}}}, "required": ["category", "content"], "additionalProperties": False}, risk=ToolRisk.WRITE, handler=remember),
        ToolSpec(name="forget_project_memory", description="Deactivate one project memory by id after the user asks to forget it.", parameters={"type": "object", "properties": {"memory_id": {"type": "string"}}, "required": ["memory_id"], "additionalProperties": False}, risk=ToolRisk.DESTRUCTIVE, handler=forget),
        ToolSpec(name="list_memory_candidates", description="List turn-summary facts proposed for memory but not automatically saved.", parameters={"type": "object", "properties": {"status": {"type": "string", "enum": ["pending", "confirmed", "rejected"], "default": "pending"}, "max_results": {"type": "integer", "default": 50}}, "additionalProperties": False}, risk=ToolRisk.READ, handler=list_candidates),
        ToolSpec(name="confirm_memory_candidate", description="Confirm and save a proposed memory only after explicit user approval.", parameters={"type": "object", "properties": {"candidate_id": {"type": "string"}}, "required": ["candidate_id"], "additionalProperties": False}, risk=ToolRisk.WRITE, handler=confirm_candidate),
        ToolSpec(name="reject_memory_candidate", description="Reject a proposed memory so it cannot be recalled later.", parameters={"type": "object", "properties": {"candidate_id": {"type": "string"}}, "required": ["candidate_id"], "additionalProperties": False}, risk=ToolRisk.WRITE, handler=reject_candidate),
    ]
    for spec in specs:
        registry.register(spec)


def register_user_memory_tools(registry: ToolRegistry, service: UserMemoryService, state: AgentState) -> None:
    def require_enabled() -> None:
        if not service.enabled:
            raise ToolError("USER_MEMORY_DISABLED", "User memory is disabled; the user must explicitly enable it first")

    async def set_enabled(arguments: dict[str, Any]) -> ToolResult:
        enabled = arguments["enabled"]
        if not isinstance(enabled, bool):
            raise ToolError("INVALID_ARGUMENTS", "enabled must be a boolean")
        service.set_enabled(enabled)
        return ToolResult.success("User memory enabled." if enabled else "User memory disabled.", data={"enabled": enabled}, metadata={"memory_updated": True})

    async def search(arguments: dict[str, Any]) -> ToolResult:
        require_enabled()
        memories = service.search(str(arguments.get("query", "")), limit=int(arguments.get("max_results", 6)))
        return ToolResult.success(f"Found {len(memories)} user memories.", data={"memories": [item.to_dict() for item in memories]})

    async def remember(arguments: dict[str, Any]) -> ToolResult:
        require_enabled()
        try:
            memory = service.store.remember(category=str(arguments["category"]), content=str(arguments["content"]), keywords=arguments.get("keywords", []), importance=arguments.get("importance", 3), source_session_id=state.session_id, source_turn_id=state.turn_id, scope="user")
        except ValueError as exc:
            raise ToolError("INVALID_ARGUMENTS", str(exc)) from exc
        return ToolResult.success("User memory saved.", data={"memory": memory.to_dict()}, metadata={"memory_updated": True})

    async def export(arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success("User memory export ready.", data=service.export())

    async def clear(arguments: dict[str, Any]) -> ToolResult:
        require_enabled()
        count = service.clear()
        return ToolResult.success(f"Cleared {count} user memories.", data={"cleared": count}, metadata={"memory_updated": True})

    category = {"type": "string", "enum": sorted(MEMORY_CATEGORIES)}
    specs = [
        ToolSpec(name="set_user_memory_enabled", description="Enable or disable global user memory only when explicitly requested.", parameters={"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"], "additionalProperties": False}, risk=ToolRisk.WRITE, handler=set_enabled),
        ToolSpec(name="search_user_memory", description="Search enabled cross-project user preferences.", parameters={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 6}}, "required": ["query"], "additionalProperties": False}, risk=ToolRisk.READ, handler=search),
        ToolSpec(name="remember_user_memory", description="Save cross-project memory only after explicit instruction and while enabled.", parameters={"type": "object", "properties": {"category": category, "content": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}, "importance": {"type": "integer", "default": 3}}, "required": ["category", "content"], "additionalProperties": False}, risk=ToolRisk.WRITE, handler=remember),
        ToolSpec(name="export_user_memory", description="Export user-memory records and enablement state.", parameters={"type": "object", "properties": {}, "additionalProperties": False}, risk=ToolRisk.READ, handler=export),
        ToolSpec(name="clear_user_memory", description="Clear all user memories after explicit confirmation.", parameters={"type": "object", "properties": {}, "additionalProperties": False}, risk=ToolRisk.DESTRUCTIVE, handler=clear),
    ]
    for spec in specs:
        registry.register(spec)
