from __future__ import annotations

import asyncio
from typing import Any

from .base import ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry
from .workspace import Workspace


def register_git_tools(registry: ToolRegistry, workspace: Workspace) -> None:
    async def get_diff(arguments: dict[str, Any]) -> ToolResult:
        staged = arguments.get("staged", False)
        args = ["git", "diff", "--no-color"]
        if staged:
            args.append("--cached")
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=workspace.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=10
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult.failure("COMMAND_TIMEOUT", "git diff timed out")

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        data = {
            "diff": _truncate(stdout, 100_000),
            "stderr": _truncate(stderr, 2_000),
            "staged": staged,
            "truncated": len(stdout) > 100_000,
        }
        if process.returncode != 0:
            return ToolResult.failure(
                "COMMAND_FAILED",
                f"git diff exited with code {process.returncode}",
                data=data,
            )
        return ToolResult.success(
            "Diff is empty" if not stdout else "Diff collected",
            data=data,
        )

    registry.register(
        ToolSpec(
            "get_diff",
            "Return the current git diff for the workspace.",
            {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean", "default": False},
                },
                "required": [],
                "additionalProperties": False,
            },
            ToolRisk.READ,
            get_diff,
            timeout=15,
        )
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]
