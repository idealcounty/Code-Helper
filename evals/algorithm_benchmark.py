"""Reproducible Algorithm Judge benchmark for the algorithm task profile."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry, Workspace, register_algorithm_tools


SEED = 20260829


def _cases() -> list[dict[str, str]]:
    """Return sample, boundary and seeded random cases for x -> 2*x."""
    values = [0, 1, -1, 100, -100]
    # A small linear congruential sequence keeps the fixture transparent and
    # avoids making the benchmark depend on a third-party generator.
    state = SEED
    for index in range(10):
        state = (1103515245 * state + 12345) % (2**31)
        values.append(str((state % 401) - 200))
    return [
        {"label": f"case-{index + 1}", "input": f"{value}\n", "expected": f"{int(value) * 2}\n"}
        for index, value in enumerate(values)
    ]


async def _run_candidate(
    workspace: Path,
    *,
    name: str,
    source: str,
    cases: list[dict[str, str]],
) -> dict[str, Any]:
    script = workspace / f"{name}.py"
    script.write_text(source, encoding="utf-8", newline="\n")
    registry = ToolRegistry()
    register_algorithm_tools(registry, Workspace(workspace))
    executor = ToolExecutor(registry)
    result = await executor.execute(
        "judge_algorithm",
        {
            "command": f'"{sys.executable}" "{script}"',
            "seed": SEED,
            "cases": cases,
        },
    )
    judge = (result.data or {}).get("judge", {})
    return {
        "id": name,
        "expected_fault": name != "correct_candidate",
        "judge_ok": result.ok,
        "code": result.code,
        "passed": int(judge.get("passed", 0)),
        "failed": int(judge.get("failed", 0)),
        "total": int(judge.get("total", 0)),
        "first_failure": judge.get("first_failure"),
        "minimized_input": judge.get("minimized_input"),
    }


async def run_benchmark() -> dict[str, Any]:
    cases = _cases()
    candidates = {
        "correct_candidate": "import sys\nvalue = int(sys.stdin.read())\nprint(value * 2)\n",
        "constant_wrong": "print(0)\n",
        "zero_boundary_bug": (
            "import sys\nvalue = int(sys.stdin.read())\n"
            "print(1 if value == 0 else value * 2)\n"
        ),
    }
    with tempfile.TemporaryDirectory(prefix="code-helper-algorithm-") as temporary:
        workspace = Path(temporary)
        scenarios = [
            await _run_candidate(workspace, name=name, source=source, cases=cases)
            for name, source in candidates.items()
        ]
    faulty = [item for item in scenarios if item["expected_fault"]]
    detected = [item for item in faulty if not item["judge_ok"]]
    minimized = [item for item in faulty if item["minimized_input"] is not None]
    return {
        "schema_version": 1,
        "seed": SEED,
        "case_count": len(cases),
        "input_classes": ["sample", "boundary", "random"],
        "scenarios": scenarios,
        "metrics": {
            "candidate_count": len(scenarios),
            "faulty_candidate_count": len(faulty),
            "fault_detection_rate": round(len(detected) / len(faulty), 4) if faulty else 0.0,
            "failure_minimization_rate": round(len(minimized) / len(detected), 4) if detected else 0.0,
            "correct_candidate_passed": next(
                item["judge_ok"] for item in scenarios if item["id"] == "correct_candidate"
            ),
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# Algorithm Judge 基准",
        "",
        f"固定 seed `{report['seed']}`，共 `{report['case_count']}` 个样例/边界/随机输入。",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| 错误候选检测率 | {metrics['fault_detection_rate']:.1%} |",
        f"| 检测失败的最小化率 | {metrics['failure_minimization_rate']:.1%} |",
        f"| 正确候选通过 | {'是' if metrics['correct_candidate_passed'] else '否'} |",
        "",
        "## 候选明细",
        "",
        "| 候选 | 通过 | 失败 | 最小失败输入 |",
        "| --- | ---: | ---: | --- |",
    ]
    for item in report["scenarios"]:
        minimized = item["minimized_input"]
        display = repr(minimized) if minimized is not None else "—"
        lines.append(f"| `{item['id']}` | {item['passed']} | {item['failed']} | `{display}` |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results"))
    args = parser.parse_args()
    report = asyncio.run(run_benchmark())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "algorithm.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (args.output_dir / "algorithm.md").write_text(
        render_markdown(report), encoding="utf-8", newline="\n"
    )
    print(render_markdown(report), end="")
    return 0 if report["metrics"]["fault_detection_rate"] == 1.0 and report["metrics"]["correct_candidate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
