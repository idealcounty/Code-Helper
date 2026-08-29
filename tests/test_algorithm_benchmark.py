from __future__ import annotations

import asyncio

from evals.algorithm_benchmark import render_markdown, run_benchmark


def test_algorithm_benchmark_detects_and_minimizes_faults() -> None:
    report = asyncio.run(run_benchmark())
    metrics = report["metrics"]
    assert report["case_count"] == 15
    assert metrics["correct_candidate_passed"] is True
    assert metrics["fault_detection_rate"] == 1.0
    assert metrics["failure_minimization_rate"] == 1.0
    assert all(item["minimized_input"] is not None for item in report["scenarios"] if item["expected_fault"])


def test_algorithm_benchmark_markdown_is_human_readable() -> None:
    markdown = render_markdown(asyncio.run(run_benchmark()))
    assert "Algorithm Judge" in markdown
    assert "错误候选检测率" in markdown
