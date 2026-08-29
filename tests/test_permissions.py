from pathlib import Path

from coding_agent.permissions import (
    PermissionDecision,
    PermissionPolicy,
    ToolCapability,
)
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


def test_permission_result_exposes_capabilities_and_special_command_risks() -> None:
    policy = PermissionPolicy()
    spec = ToolSpec(
        "run_command", "run", {"type": "object", "properties": {}}, ToolRisk.COMMAND, _noop
    )

    result = policy.evaluate(
        mode="act",
        spec=spec,
        arguments={"command": "pip install requests https://example.test", "timeout": 90},
    )

    assert result.decision is PermissionDecision.ASK
    assert set(result.capabilities) == {
        ToolCapability.PROCESS_EXEC,
        ToolCapability.NETWORK_EGRESS,
        ToolCapability.DEPENDENCY_INSTALL,
    }


def test_workspace_boundary_is_checked_before_approval() -> None:
    policy = PermissionPolicy(workspace_root=Path("D:/workspace"))
    spec = _spec(ToolRisk.WRITE)

    result = policy.evaluate(
        mode="act", spec=spec, arguments={"path": "../outside.txt"}
    )

    assert result.decision is PermissionDecision.DENY
    assert ToolCapability.PATH_OUTSIDE_WORKSPACE in result.capabilities


def test_scoped_session_grant_is_time_limited_and_path_bound() -> None:
    policy = PermissionPolicy(workspace_root=Path("D:/workspace"))
    spec = _spec(ToolRisk.WRITE)
    grant = policy.grant(
        [ToolCapability.WORKSPACE_WRITE],
        path_prefix="src",
        ttl_seconds=60,
    )

    allowed = policy.evaluate(
        mode="act", spec=spec, arguments={"path": "src/app.py"}
    )
    denied = policy.evaluate(
        mode="act", spec=spec, arguments={"path": "tests/app.py"}
    )

    assert allowed.decision is PermissionDecision.ALLOW
    assert denied.decision is PermissionDecision.ASK
    assert policy.revoke(grant.grant_id) is True
    assert policy.evaluate(
        mode="act", spec=spec, arguments={"path": "src/app.py"}
    ).decision is PermissionDecision.ASK


def test_power_shell_style_recursive_delete_is_denied() -> None:
    policy = PermissionPolicy()
    spec = ToolSpec(
        "run_command", "run", {"type": "object", "properties": {}}, ToolRisk.COMMAND, _noop
    )

    result = policy.evaluate(
        mode="act", spec=spec, arguments={"command": "ri -Recurse -Force .\\build"}
    )

    assert result.decision is PermissionDecision.DENY
