from __future__ import annotations

from typing import Any

from coding_agent.permissions import PermissionDecision, PermissionPolicy
from coding_agent.tools.base import ToolResult, ToolRisk, ToolSpec


async def _unused(_: dict[str, Any]) -> ToolResult:
    return ToolResult.success("unused")


def _spec(name: str, risk: ToolRisk) -> ToolSpec:
    return ToolSpec(
        name,
        "test",
        {"type": "object", "properties": {}},
        risk,
        _unused,
    )


def test_read_tools_are_allowed() -> None:
    result = PermissionPolicy().evaluate(
        mode="ask", spec=_spec("read_file", ToolRisk.READ), arguments={}
    )
    assert result.decision is PermissionDecision.ALLOW


def test_write_tools_are_denied_outside_act_mode() -> None:
    result = PermissionPolicy().evaluate(
        mode="plan", spec=_spec("apply_patch", ToolRisk.WRITE), arguments={}
    )
    assert result.decision is PermissionDecision.DENY


def test_destructive_command_is_denied() -> None:
    result = PermissionPolicy().evaluate(
        mode="act",
        spec=_spec("run_command", ToolRisk.COMMAND),
        arguments={"command": "git reset --hard"},
    )
    assert result.decision is PermissionDecision.DENY


def test_normal_command_requires_approval() -> None:
    result = PermissionPolicy().evaluate(
        mode="act",
        spec=_spec("run_command", ToolRisk.COMMAND),
        arguments={"command": "pytest -q"},
    )
    assert result.decision is PermissionDecision.ASK
