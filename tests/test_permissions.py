from coding_agent.permissions import PermissionDecision, PermissionPolicy
from coding_agent.tools.base import ToolRisk, ToolSpec, ToolResult


async def _noop(_: dict) -> ToolResult:
    return ToolResult.success("ok")


def _spec(risk: ToolRisk) -> ToolSpec:
    return ToolSpec("demo", "demo", {"type": "object", "properties": {}}, risk, _noop)


def test_destructive_commands_are_denied_in_act_mode() -> None:
    policy = PermissionPolicy()
    result = policy.evaluate(
        mode="act",
        spec=ToolSpec("run_command", "run", {"type": "object", "properties": {}}, ToolRisk.COMMAND, _noop),
        arguments={"command": "git reset --hard HEAD"},
    )
    assert result.decision is PermissionDecision.DENY


def test_write_and_command_modes_are_denied_outside_act() -> None:
    policy = PermissionPolicy()
    assert policy.evaluate(mode="ask", spec=_spec(ToolRisk.WRITE), arguments={}).decision is PermissionDecision.DENY
    assert policy.evaluate(mode="plan", spec=_spec(ToolRisk.COMMAND), arguments={"command": "pytest"}).decision is PermissionDecision.DENY


def test_read_tools_are_allowed_in_all_modes() -> None:
    policy = PermissionPolicy()
    for mode in ("ask", "plan", "act"):
        assert policy.evaluate(mode=mode, spec=_spec(ToolRisk.READ), arguments={}).decision is PermissionDecision.ALLOW
