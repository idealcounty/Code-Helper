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
