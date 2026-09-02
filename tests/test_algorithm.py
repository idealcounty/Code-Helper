from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from coding_agent.algorithm import AlgorithmJudge, JudgeCase, parse_problem
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry, Workspace, register_algorithm_tools


def test_problem_parser_extracts_structured_sections() -> None:
    spec = parse_problem(
        "# Two Sum\n\nInput:\n- an array\n\nOutput:\n- indices\n\nConstraints:\n- n <= 100\n\nExamples:\n1 2 -> 3"
    )
    assert spec.title == "Two Sum"
    assert "an array" in spec.input_description
    assert spec.constraints == ("n <= 100",)
    assert spec.examples == ("1 2 -> 3",)


def test_problem_parser_extracts_numeric_bounds_and_confidence() -> None:
    spec = parse_problem(
        "# Bounds\n\nInput:\n t and n\n\nOutput:\n answer\n\nConstraints:\n1≤t≤3⋅10^4\n1≤n≤2⋅10^5\nsum n ≤ 2⋅10^5"
    )
    variables = {item["name"]: item for item in spec.variables}
    assert variables["t"]["min"] == 1
    assert variables["t"]["max"] == 30_000
    assert variables["n"]["max"] == 200_000
    assert spec.aggregate_constraints
    assert spec.confidence > 0.5


def test_algorithm_judge_is_reproducible_and_classifies_wrong_answer() -> None:
    cases = [JudgeCase("1\n", "2\n", "one"), JudgeCase("2\n", "4\n", "two")]
    outputs = [("2\n", "ok", None), ("5\n", "ok", None)]
    report = AlgorithmJudge(seed=17).evaluate(cases, outputs)
    assert report.seed == 17
    assert report.passed == 1 and report.failed == 1
    assert report.first_failure is not None
    assert report.first_failure["status"] == "wrong_answer"


def test_judge_algorithm_tool_runs_cases_through_tool_executor(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_algorithm_tools(registry, Workspace(tmp_path))
    executor = ToolExecutor(registry)
    code = "import sys; print(int(sys.stdin.read()) * 2)"
    command = f'"{sys.executable}" -c "{code}"'
    result = asyncio.run(
        executor.execute(
            "judge_algorithm",
            {
                "command": command,
                "seed": 42,
                "cases": [
                    {"label": "one", "input": "1\n", "expected": "2\n"},
                    {"label": "three", "input": "3\n", "expected": "6\n"},
                ],
            },
        )
    )
    assert result.ok is True
    assert result.data["judge"]["seed"] == 42
    assert result.data["judge"]["passed"] == 2
    assert result.data["judge"]["benchmark"]["p95_ms"] >= 0


def test_judge_algorithm_records_reproducible_minimized_failure(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_algorithm_tools(registry, Workspace(tmp_path))
    executor = ToolExecutor(registry)
    command = f'"{sys.executable}" -c "print(0)"'
    result = asyncio.run(
        executor.execute(
            "judge_algorithm",
            {
                "command": command,
                "seed": 9,
                "cases": [{"input": "123 456\n", "expected": "1\n"}],
            },
        )
    )
    assert result.ok is False
    assert result.code == "ALGORITHM_JUDGE_FAILED"
    assert result.data["judge"]["minimized_input"] is not None


def test_differential_experiment_uses_oracle_and_reports_timing(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_algorithm_tools(registry, Workspace(tmp_path))
    python = str(sys.executable)
    candidate = f'"{python}" -c "print(0)"'
    oracle = f'"{python}" -c "import sys; print(int(sys.stdin.read()) * 2)"'
    result = asyncio.run(
        ToolExecutor(registry).execute(
            "run_algorithm_experiment",
            {
                "candidate_command": candidate,
                "oracle_command": oracle,
                "seed": 12,
                "cases": [
                    {"label": "zero", "input": "0\n", "source": "boundary"},
                    {"label": "two", "input": "2\n", "source": "random"},
                ],
            },
        )
    )
    assert result.ok is False
    report = result.data["judge"]
    assert report["failed"] == 1
    assert report["cases"][0]["status"] == "passed"
    assert report["cases"][1]["status"] == "wrong_answer"
    assert report["cases"][1]["duration_ms"] >= 0
    assert report["cases"][1]["oracle_source"] == "user_command"
    assert report["minimized_input"] is not None


def test_generate_algorithm_cases_is_seeded_and_labels_sources(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_algorithm_tools(registry, Workspace(tmp_path))
    executor = ToolExecutor(registry)
    arguments = {"min_value": -2, "max_value": 4, "random_count": 8, "seed": 19}
    first = asyncio.run(executor.execute("generate_algorithm_cases", arguments))
    second = asyncio.run(executor.execute("generate_algorithm_cases", arguments))
    assert first.ok and first.data == second.data
    sources = {item["source"] for item in first.data["cases"]}
    assert sources == {"boundary", "random"}
    assert first.metadata["random_cases"] == 8


def test_analyze_complexity_reports_nested_loops_and_recursion(tmp_path: Path) -> None:
    source = (
        "def walk(items):\n"
        "    if not items:\n"
        "        return 0\n"
        "    for item in items:\n"
        "        for child in item:\n"
        "            walk(child)\n"
        "    return 1\n"
    )
    path = tmp_path / "solution.py"
    path.write_text(source, encoding="utf-8")
    registry = ToolRegistry()
    register_algorithm_tools(registry, Workspace(tmp_path))

    result = asyncio.run(
        ToolExecutor(registry).execute(
            "analyze_complexity", {"path": "solution.py"}
        )
    )

    assert result.ok
    complexity = result.data["complexity"]
    assert complexity["max_loop_nesting"] == 2
    assert complexity["estimated_time_complexity"] == "O(n^2)"
    assert complexity["recursive_functions"] == ["walk"]
    assert "nested loops may be super-linear" in complexity["warnings"]


def test_analyze_complexity_uses_conservative_generic_estimate(tmp_path: Path) -> None:
    path = tmp_path / "main.cpp"
    path.write_text(
        "void f() { for (int i=0; i<n; ++i) { for (int j=0; j<n; ++j) {} } }\n",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    register_algorithm_tools(registry, Workspace(tmp_path))

    result = asyncio.run(
        ToolExecutor(registry).execute(
            "analyze_complexity", {"path": "main.cpp"}
        )
    )

    assert result.ok
    complexity = result.data["complexity"]
    assert complexity["parser"] == "heuristic"
    assert complexity["loop_count"] == 2
    assert complexity["estimated_time_complexity"] == "O(n^2)"
    assert complexity["warning"].startswith("Heuristic estimate")
