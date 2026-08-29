from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from dataclasses import replace
from typing import Any

from ..algorithm.judge import AlgorithmJudge, JudgeCase, normalize_output, shrink_input_candidates
from ..cancellation import CancellationToken
from .base import ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry
from .shell import _terminate_process_tree
from .workspace import Workspace


MAX_CASES = 100
MAX_CASE_INPUT = 100_000
MAX_CASE_OUTPUT = 100_000


def register_algorithm_tools(
    registry: ToolRegistry,
    workspace: Workspace,
    *,
    cancellation: CancellationToken | None = None,
) -> None:
    async def judge_algorithm(arguments: dict[str, Any]) -> ToolResult:
        command = str(arguments["command"])
        timeout = min(max(float(arguments.get("timeout", 5)), 0.1), 30.0)
        seed = int(arguments.get("seed", 0))
        raw_cases = arguments.get("cases") or []
        if not raw_cases or len(raw_cases) > MAX_CASES:
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                f"cases must contain 1-{MAX_CASES} items",
            )
        cases: list[JudgeCase] = []
        for index, item in enumerate(raw_cases):
            if not isinstance(item, dict):
                return ToolResult.failure("INVALID_ARGUMENTS", f"cases[{index}] must be an object")
            input_data = str(item.get("input", ""))
            expected = str(item.get("expected", ""))
            if len(input_data) > MAX_CASE_INPUT or len(expected) > MAX_CASE_OUTPUT:
                return ToolResult.failure("OUTPUT_LIMIT", f"cases[{index}] exceeds input/output limits")
            cases.append(JudgeCase(input_data, expected, str(item.get("label") or f"case-{index + 1}")))

        outputs: list[tuple[str, str, str | None]] = []
        for case in cases:
            if cancellation is not None and cancellation.requested:
                outputs.append(("", "cancelled", cancellation.reason))
                break
            actual, status, detail = await _run_case(
                command,
                case.input_data,
                workspace,
                timeout,
                cancellation,
            )
            outputs.append((actual, status, detail))
            if status in {"timeout", "cancelled", "output_limit"}:
                break
        report = AlgorithmJudge(seed=seed).evaluate(cases[: len(outputs)], outputs)
        failed_index = next(
            (index for index, item in enumerate(report.cases) if item.status != "passed"),
            None,
        )
        if failed_index is not None and report.cases[failed_index].status == "wrong_answer":
            minimized = await _minimize_failure(
                command,
                cases[failed_index],
                workspace,
                timeout,
                cancellation,
            )
            if minimized is not None:
                report = replace(report, minimized_input=minimized)
        metadata = {
            "purpose": "verify",
            "judge": True,
            "seed": seed,
            "cases": report.total,
            "passed": report.passed,
        }
        if report.ok:
            return ToolResult.success("Algorithm judge passed all cases", data={"judge": report.to_dict()}, metadata=metadata)
        return ToolResult.failure(
            "ALGORITHM_JUDGE_FAILED",
            "Algorithm judge found a failing case",
            data={"judge": report.to_dict()},
            metadata=metadata,
        )

    registry.register(
        ToolSpec(
            "judge_algorithm",
            "Run a candidate algorithm command against deterministic stdin/expected-output cases with a fixed seed.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "input": {"type": "string"},
                                "expected": {"type": "string"},
                            },
                            "required": ["input", "expected"],
                            "additionalProperties": False,
                        },
                    },
                    "seed": {"type": "integer", "default": 0},
                    "timeout": {"type": "number", "default": 5},
                },
                "required": ["command", "cases"],
                "additionalProperties": False,
            },
            ToolRisk.COMMAND,
            judge_algorithm,
            timeout=35,
        )
    )


async def _run_case(
    command: str,
    input_data: str,
    workspace: Workspace,
    timeout: float,
    cancellation: CancellationToken | None,
) -> tuple[str, str, str | None]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=workspace.root,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            name: value
            for name, value in os.environ.items()
            if not any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
        },
        **options,
    )
    communication = asyncio.create_task(process.communicate(input_data.encode()))
    cancel_wait = asyncio.create_task(cancellation.wait()) if cancellation is not None else None
    try:
        waiters = {communication}
        if cancel_wait is not None:
            waiters.add(cancel_wait)
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if cancel_wait is not None and cancel_wait in done:
            await _terminate_process_tree(process)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(communication), timeout=2)
            return "", "cancelled", cancellation.reason
        if communication not in done:
            await _terminate_process_tree(process)
            communication.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communication
            return "", "timeout", f"exceeded {timeout:g} seconds"
        stdout, stderr = communication.result()
    finally:
        if cancel_wait is not None:
            cancel_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_wait
    if len(stdout) > MAX_CASE_OUTPUT or len(stderr) > MAX_CASE_OUTPUT:
        await _terminate_process_tree(process)
        return "", "output_limit", "case output exceeded limit"
    actual = stdout.decode(errors="replace")
    if process.returncode != 0:
        detail = stderr.decode(errors="replace")[:2_000]
        return actual, "runtime_error", detail or f"exit code {process.returncode}"
    return actual, "ok", None


async def _minimize_failure(
    command: str,
    case: JudgeCase,
    workspace: Workspace,
    timeout: float,
    cancellation: CancellationToken | None,
) -> str | None:
    """Shrink a wrong-answer input while re-running the same candidate command."""
    current = case.input_data
    expected = normalize_output(case.expected_output)
    for candidate in shrink_input_candidates(current):
        if cancellation is not None and cancellation.requested:
            break
        actual, status, _ = await _run_case(
            command, candidate, workspace, timeout, cancellation
        )
        if status == "ok" and normalize_output(actual) != expected:
            current = candidate
    return current if current != case.input_data else None
