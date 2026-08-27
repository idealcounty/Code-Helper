from __future__ import annotations

import asyncio
import os
from typing import Any

from .base import ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry
from .workspace import Workspace


def register_shell_tools(
    registry: ToolRegistry,
    workspace: Workspace,
    *,
    default_timeout: float = 60.0,
) -> None:
    async def run_command(arguments: dict[str, Any]) -> ToolResult:
        command = arguments["command"]
        timeout = min(float(arguments.get("timeout", default_timeout)), 300.0)
        purpose = arguments.get("purpose", "other")

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_sanitized_environment(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult.failure(
                "COMMAND_TIMEOUT",
                f"Command exceeded {timeout:g} seconds",
                metadata={"purpose": purpose, "timeout": timeout},
            )

        stdout = _decode_and_truncate(stdout_bytes)
        stderr = _decode_and_truncate(stderr_bytes)
        exit_code = int(process.returncode or 0)
        result_data = {
            "command": command,
            "stdout": stdout[0],
            "stderr": stderr[0],
            "exit_code": exit_code,
        }
        metadata = {
            "purpose": purpose,
            "stdout_truncated": stdout[1],
            "stderr_truncated": stderr[1],
            "verification_passed": purpose == "verify" and exit_code == 0,
        }
        if exit_code == 0:
            return ToolResult.success(
                "Command completed successfully", data=result_data, metadata=metadata
            )
        return ToolResult.failure(
            "COMMAND_FAILED",
            f"Command exited with code {exit_code}",
            data=result_data,
            metadata=metadata,
        )

    registry.register(
        ToolSpec(
            "run_command",
            "Run a command in the workspace. Use purpose='verify' for tests, builds, or checks.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "purpose": {
                        "type": "string",
                        "enum": ["inspect", "verify", "other"],
                        "default": "other",
                    },
                    "timeout": {"type": "number", "default": default_timeout},
                },
                "required": ["command", "purpose"],
                "additionalProperties": False,
            },
            ToolRisk.COMMAND,
            run_command,
            timeout=default_timeout + 5,
        )
    )


def _decode_and_truncate(data: bytes, limit: int = 12_000) -> tuple[str, bool]:
    text = data.decode(errors="replace")
    if len(text) <= limit:
        return text, False
    head = text[:8_000]
    tail = text[-4_000:]
    return f"{head}\n\n[... output truncated ...]\n\n{tail}", True


def _sanitized_environment() -> dict[str, str]:
    sensitive_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in sensitive_markers)
    }
