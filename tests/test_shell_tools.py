from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry, Workspace, register_shell_tools


def test_command_does_not_inherit_api_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODE_HELPER_API_KEY", "must-not-reach-child")
    registry = ToolRegistry()
    register_shell_tools(registry, Workspace(tmp_path), default_timeout=10)
    executor = ToolExecutor(registry)
    command = (
        f'"{sys.executable}" -c "import os; '
        "print(os.getenv('CODE_HELPER_API_KEY', 'not-present'))\""
    )

    result = asyncio.run(
        executor.execute(
            "run_command", {"command": command, "purpose": "inspect"}
        )
    )

    assert result.ok is True
    assert result.data["stdout"].strip() == "not-present"
    assert "must-not-reach-child" not in result.data["stdout"]

