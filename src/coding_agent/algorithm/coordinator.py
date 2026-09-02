"""Deterministic orchestration for the Algorithm Reliability Lab.

The normal Agent Loop is intentionally model driven.  Algorithm validation is
different: once a candidate command and (optionally) an Oracle are known, the
remaining work is deterministic.  This coordinator keeps that work outside
the model loop so the UI can start a run directly and receive useful progress
events without waiting for another reasoning request at every stage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

from ..cancellation import CancellationToken, RunCancelled
from ..events import AgentEvent, EventBus
from ..tools.algorithm import (
    MAX_CASE_INPUT,
    MAX_CASE_OUTPUT,
    _timed_run_case,
    normalize_output,
    shrink_input_candidates,
)
from ..tools.base import ToolResult
from ..tools.workspace import Workspace
from .complexity import analyze_file
from .problem import parse_problem, suggest_boundary_cases
from .reliability import build_report, persist_report


RunProfile = Literal["quick", "standard", "full"]
RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


PROFILE_DEFAULTS: dict[RunProfile, dict[str, Any]] = {
    "quick": {
        "max_cases": 16,
        "max_random_cases": 8,
        "parallelism": 2,
        "timeout": 3.0,
        "shrink": False,
        "benchmark_repeats": 0,
    },
    "standard": {
        "max_cases": 64,
        "max_random_cases": 32,
        "parallelism": 4,
        "timeout": 5.0,
        "shrink": True,
        "benchmark_repeats": 0,
    },
    "full": {
        "max_cases": 256,
        "max_random_cases": 128,
        "parallelism": 4,
        "timeout": 8.0,
        "shrink": True,
        "benchmark_repeats": 2,
    },
}


@dataclass(frozen=True, slots=True)
class AlgorithmRunConfig:
    candidate_command: str
    oracle_command: str = ""
    candidate_path: str = ""
    cases: tuple[dict[str, Any], ...] = ()
    problem_text: str = ""
    profile: RunProfile = "standard"
    seed: int = 0
    timeout: float | None = None
    shrink: bool | None = None
    benchmark: bool | None = None

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(PROFILE_DEFAULTS[self.profile])


class AlgorithmRunCoordinator:
    """Run algorithm validation without calling a model.

    A coordinator instance represents one run.  It is safe to observe through
    ``progress_callback`` while ``run`` is executing and to cancel it through
    the shared ``CancellationToken``.
    """

    def __init__(
        self,
        *,
        workspace: Workspace,
        event_bus: EventBus,
        session_id: str,
        cancellation: CancellationToken,
        progress_callback: ProgressCallback | None = None,
        run_id: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.event_bus = event_bus
        self.session_id = session_id
        self.cancellation = cancellation
        self.progress_callback = progress_callback
        self.run_id = run_id or uuid4().hex
        self.turn_id = f"algorithm-run-{self.run_id}"
        self.started_at = perf_counter()
        self._cache_directory = workspace.root / ".code-helper" / "algorithm-cache"
        self._cache_directory.mkdir(parents=True, exist_ok=True)
        self._last_spec: dict[str, Any] | None = None
        self._shrink_cache_hit = False

    async def run(self, config: AlgorithmRunConfig) -> dict[str, Any]:
        """Execute a bounded validation run and persist an existing report shape."""

        self.started_at = perf_counter()
        await self._progress("queued", 0, "算法实验已排队", profile=config.profile)
        try:
            self._validate_config(config)
            defaults = config.defaults
            timeout = max(0.1, min(float(config.timeout or defaults["timeout"]), 30.0))
            max_cases = int(defaults["max_cases"])
            cases = self._build_cases(config, max_cases=max_cases)
            if not cases:
                return await self._finish_failure(
                    config,
                    "INVALID_CASES",
                    "至少提供一条可执行测试用例，或提供可解析题面",
                )

            await self._progress("preparing", 8, f"准备 {len(cases)} 条测试用例", cases=len(cases))
            candidate_command, compile_info = await self._prepare_command(config)
            if self.cancellation.requested:
                raise RunCancelled(self.cancellation.reason)
            if compile_info.get("error"):
                return await self._finish_failure(
                    config, "COMPILE_FAILED", str(compile_info["error"]), compile_info=compile_info
                )
            await self._progress("compiling", 18, str(compile_info.get("message") or "候选程序已准备"), compile=compile_info)

            sources = {str(item.get("source") or "explicit") for item in cases}
            if "boundary" in sources:
                await self._progress("boundary_testing", 30, "正在执行边界用例", total=len(cases))
            if "random" in sources:
                await self._progress("random_testing", 42, "正在执行固定 Seed 随机用例", seed=config.seed)

            if config.oracle_command.strip():
                results, cache_stats = await self._run_differential(
                    candidate_command,
                    config.oracle_command.strip(),
                    cases,
                    timeout=timeout,
                    parallelism=int(defaults["parallelism"]),
                    fail_fast=True,
                    seed=config.seed,
                    source_signature=self._source_signature(config.candidate_path),
                    oracle_signature=self._command_source_signature(config.oracle_command),
                )
                oracle_type = "user_command"
            else:
                results, cache_stats = await self._run_expected(
                    candidate_command,
                    cases,
                    timeout=timeout,
                    parallelism=int(defaults["parallelism"]),
                    fail_fast=True,
                    seed=config.seed,
                    source_signature=self._source_signature(config.candidate_path),
                )
                oracle_type = "expected_output"

            if self.cancellation.requested:
                raise RunCancelled(self.cancellation.reason)

            await self._progress(
                "testing",
                68,
                f"完成 {len(results)} 条用例：{sum(item['status'] == 'passed' for item in results)} 通过",
                completed=len(results),
                total=len(cases),
                cache=cache_stats,
            )
            first_failure = next((item for item in results if item.get("status") != "passed"), None)
            minimized: str | None = None
            shrink_trace: list[dict[str, int]] = []
            do_shrink = config.shrink if config.shrink is not None else bool(defaults["shrink"])
            if first_failure and first_failure.get("status") == "wrong_answer" and do_shrink:
                minimized, shrink_trace = await self._shrink(
                    candidate_command,
                    config.oracle_command.strip() if oracle_type == "user_command" else "",
                    first_failure,
                    timeout=timeout,
                    limit=8 if config.profile == "quick" else 16 if config.profile == "standard" else 32,
                    source_signature=self._source_signature(config.candidate_path),
                    oracle_signature=self._command_source_signature(config.oracle_command),
                )

            benchmark = self._benchmark(results)
            do_benchmark = config.benchmark if config.benchmark is not None else bool(defaults["benchmark_repeats"])
            if do_benchmark and not first_failure:
                await self._progress("benchmarking", 82, "正确性通过，正在采集性能样本", cache=cache_stats)
                benchmark = await self._benchmark_repeats(
                    candidate_command,
                    cases,
                    timeout=timeout,
                    repeats=int(defaults["benchmark_repeats"]),
                )

            passed = sum(item.get("status") == "passed" for item in results)
            failed = len(results) - passed
            judge = {
                "seed": config.seed,
                "total": len(results),
                "passed": passed,
                "failed": failed,
                "ok": bool(results) and failed == 0,
                "cases": results,
                "first_failure": first_failure,
                "minimized_input": minimized,
                "shrink_trace": shrink_trace,
                "oracle": {"type": oracle_type, "command": config.oracle_command.strip()},
                "benchmark": benchmark,
            }
            arguments = {
                "command": candidate_command,
                "candidate_command": candidate_command,
                "oracle_command": config.oracle_command.strip(),
                "path": config.candidate_path,
                "cases": cases,
                "seed": config.seed,
            }
            tool_result = (
                ToolResult.success("Deterministic algorithm run passed all cases", data={"judge": judge}, metadata={"purpose": "verify"})
                if not first_failure
                else ToolResult.failure("ALGORITHM_RUN_FAILED", "Deterministic algorithm run found a failing case", data={"judge": judge}, metadata={"purpose": "verify"})
            )
            await self._progress("reporting", 94, "正在生成可靠性报告", cache=cache_stats)
            report = build_report(
                session_id=self.session_id,
                turn_id=self.turn_id,
                step=0,
                event_sequence=self.event_bus.sequence,
                arguments=arguments,
                result=tool_result.to_dict(),
                complexity=self._complexity(config.candidate_path),
                workspace_root=self.workspace.root,
            )
            if report is None:
                return await self._finish_failure(config, "REPORT_FAILED", "无法生成算法报告")
            report["run"] = {
                "run_id": self.run_id,
                "profile": config.profile,
                "model_requests": 0,
                "cache": cache_stats,
                "compile_cache": {
                    "hit": bool(compile_info.get("cache_hit")),
                    "key": compile_info.get("cache_key"),
                },
                "shrink_cache_hit": self._shrink_cache_hit,
                "duration_ms": round((perf_counter() - self.started_at) * 1000, 3),
                "stages": self._report_stages(cases, compile_info, first_failure, do_benchmark),
                "metrics": {
                    "cases_executed": len(results),
                    "processes_started": len(results) * (2 if oracle_type == "user_command" else 1),
                    "cache_hits": cache_stats.get("hits", 0),
                    "cache_misses": cache_stats.get("misses", 0),
                    "compile_cache_hit": int(bool(compile_info.get("cache_hit"))),
                    "shrink_cache_hit": int(self._shrink_cache_hit),
                    "compile_ms": round(float(compile_info.get("duration_ms") or 0), 3),
                    "candidate_execution_total_ms": round(sum(float(item.get("duration_ms") or 0) for item in results), 3),
                    "oracle_execution_total_ms": round(sum(float(item.get("oracle_duration_ms") or 0) for item in results), 3),
                    "candidate_process_count": len(results),
                    "oracle_process_count": len(results) if oracle_type == "user_command" else 0,
                    "shrink_attempts": max(0, len(shrink_trace) - 1),
                    "model_requests": 0,
                },
            }
            if self._last_spec is not None:
                report["problem_spec"] = self._last_spec
            report["source"]["run_id"] = self.run_id
            report_path = persist_report(self.workspace.root, report)
            await self.event_bus.publish(
                AgentEvent(
                    type="algorithm_run_completed" if not first_failure else "algorithm_run_failed",
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    payload={
                        "run_id": self.run_id,
                        "report_id": report["report_id"],
                        "profile": config.profile,
                        "status": "completed" if not first_failure else "failed",
                        "summary": report["summary"],
                        "cache": cache_stats,
                        "model_requests": 0,
                        "path": str(report_path.relative_to(self.workspace.root)),
                    },
                )
            )
            await self._progress("completed" if not first_failure else "failed", 100, "算法实验完成", report_id=report["report_id"], summary=report["summary"], cache=cache_stats)
            return {"run_id": self.run_id, "status": "completed" if not first_failure else "failed", "report_id": report["report_id"], "report": report, "report_path": str(report_path), "cache": cache_stats, "model_requests": 0}
        except RunCancelled:
            return await self._finish_cancelled(config)
        except asyncio.CancelledError:
            self.cancellation.cancel("task_cancelled")
            return await self._finish_cancelled(config)
        except Exception as exc:
            return await self._finish_failure(config, type(exc).__name__, str(exc))

    def _validate_config(self, config: AlgorithmRunConfig) -> None:
        if config.profile not in PROFILE_DEFAULTS:
            raise ValueError(f"Unknown algorithm run profile: {config.profile}")
        if not config.candidate_command.strip():
            raise ValueError("candidate_command is required")

    def _build_cases(self, config: AlgorithmRunConfig, *, max_cases: int) -> list[dict[str, Any]]:
        cases: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(config.cases):
            if not isinstance(item, dict):
                continue
            input_data = str(item.get("input") or "")
            if len(input_data.encode("utf-8")) > MAX_CASE_INPUT:
                continue
            if not config.oracle_command.strip() and "expected" not in item:
                continue
            key = input_data + "\0" + str(item.get("expected") or "")
            if key in seen:
                continue
            seen.add(key)
            cases.append({
                "label": str(item.get("label") or f"case-{index + 1}"),
                "input": input_data,
                "expected": str(item.get("expected") or ""),
                "source": str(item.get("source") or "explicit"),
            })
            if len(cases) >= max_cases:
                return cases
        if not cases and config.problem_text.strip() and config.oracle_command.strip():
            spec = parse_problem(config.problem_text)
            self._last_spec = spec.to_dict()
            for item in suggest_boundary_cases(spec, limit=max_cases):
                cases.append({"label": item.get("label", "boundary"), "input": str(item.get("input") or ""), "expected": str(item.get("expected") or ""), "source": "boundary"})
            seen = {str(item.get("input") or "") for item in cases}
            random_budget = int(config.defaults["max_random_cases"])
            rng = random.Random(config.seed)
            for variable in spec.variables:
                if len(cases) >= max_cases or random_budget <= 0:
                    break
                lower = variable.get("min")
                upper = variable.get("max")
                if not isinstance(lower, int) or not isinstance(upper, int) or lower > upper:
                    continue
                for _ in range(random_budget):
                    if len(cases) >= max_cases:
                        break
                    value = rng.randint(lower, upper)
                    input_data = str(value)
                    if input_data in seen:
                        continue
                    seen.add(input_data)
                    cases.append({"label": f"random-{variable.get('name', 'value')}-{len(cases) + 1}", "input": input_data, "expected": "", "source": "random"})
                random_budget = 0
        return cases[:max_cases]

    async def _prepare_command(self, config: AlgorithmRunConfig) -> tuple[str, dict[str, Any]]:
        command = config.candidate_command.strip()
        if not config.candidate_path:
            return command, {"message": "使用用户提供的候选命令（无需编译阶段）", "compiled": False}
        path = self.workspace.resolve(config.candidate_path, must_exist=True)
        if path.suffix.casefold() not in {".cpp", ".cc", ".cxx", ".c", ".java"}:
            return command, {"message": "脚本候选已准备（跳过编译）", "compiled": False}
        if command != config.candidate_path and command != str(path):
            return command, {"message": "使用用户提供的候选命令（跳过自动编译）", "compiled": False}
        source_signature = self._source_signature(config.candidate_path)
        cache_key = self._compile_cache_key(path, source_signature)
        cache_root = self.workspace.root / ".code-helper" / "algorithm-runs" / "compiled"
        cache_root.mkdir(parents=True, exist_ok=True)
        if path.suffix.casefold() == ".java":
            output = cache_root / cache_key
            class_file = output / f"{path.stem}.class"
            run_command = f"java -cp {_shell_quote(str(output))} {_shell_quote(path.stem)}"
            if class_file.is_file() and class_file.stat().st_size > 0:
                return run_command, {"message": "Java 候选命中编译缓存", "compiled": True, "cache_hit": True, "cache_key": cache_key, "run_command": run_command}
            output.mkdir(parents=True, exist_ok=True)
            compile_command = f"javac -d {_shell_quote(str(output))} {_shell_quote(str(path))}"
        else:
            output = cache_root / f"{cache_key}.exe" if os.name == "nt" else cache_root / cache_key
            if output.is_file() and output.stat().st_size > 0:
                run_command = _shell_quote(str(output))
                return run_command, {"message": "候选程序命中编译缓存", "compiled": True, "cache_hit": True, "cache_key": cache_key, "run_command": run_command}
            compile_command = f"g++ -std=c++17 -O2 -pipe {_shell_quote(str(path))} -o {_shell_quote(str(output))}"
            run_command = _shell_quote(str(output))
        await self._progress("compiling", 12, "正在编译候选程序", command=compile_command, cache_key=cache_key)
        _, status, detail, duration = await _timed_run_case(compile_command, "", self.workspace, 30.0, self.cancellation)
        if status != "ok":
            return command, {"compiled": False, "error": detail or "候选编译失败", "command": compile_command, "duration_ms": duration, "cache_key": cache_key}
        return run_command, {"compiled": True, "message": "候选程序编译成功", "command": compile_command, "run_command": run_command, "duration_ms": duration, "cache_key": cache_key}

    async def _run_differential(self, candidate: str, oracle: str, cases: list[dict[str, Any]], *, timeout: float, parallelism: int, fail_fast: bool, seed: int, source_signature: str = "", oracle_signature: str = "") -> tuple[list[dict[str, Any]], dict[str, int]]:
        async def execute(item: dict[str, Any], index: int) -> dict[str, Any]:
            input_data = str(item.get("input") or "")
            cache_key = self._cache_key("differential", source_signature, oracle_signature, candidate, oracle, input_data, timeout, seed)
            cached = self._cache_read(cache_key)
            if cached is not None:
                cached["cache_hit"] = True
                return cached
            oracle_output, oracle_status, oracle_detail, oracle_ms = await _timed_run_case(oracle, input_data, self.workspace, timeout, self.cancellation)
            if oracle_status != "ok":
                result = self._result_item(item, index, "oracle_error" if oracle_status == "runtime_error" else oracle_status, "", "", oracle_detail or "Oracle failed", oracle_ms, 0, oracle_ms, oracle_source="user_command")
            else:
                actual, status, detail, candidate_ms = await _timed_run_case(candidate, input_data, self.workspace, timeout, self.cancellation)
                normalized_expected = normalize_output(oracle_output)
                normalized_actual = normalize_output(actual)
                final_status = "passed" if status == "ok" and normalized_expected == normalized_actual else "wrong_answer" if status == "ok" else "time_limit_exceeded" if status == "timeout" else status
                result = self._result_item(item, index, final_status, normalized_expected, normalized_actual, detail or ("candidate output differs from Oracle" if final_status == "wrong_answer" else ""), oracle_ms, candidate_ms, oracle_ms + candidate_ms, oracle_source="user_command")
            self._cache_write(cache_key, result)
            return result
        results, stats = await self._bounded_cases(cases, execute, parallelism=parallelism, fail_fast=fail_fast)
        return results, stats

    async def _run_expected(self, candidate: str, cases: list[dict[str, Any]], *, timeout: float, parallelism: int, fail_fast: bool, seed: int, source_signature: str = "") -> tuple[list[dict[str, Any]], dict[str, int]]:
        async def execute(item: dict[str, Any], index: int) -> dict[str, Any]:
            input_data = str(item.get("input") or "")
            expected = str(item.get("expected") or "")
            cache_key = self._cache_key("expected", source_signature, candidate, expected, input_data, timeout, seed)
            cached = self._cache_read(cache_key)
            if cached is not None:
                cached["cache_hit"] = True
                return cached
            actual, status, detail, duration = await _timed_run_case(candidate, input_data, self.workspace, timeout, self.cancellation)
            normalized_expected = normalize_output(expected)
            normalized_actual = normalize_output(actual)
            final_status = "passed" if status == "ok" and normalized_expected == normalized_actual else "wrong_answer" if status == "ok" else "time_limit_exceeded" if status == "timeout" else status
            result = self._result_item(item, index, final_status, normalized_expected, normalized_actual, detail or ("candidate output differs from expected output" if final_status == "wrong_answer" else ""), 0, duration, duration)
            self._cache_write(cache_key, result)
            return result
        return await self._bounded_cases(cases, execute, parallelism=parallelism, fail_fast=fail_fast)

    async def _bounded_cases(self, cases: list[dict[str, Any]], execute: Callable[[dict[str, Any], int], Awaitable[dict[str, Any]]], *, parallelism: int, fail_fast: bool) -> tuple[list[dict[str, Any]], dict[str, int]]:
        semaphore = asyncio.Semaphore(max(1, min(parallelism, len(cases))))
        results: dict[int, dict[str, Any]] = {}
        tasks: set[asyncio.Task[dict[str, Any]]] = set()
        cache_hits = 0
        cache_misses = 0

        async def record_done(result: dict[str, Any]) -> None:
            """Store one result and publish lightweight partial progress."""

            nonlocal cache_hits, cache_misses
            results[int(result.get("_index", 0))] = result
            cache_hits += int(bool(result.pop("cache_hit", False)))
            cache_misses += 1 - int(bool(result.get("_cached", False)))
            completed = len(results)
            passed = sum(item.get("status") == "passed" for item in results.values())
            failed = completed - passed
            # A progress event per completed case keeps the UI responsive while
            # remaining independent of model streaming or Agent steps.
            await self._progress(
                "testing",
                30 + int(38 * completed / max(1, len(cases))),
                f"已完成 {completed}/{len(cases)} 条用例",
                completed=completed,
                total=len(cases),
                passed=passed,
                failed=failed,
                cache={"hits": cache_hits, "misses": cache_misses},
            )

        async def one(item: dict[str, Any], index: int) -> dict[str, Any]:
            async with semaphore:
                self.cancellation.raise_if_cancelled()
                return await execute(item, index)

        for index, item in enumerate(cases):
            tasks.add(asyncio.create_task(one(item, index)))
            if len(tasks) >= max(1, parallelism):
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                tasks = set(pending)
                done_results: list[dict[str, Any]] = []
                for task in done:
                    result = task.result()
                    done_results.append(result)
                    await record_done(result)
                if fail_fast and any(result.get("status") != "passed" for result in done_results):
                    for pending_task in tasks:
                        pending_task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks.clear()
                    break
        if tasks:
            done, _ = await asyncio.wait(tasks)
            for task in done:
                result = task.result()
                await record_done(result)
        ordered = [results[index] for index in sorted(results)]
        for item in ordered:
            item.pop("_index", None)
            item.pop("_cached", None)
        return ordered, {"hits": cache_hits, "misses": cache_misses}

    def _result_item(self, item: dict[str, Any], index: int, status: str, expected: str, actual: str, detail: str, oracle_ms: float, candidate_ms: float, duration: float, *, oracle_source: str = "expected_output") -> dict[str, Any]:
        return {"_index": index, "label": str(item.get("label") or f"case-{index + 1}"), "status": status, "expected": expected[:MAX_CASE_OUTPUT], "actual": actual[:MAX_CASE_OUTPUT], "detail": detail[:2000], "input": str(item.get("input") or "")[:MAX_CASE_INPUT], "input_size": len(str(item.get("input") or "").encode("utf-8")), "case_source": str(item.get("source") or "explicit"), "oracle_source": oracle_source, "oracle_duration_ms": round(float(oracle_ms), 3), "duration_ms": round(float(candidate_ms), 3), "total_duration_ms": round(float(duration), 3)}

    @staticmethod
    def _report_stages(cases: list[dict[str, Any]], compile_info: dict[str, Any], first_failure: dict[str, Any] | None, benchmark_enabled: bool) -> list[str]:
        sources = {str(item.get("source") or "explicit") for item in cases}
        stages = ["preparing"]
        if compile_info.get("compiled") or compile_info.get("command"):
            stages.append("compiling")
        if "boundary" in sources:
            stages.append("boundary_testing")
        if "random" in sources:
            stages.append("random_testing")
        stages.append("testing")
        if first_failure and first_failure.get("status") == "wrong_answer":
            stages.append("shrinking")
        if benchmark_enabled and not first_failure:
            stages.append("benchmarking")
        stages.append("reporting")
        return stages

    async def _shrink(self, candidate: str, oracle: str, failure: dict[str, Any], *, timeout: float, limit: int, source_signature: str = "", oracle_signature: str = "") -> tuple[str | None, list[dict[str, int]]]:
        current = str(failure.get("input") or "")
        cache_key = self._cache_key(
            "shrink",
            source_signature,
            oracle_signature,
            candidate,
            oracle,
            current,
            failure.get("expected", ""),
            timeout,
            limit,
        )
        cached = self._cache_read(f"shrink-{cache_key}")
        if cached is not None:
            self._shrink_cache_hit = True
            return cached.get("minimized_input"), list(cached.get("shrink_trace") or [])
        self._shrink_cache_hit = False
        trace = [{"bytes": len(current.encode("utf-8"))}]
        candidates = list(shrink_input_candidates(current))[: max(0, limit)]
        for possible in candidates:
            self.cancellation.raise_if_cancelled()
            if oracle:
                expected, oracle_status, _, _ = await _timed_run_case(oracle, possible, self.workspace, timeout, self.cancellation)
            else:
                expected = str(failure.get("expected") or "")
                oracle_status = "ok"
            actual, candidate_status, _, _ = await _timed_run_case(candidate, possible, self.workspace, timeout, self.cancellation)
            if oracle_status == "ok" and candidate_status == "ok" and normalize_output(expected) != normalize_output(actual):
                current = possible
                size = len(current.encode("utf-8"))
                if trace[-1]["bytes"] != size:
                    trace.append({"bytes": size})
        self._cache_write(
            f"shrink-{cache_key}",
            {"status": "ok", "minimized_input": current, "shrink_trace": trace},
        )
        return current, trace

    def _benchmark(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        durations = [float(item.get("duration_ms") or 0) for item in results if float(item.get("duration_ms") or 0) > 0]
        curve: list[dict[str, Any]] = []
        for size in sorted({int(item.get("input_size") or 0) for item in results}):
            samples = [float(item.get("duration_ms") or 0) for item in results if int(item.get("input_size") or 0) == size and float(item.get("duration_ms") or 0) > 0]
            if samples:
                curve.append({"input_size": size, "samples": len(samples), "p50_ms": self._percentile(samples, 0.5), "p95_ms": self._percentile(samples, 0.95), "max_ms": round(max(samples), 3)})
        return {"samples": len(durations), "p50_ms": self._percentile(durations, 0.5), "p95_ms": self._percentile(durations, 0.95), "max_ms": round(max(durations, default=0.0), 3), "curve": curve}

    async def _benchmark_repeats(self, candidate: str, cases: list[dict[str, Any]], *, timeout: float, repeats: int) -> dict[str, Any]:
        samples: list[dict[str, Any]] = []
        for repeat in range(max(1, repeats)):
            for index, item in enumerate(cases):
                self.cancellation.raise_if_cancelled()
                _, status, _, duration = await _timed_run_case(candidate, str(item.get("input") or ""), self.workspace, timeout, self.cancellation)
                if status == "ok":
                    samples.append({"input_size": len(str(item.get("input") or "").encode("utf-8")), "duration_ms": duration, "repeat": repeat})
        return self._benchmark(samples)

    def _complexity(self, path: str) -> dict[str, Any] | None:
        if not path:
            return None
        try:
            resolved = self.workspace.resolve(path, must_exist=True)
            measured = analyze_file(resolved)
            return measured if measured.get("status") == "ok" else None
        except Exception:
            return None

    def _cache_key(self, kind: str, *parts: Any) -> str:
        payload = json.dumps([kind, *parts], ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self._cache_directory / f"{key}.json"

    def _cache_read(self, key: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._cache_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("status") else None

    def _cache_write(self, key: str, value: dict[str, Any]) -> None:
        path = self._cache_path(key)
        temporary = path.with_suffix(".tmp")
        payload = dict(value)
        payload["_cached"] = True
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            return

    def _source_signature(self, path: str) -> str:
        if not path:
            return ""
        try:
            resolved = self.workspace.resolve(path, must_exist=True)
            return hashlib.sha256(resolved.read_bytes()).hexdigest()
        except (OSError, ValueError):
            return ""

    def _command_source_signature(self, command: str) -> str:
        """Hash source files referenced by a shell command when detectable."""

        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = command.split()
        candidates: list[tuple[str, str]] = []
        for token in tokens:
            cleaned = token.strip().strip('"').strip("'")
            if not cleaned or cleaned.startswith("-"):
                continue
            try:
                resolved = self.workspace.resolve(cleaned, must_exist=True)
            except Exception:
                continue
            if not resolved.is_file() or resolved.suffix.casefold() not in {".py", ".js", ".mjs", ".cpp", ".cc", ".cxx", ".c", ".java"}:
                continue
            try:
                relative = str(resolved.relative_to(self.workspace.root)).replace("\\", "/")
                digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except (OSError, ValueError):
                continue
            candidates.append((relative, digest))
        if not candidates:
            return ""
        payload = json.dumps(sorted(candidates), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _compile_cache_key(self, path: Path, source_signature: str) -> str:
        """Create a stable, workspace-local key for a compiler artifact."""

        try:
            relative = str(path.relative_to(self.workspace.root)).replace("\\", "/")
        except ValueError:
            relative = str(path)
        payload = json.dumps(
            [relative, source_signature, path.suffix.casefold(), "g++-std=c++17-O2"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    async def _progress(self, stage: str, progress: int, message: str, **extra: Any) -> None:
        payload = {"run_id": self.run_id, "stage": stage, "progress": max(0, min(100, int(progress))), "message": message, "model_requests": 0, **extra}
        await self.event_bus.publish(AgentEvent(type="algorithm_run_progress", session_id=self.session_id, turn_id=self.turn_id, payload=payload))
        if self.progress_callback is not None:
            result = self.progress_callback(payload)
            if asyncio.iscoroutine(result):
                await result

    async def _finish_failure(self, config: AlgorithmRunConfig, code: str, message: str, **extra: Any) -> dict[str, Any]:
        payload = {"run_id": self.run_id, "profile": config.profile, "status": "failed", "code": code, "message": message, "model_requests": 0, **extra}
        await self.event_bus.publish(AgentEvent(type="algorithm_run_failed", session_id=self.session_id, turn_id=self.turn_id, payload=payload))
        await self._progress("failed", 100, message, code=code, **extra)
        return payload

    async def _finish_cancelled(self, config: AlgorithmRunConfig) -> dict[str, Any]:
        payload = {"run_id": self.run_id, "profile": config.profile, "status": "cancelled", "code": "CANCELLED", "message": self.cancellation.reason, "model_requests": 0}
        await self.event_bus.publish(AgentEvent(type="algorithm_run_cancelled", session_id=self.session_id, turn_id=self.turn_id, payload=payload))
        await self._progress("cancelled", 100, "算法实验已取消", code="CANCELLED")
        return payload

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = (len(ordered) - 1) * max(0.0, min(1.0, quantile))
        low = int(index)
        high = min(low + 1, len(ordered) - 1)
        return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 3)


def default_candidate_command(path: str) -> str:
    """Return a portable execution command for a source file."""
    suffix = Path(path).suffix.casefold()
    quoted = _shell_quote(path)
    if suffix == ".py":
        return f"{_shell_quote(sys.executable)} {quoted}"
    if suffix in {".js", ".mjs"}:
        return f"node {quoted}"
    if suffix == ".java":
        return path
    if suffix in {".cpp", ".cc", ".cxx", ".c"}:
        return path
    return path


def _shell_quote(value: str) -> str:
    """Quote a command argument for the shell used by asyncio subprocesses."""
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)
