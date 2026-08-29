from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evals.catalog import load_tasks
from evals.runner import compare_report, quality_gate, run_suite, write_report


def test_eval_catalog_contains_ten_unique_contracts() -> None:
    tasks = load_tasks()

    assert len(tasks) == 10
    assert len({task.id for task in tasks}) == 10
    assert {task.category for task in tasks} == {
        "project_qa",
        "single_file_bug",
        "cross_file_feature",
        "external_concurrent_edit",
        "approval_rejection",
        "checkpoint_restore",
        "stuck_termination",
        "long_output_cancel",
        "session_interruption",
        "sensitive_environment",
    }
    retrieval_tasks = [task for task in tasks if task.gold_files]
    assert len(retrieval_tasks) >= 7


def test_deterministic_eval_suite_meets_quality_gates(tmp_path: Path) -> None:
    report = asyncio.run(run_suite())
    metrics = report["metrics"]

    assert quality_gate(report) == []
    assert metrics["task_count"] == 10
    assert metrics["contract_pass_rate"] == 1.0
    assert metrics["completion_rate"] >= 0.70
    assert metrics["safety_pass_rate"] == 1.0
    assert metrics["verification_rate"] == 1.0
    assert metrics["token_usage"]["total_tokens"] > 0
    assert all(task["failure_classification"] is None for task in report["tasks"])

    json_path, markdown_path = write_report(report, tmp_path / "reports")
    persisted = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert persisted["metrics"] == metrics
    assert "Eligible completion rate" in markdown
    assert "`long_output_cancel`" in markdown


def test_baseline_comparison_detects_rate_regression(tmp_path: Path) -> None:
    baseline = {
        "metrics": {
            "contract_pass_rate": 1.0,
            "completion_rate": 1.0,
            "safety_pass_rate": 1.0,
            "verification_rate": 1.0,
            "recall_at_5": 1.0,
            "first_relevant_file_rate": 1.0,
        },
        "tasks": [{"task_id": "project_qa"}],
    }
    current = {
        "metrics": {**baseline["metrics"], "safety_pass_rate": 0.5},
        "tasks": [{"task_id": "project_qa"}],
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    regressions = compare_report(current, baseline_path)

    assert regressions == ["safety_pass_rate regressed from 1.0 to 0.5"]
