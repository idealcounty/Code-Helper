from __future__ import annotations

import asyncio
import contextlib
import os
import random
import subprocess
from time import perf_counter
from dataclasses import replace
from typing import Any

from ..algorithm.judge import AlgorithmJudge, JudgeCase, normalize_output, shrink_input_candidates
from ..algorithm.complexity import analyze_file
from ..cancellation import CancellationToken
from .base import ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry
from .shell import _terminate_process_tree
from .workspace import Workspace


MAX_CASES = 256
MAX_EXPERIMENT_CASES = 256
MAX_CASE_INPUT = 100_000
MAX_CASE_OUTPUT = 100_000


def register_algorithm_tools(
    registry: ToolRegistry,
    workspace: Workspace,
    *,
    cancellation: CancellationToken | None = None,
) -> None:
    async def analyze_complexity(arguments: dict[str, Any]) -> ToolResult:
        try:
            path = workspace.resolve(arguments["path"], must_exist=True)
            if path.stat().st_size > 200_000:
                return ToolResult.failure(
                    "FILE_TOO_LARGE",
                    "Complexity analysis is limited to files up to 200000 bytes",
                )
            if not path.is_file():
                return ToolResult.failure("NOT_A_FILE", f"Not a regular file: {arguments['path']}")
            report = analyze_file(path)
        except Exception as exc:
            return ToolResult.failure("COMPLEXITY_ANALYSIS_FAILED", str(exc))
        if report.get("status") != "ok":
            return ToolResult.failure(
                "COMPLEXITY_ANALYSIS_FAILED",
                str(report.get("error") or "Unable to read source file"),
                data={"complexity": report},
            )
        report["path"] = workspace.relative(path)
        return ToolResult.success(
            f"Estimated complexity for {workspace.relative(path)}",
            data={"complexity": report},
        )

    registry.register(
        ToolSpec(
            "analyze_complexity",
            "Estimate loop nesting and recursion for an algorithm source file without modifying it.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            ToolRisk.READ,
            analyze_complexity,
        )
    )

    async def generate_algorithm_cases(arguments: dict[str, Any]) -> ToolResult:
        low = int(arguments.get("min_value", 0))
        high = int(arguments.get("max_value", 100))
        count = min(max(int(arguments.get("random_count", 32)), 0), MAX_EXPERIMENT_CASES)
        seed = int(arguments.get("seed", 0))
        if low > high:
            return ToolResult.failure("INVALID_ARGUMENTS", "min_value must not exceed max_value")
        values: list[tuple[int, str]] = []
        for value in (low, low + 1, 0, 1, high - 1, high):
            if low <= value <= high and all(existing != value for existing, _ in values):
                values.append((value, "boundary"))
        rng = random.Random(seed)
        for _ in range(count):
            values.append((rng.randint(low, high), "random"))
        template = str(arguments.get("input_template") or "{value}\n")
        if "{value}" not in template:
            return ToolResult.failure("INVALID_ARGUMENTS", "input_template must contain {value}")
        cases = [
            {"label": f"{source}-{index + 1}", "input": template.replace("{value}", str(value)), "source": source}
            for index, (value, source) in enumerate(values[:MAX_EXPERIMENT_CASES])
        ]
        return ToolResult.success(
            f"Generated {len(cases)} reproducible algorithm cases",
            data={"cases": cases, "seed": seed, "range": {"min": low, "max": high}},
            metadata={"seed": seed, "boundary_cases": sum(item["source"] == "boundary" for item in cases), "random_cases": sum(item["source"] == "random" for item in cases)},
        )

    registry.register(
        ToolSpec(
            "generate_algorithm_cases",
            "Generate reproducible scalar boundary and random stdin cases from confirmed integer bounds.",
            {
                "type": "object",
                "properties": {
                    "min_value": {"type": "integer"},
                    "max_value": {"type": "integer"},
                    "random_count": {"type": "integer", "default": 32},
                    "seed": {"type": "integer", "default": 0},
                    "input_template": {"type": "string", "default": "{value}\\n"},
                },
                "required": ["min_value", "max_value"],
                "additionalProperties": False,
            },
            ToolRisk.READ,
            generate_algorithm_cases,
        )
    )

    async def run_algorithm_experiment(arguments: dict[str, Any]) -> ToolResult:
        """Run a candidate and a trusted small-input Oracle on the same cases."""
        candidate = str(arguments["candidate_command"])
        oracle = str(arguments["oracle_command"])
        timeout = min(max(float(arguments.get("timeout", 5)), 0.1), 15.0)
        seed = int(arguments.get("seed", 0))
        raw_cases = arguments.get("cases") or []
        if not raw_cases or len(raw_cases) > MAX_EXPERIMENT_CASES:
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                f"cases must contain 1-{MAX_EXPERIMENT_CASES} items",
            )
        results: list[dict[str, Any]] = []
        first_failure: dict[str, Any] | None = None
        minimized_input: str | None = None
        shrink_trace: list[dict[str, int]] = []
        for index, item in enumerate(raw_cases):
            if not isinstance(item, dict):
                return ToolResult.failure("INVALID_ARGUMENTS", f"cases[{index}] must be an object")
            input_data = str(item.get("input", ""))
            if len(input_data) > MAX_CASE_INPUT:
                return ToolResult.failure("OUTPUT_LIMIT", f"cases[{index}] exceeds input limit")
            label = str(item.get("label") or f"case-{index + 1}")
            case_source = str(item.get("source") or "random")
            oracle_output, oracle_status, oracle_detail, oracle_ms = await _timed_run_case(
                oracle, input_data, workspace, timeout, cancellation
            )
            if oracle_status != "ok":
                result = {
                    "label": label,
                    "status": "oracle_error" if oracle_status not in {"timeout", "cancelled"} else oracle_status,
                    "expected": "",
                    "actual": "",
                    "detail": oracle_detail or "Oracle did not produce a usable answer",
                    "input": input_data,
                    "input_size": len(input_data.encode("utf-8")),
                    "case_source": case_source,
                    "oracle_source": "user_command",
                    "oracle_duration_ms": oracle_ms,
                    "duration_ms": 0,
                }
            else:
                actual, status, detail, candidate_ms = await _timed_run_case(
                    candidate, input_data, workspace, timeout, cancellation
                )
                normalized_expected = normalize_output(oracle_output)
                normalized_actual = normalize_output(actual)
                if status == "ok":
                    status = "passed" if normalized_expected == normalized_actual else "wrong_answer"
                    if status == "wrong_answer":
                        detail = "candidate output differs from Oracle"
                elif status == "timeout":
                    status = "time_limit_exceeded"
                result = {
                    "label": label,
                    "status": status,
                    "expected": normalized_expected,
                    "actual": normalized_actual,
                    "detail": detail or "",
                    "input": input_data,
                    "input_size": len(input_data.encode("utf-8")),
                    "case_source": case_source,
                    "oracle_source": "user_command",
                    "oracle_duration_ms": oracle_ms,
                    "duration_ms": candidate_ms,
                }
            results.append(result)
            if first_failure is None and result["status"] != "passed":
                first_failure = result
                if result["status"] == "wrong_answer":
                    minimized_input, shrink_trace = await _minimize_differential_failure(
                        candidate, oracle, input_data, workspace, timeout, cancellation
                    )
            if result["status"] == "cancelled":
                break
        passed = sum(item["status"] == "passed" for item in results)
        failed = len(results) - passed
        durations = sorted(
            float(item["duration_ms"])
            for item in results
            if isinstance(item.get("duration_ms"), (int, float)) and item["duration_ms"] > 0
        )
        curve: list[dict[str, Any]] = []
        by_size: dict[int, list[float]] = {}
        for item in results:
            size = int(item.get("input_size") or 0)
            duration = float(item.get("duration_ms") or 0)
            if duration > 0:
                by_size.setdefault(size, []).append(duration)
        for size, samples in sorted(by_size.items()):
            curve.append({"input_size": size, "samples": len(samples), "p50_ms": _percentile(samples, 0.50), "p95_ms": _percentile(samples, 0.95), "max_ms": max(samples)})
        report = {
            "seed": seed,
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "ok": bool(results) and failed == 0,
            "cases": results,
            "first_failure": first_failure,
            "minimized_input": minimized_input,
            "shrink_trace": shrink_trace,
            "oracle": {"type": "user_command", "command": oracle},
            "benchmark": {
                "samples": len(durations),
                "p50_ms": _percentile(durations, 0.50),
                "p95_ms": _percentile(durations, 0.95),
                "max_ms": max(durations, default=0.0),
                "curve": curve,
            },
        }
        metadata = {
            "purpose": "verify",
            "judge": True,
            "oracle": True,
            "seed": seed,
            "cases": len(results),
            "passed": passed,
        }
        if report["ok"]:
            return ToolResult.success("Differential algorithm experiment passed all cases", data={"judge": report}, metadata=metadata)
        return ToolResult.failure(
            "ALGORITHM_EXPERIMENT_FAILED",
            "Differential experiment found a failing case",
            data={"judge": report},
            metadata=metadata,
        )

    registry.register(
        ToolSpec(
            "run_algorithm_experiment",
            "Compare a candidate command with a user-approved brute-force Oracle on reproducible cases.",
            {
                "type": "object",
                "properties": {
                    "candidate_command": {"type": "string"},
                    "oracle_command": {"type": "string"},
                    "path": {"type": "string", "description": "Optional candidate source path for hashing and static complexity evidence."},
                    "cases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "input": {"type": "string"},
                                "source": {"type": "string"},
                            },
                            "required": ["input"],
                            "additionalProperties": False,
                        },
                    },
                    "seed": {"type": "integer", "default": 0},
                    "timeout": {"type": "number", "default": 5},
                },
                "required": ["candidate_command", "oracle_command", "cases"],
                "additionalProperties": False,
            },
            ToolRisk.COMMAND,
            run_algorithm_experiment,
            timeout=35,
        )
    )

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
        durations: list[float] = []
        for case in cases:
            if cancellation is not None and cancellation.requested:
                outputs.append(("", "cancelled", cancellation.reason))
                break
            actual, status, detail, duration_ms = await _timed_run_case(
                command,
                case.input_data,
                workspace,
                timeout,
                cancellation,
            )
            outputs.append((actual, status, detail))
            durations.append(float(duration_ms))
            if status in {"timeout", "cancelled", "output_limit"}:
                break
        report = AlgorithmJudge(seed=seed).evaluate(cases[: len(outputs)], outputs)
        failed_index = next(
            (index for index, item in enumerate(report.cases) if item.status != "passed"),
            None,
        )
        judge_shrink_trace: list[dict[str, int]] = []
        if failed_index is not None and report.cases[failed_index].status == "wrong_answer":
            minimized, judge_shrink_trace = await _minimize_failure(
                command,
                cases[failed_index],
                workspace,
                timeout,
                cancellation,
            )
            if minimized is not None:
                report = replace(report, minimized_input=minimized)
        report_data = report.to_dict()
        report_data["shrink_trace"] = judge_shrink_trace
        for index, item in enumerate(report_data.get("cases", [])):
            if index < len(cases):
                item["duration_ms"] = durations[index] if index < len(durations) else 0
                item["input_size"] = len(cases[index].input_data.encode("utf-8"))
                item["case_source"] = "explicit"
                item["oracle_source"] = "expected_output"
        positive_durations = [item for item in durations if item > 0]
        by_size: dict[int, list[float]] = {}
        for index, case in enumerate(cases):
            if index < len(durations) and durations[index] > 0:
                by_size.setdefault(len(case.input_data.encode("utf-8")), []).append(durations[index])
        benchmark_curve = [
            {"input_size": size, "samples": len(samples), "p50_ms": _percentile(samples, 0.50), "p95_ms": _percentile(samples, 0.95), "max_ms": max(samples)}
            for size, samples in sorted(by_size.items())
        ]
        report_data["benchmark"] = {
            "samples": len(positive_durations),
            "p50_ms": _percentile(positive_durations, 0.50),
            "p95_ms": _percentile(positive_durations, 0.95),
            "max_ms": max(positive_durations, default=0.0),
            "curve": benchmark_curve,
        }
        metadata = {
            "purpose": "verify",
            "judge": True,
            "seed": seed,
            "cases": report.total,
            "passed": report.passed,
            "p95_ms": report_data["benchmark"]["p95_ms"],
        }
        if report.ok:
            return ToolResult.success("Algorithm judge passed all cases", data={"judge": report_data}, metadata=metadata)
        return ToolResult.failure(
            "ALGORITHM_JUDGE_FAILED",
            "Algorithm judge found a failing case",
            data={"judge": report_data},
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
                    "path": {"type": "string", "description": "Optional candidate source path for hashing and static complexity evidence."},
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
        # ``communicate`` normally waits for the child, but explicitly await
        # the process here as well.  This closes the Windows Proactor
        # transport before the request/event loop is torn down and avoids
        # unclosed-pipe warnings during repeated algorithm runs.
        with contextlib.suppress(ProcessLookupError, asyncio.CancelledError):
            await process.wait()
    if len(stdout) > MAX_CASE_OUTPUT or len(stderr) > MAX_CASE_OUTPUT:
        await _terminate_process_tree(process)
        return "", "output_limit", "case output exceeded limit"
    actual = stdout.decode(errors="replace")
    if process.returncode != 0:
        detail = stderr.decode(errors="replace")[:2_000]
        return actual, "runtime_error", detail or f"exit code {process.returncode}"
    return actual, "ok", None


async def _timed_run_case(
    command: str,
    input_data: str,
    workspace: Workspace,
    timeout: float,
    cancellation: CancellationToken | None,
) -> tuple[str, str, str | None, int]:
    started = perf_counter()
    actual, status, detail = await _run_case(command, input_data, workspace, timeout, cancellation)
    return actual, status, detail, round((perf_counter() - started) * 1000)


async def _minimize_differential_failure(
    candidate: str,
    oracle: str,
    input_data: str,
    workspace: Workspace,
    timeout: float,
    cancellation: CancellationToken | None,
) -> tuple[str, list[dict[str, int]]]:
    current = input_data
    trace = [{"bytes": len(current.encode("utf-8"))}]
    for possible in shrink_input_candidates(input_data):
        expected, oracle_status, _, _ = await _timed_run_case(oracle, possible, workspace, timeout, cancellation)
        actual, candidate_status, _, _ = await _timed_run_case(candidate, possible, workspace, timeout, cancellation)
        if oracle_status == "ok" and candidate_status == "ok" and normalize_output(expected) != normalize_output(actual):
            current = possible
            size = len(current.encode("utf-8"))
            if trace[-1]["bytes"] != size:
                trace.append({"bytes": size})
    return current, trace


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * quantile))))
    return round(values[index], 3)


async def _minimize_failure(
    command: str,
    case: JudgeCase,
    workspace: Workspace,
    timeout: float,
    cancellation: CancellationToken | None,
) -> tuple[str | None, list[dict[str, int]]]:
    """Shrink a wrong-answer input, retaining the original when already minimal."""
    current = case.input_data
    trace = [{"bytes": len(current.encode("utf-8"))}]
    expected = normalize_output(case.expected_output)
    for candidate in shrink_input_candidates(current):
        if cancellation is not None and cancellation.requested:
            break
        actual, status, _ = await _run_case(
            command, candidate, workspace, timeout, cancellation
        )
        if status == "ok" and normalize_output(actual) != expected:
            current = candidate
            size = len(current.encode("utf-8"))
            if trace[-1]["bytes"] != size:
                trace.append({"bytes": size})
    # A one-token boundary case may have no strictly smaller executable input;
    # retain it as the reproducible witness instead of dropping the evidence.
    return current, trace
