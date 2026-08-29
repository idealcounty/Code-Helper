from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import shlex
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any

from ..cancellation import CancellationToken
from .base import ToolError, ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry
from .workspace import Workspace


def register_shell_tools(
    registry: ToolRegistry,
    workspace: Workspace,
    *,
    default_timeout: float = 60.0,
    cancellation: CancellationToken | None = None,
) -> None:
    async def run_command(arguments: dict[str, Any]) -> ToolResult:
        argv = arguments.get("argv")
        command = str(arguments.get("command") or "")
        execution_mode = "argv" if argv is not None else "shell"
        display_command = _display_command(command, argv)
        timeout = min(float(arguments.get("timeout", default_timeout)), 300.0)
        purpose = arguments.get("purpose", "other")

        if cancellation is not None and cancellation.requested:
            return ToolResult.failure(
                "COMMAND_CANCELLED",
                f"Command was not started because the run was cancelled: {cancellation.reason}",
                metadata={"purpose": purpose, "termination": "cancelled"},
            )

        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        process_kwargs = {
            "cwd": workspace.root,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": _sanitized_environment(),
            **process_options,
        }
        if argv is not None:
            process = await asyncio.create_subprocess_exec(*argv, **process_kwargs)
        else:
            process = await asyncio.create_subprocess_shell(command, **process_kwargs)
        output_callback = arguments.get("_output_callback")
        communicate_task = asyncio.create_task(
            _communicate_with_deltas(process, output_callback)
        )
        cancel_task = (
            asyncio.create_task(cancellation.wait()) if cancellation is not None else None
        )
        try:
            waiters = {communicate_task}
            if cancel_task is not None:
                waiters.add(cancel_task)
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task is not None and cancel_task in done:
                terminated = await _terminate_process_tree(process)
                stdout_bytes, stderr_bytes = await _finish_communication(
                    communicate_task
                )
                data, output_metadata = _command_output(
                    display_command, stdout_bytes, stderr_bytes, process.returncode
                )
                return ToolResult.failure(
                    "COMMAND_CANCELLED",
                    f"Command cancelled: {cancellation.reason}",
                    data=data,
                    metadata={
                        "purpose": purpose,
                        "termination": "cancelled",
                        "process_tree_terminated": terminated,
                        **output_metadata,
                        "output_streamed": output_callback is not None,
                    },
                )
            if communicate_task not in done:
                terminated = await _terminate_process_tree(process)
                stdout_bytes, stderr_bytes = await _finish_communication(
                    communicate_task
                )
                data, output_metadata = _command_output(
                    display_command, stdout_bytes, stderr_bytes, process.returncode
                )
                return ToolResult.failure(
                    "COMMAND_TIMEOUT",
                    f"Command exceeded {timeout:g} seconds",
                    data=data,
                    metadata={
                        "purpose": purpose,
                        "timeout": timeout,
                        "termination": "timeout",
                        "process_tree_terminated": terminated,
                        **output_metadata,
                        "output_streamed": output_callback is not None,
                    },
                )
            stdout_bytes, stderr_bytes = communicate_task.result()
        except asyncio.CancelledError:
            await _terminate_process_tree(process)
            await _finish_communication(communicate_task)
            raise
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

        data, output_metadata = _command_output(
            display_command, stdout_bytes, stderr_bytes, process.returncode
        )
        exit_code = data["exit_code"]
        metadata = {
            "purpose": purpose,
            "execution_mode": execution_mode,
            "termination": "completed",
            "output_streamed": output_callback is not None,
            **output_metadata,
        }
        if exit_code == 0:
            return ToolResult.success(
                "Command completed successfully", data=data, metadata=metadata
            )
        return ToolResult.failure(
            "COMMAND_FAILED",
            f"Command exited with code {exit_code}",
            data=data,
            metadata=metadata,
        )

    registry.register(
        ToolSpec(
            "run_command",
            "Run a command in the workspace. Use purpose='verify' only for real tests, builds, lint, typechecks, compiles, or an exact user-requested check; informational commands do not verify changes.",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Legacy shell command; prefer argv for a structured invocation.",
                    },
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Executable and arguments, run without a shell.",
                    },
                    "purpose": {
                        "type": "string",
                        "enum": ["inspect", "verify", "other"],
                        "default": "other",
                    },
                    "timeout": {"type": "number", "default": default_timeout},
                },
                "required": ["purpose"],
                "additionalProperties": False,
            },
            ToolRisk.COMMAND,
            run_command,
            timeout=default_timeout + 5,
            validator=_validate_command_arguments,
        )
    )


def _validate_command_arguments(arguments: dict[str, Any]) -> None:
    command = arguments.get("command")
    argv = arguments.get("argv")
    if (command is None) == (argv is None):
        raise ToolError(
            "INVALID_ARGUMENTS",
            "Provide exactly one of 'argv' (preferred) or 'command'",
        )
    if command is not None and not str(command).strip():
        raise ToolError("INVALID_ARGUMENTS", "command must not be empty")
    if argv is not None:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ToolError(
                "INVALID_ARGUMENTS",
                "argv must contain a non-empty executable and string arguments",
            )
        if any("\x00" in item for item in argv):
            raise ToolError("INVALID_ARGUMENTS", "argv cannot contain NUL bytes")


def _display_command(command: str, argv: list[str] | None) -> str:
    if argv is None:
        return command
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


async def _finish_communication(
    task: asyncio.Task[tuple[bytes, bytes]],
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return b"", b""


async def _communicate_with_deltas(
    process: asyncio.subprocess.Process,
    callback: Callable[[str, str], Awaitable[None] | None] | None,
) -> tuple[bytes, bytes]:
    """Read both streams incrementally while retaining bytes for the final result."""
    async def read_stream(stream: asyncio.StreamReader | None, name: str) -> bytes:
        if stream is None:
            return b""
        chunks: list[bytes] = []
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if callback is not None:
                try:
                    value = callback(name, chunk.decode(errors="replace"))
                    if hasattr(value, "__await__"):
                        await value
                except Exception:
                    # Output observers must never change command success or cancellation.
                    pass
        return b"".join(chunks)

    stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
    stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    await process.wait()
    return stdout, stderr


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> bool:
    if process.returncode is not None:
        return True
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        else:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
        return True
    except (OSError, ProcessLookupError, TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return False


def _command_output(
    command: str,
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    returncode: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stdout = _decode_and_truncate(stdout_bytes)
    stderr = _decode_and_truncate(stderr_bytes)
    return (
        {
            "command": command,
            "stdout": stdout[0],
            "stderr": stderr[0],
            "exit_code": int(returncode if returncode is not None else -1),
        },
        {
            "stdout_truncated": stdout[1],
            "stderr_truncated": stderr[1],
            "_full_stdout": stdout[2] if stdout[1] else "",
            "_full_stderr": stderr[2] if stderr[1] else "",
        },
    )


def _decode_and_truncate(data: bytes, limit: int = 12_000) -> tuple[str, bool, str]:
    text = data.decode(errors="replace")
    if len(text) <= limit:
        return text, False, ""
    head = text[:8_000]
    tail = text[-4_000:]
    return f"{head}\n\n[... output truncated ...]\n\n{tail}", True, text


def _sanitized_environment() -> dict[str, str]:
    sensitive_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in sensitive_markers)
    }
