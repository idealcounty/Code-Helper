from __future__ import annotations

from typing import Any

from coding_agent.session import AgentState

from .base import ToolError, ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry


def register_plan_tools(registry: ToolRegistry, state: AgentState) -> None:
    async def update_plan(arguments: dict[str, Any]) -> ToolResult:
        steps = arguments.get("steps", [])
        if not isinstance(steps, list) or not steps or len(steps) > 12:
            raise ToolError("INVALID_ARGUMENTS", "steps must contain 1 to 12 items")
        normalized: list[dict[str, str]] = []
        in_progress = 0
        for item in steps:
            if not isinstance(item, dict):
                raise ToolError("INVALID_ARGUMENTS", "Each plan step must be an object")
            text = str(item.get("step", "")).strip()
            status = item.get("status", "pending")
            if not text or len(text) > 240:
                raise ToolError("INVALID_ARGUMENTS", "Each step must have 1-240 characters")
            if status not in {"pending", "in_progress", "completed"}:
                raise ToolError("INVALID_ARGUMENTS", "Invalid plan step status")
            in_progress += status == "in_progress"
            normalized.append({"step": text, "status": status})
        if in_progress > 1:
            raise ToolError("INVALID_ARGUMENTS", "Only one plan step may be in_progress")
        state.plan = normalized
        return ToolResult.success(
            "Plan updated.",
            data={"plan": state.plan, "reason": arguments.get("reason", "")},
            metadata={"plan_updated": True},
        )

    registry.register(ToolSpec(
        name="update_plan",
        description="Create or update the visible task plan. Use short steps and mark progress explicitly.",
        parameters={
            "type": "object",
            "properties": {
                "steps": {"type": "array", "description": "Ordered plan steps with step and status fields."},
                "reason": {"type": "string"},
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
        risk=ToolRisk.READ,
        handler=update_plan,
    ))
