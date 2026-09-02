from __future__ import annotations

import asyncio

from evals.catalog import load_tasks
from evals.runner import run_suite


WORKFLOW_TASKS = {"workflow_add_feature", "workflow_bug_fix", "workflow_code_review"}


def test_workflow_eval_catalog_contains_three_contracts() -> None:
    assert WORKFLOW_TASKS.issubset({task.id for task in load_tasks()})


def test_workflow_eval_contracts_and_metrics() -> None:
    report = asyncio.run(run_suite(task_ids=WORKFLOW_TASKS))
    assert report["metrics"]["contract_pass_rate"] == 1.0
    assert report["metrics"]["completion_rate"] == 1.0
    assert report["metrics"]["safety_pass_rate"] == 1.0
    assert report["metrics"]["verification_rate"] == 1.0
    assert {item["task_id"] for item in report["tasks"]} == WORKFLOW_TASKS
    assert all("skill_loaded" in {
        assertion["name"].removeprefix("event:")
        for assertion in item["assertions"]
        if assertion["name"].startswith("event:") and assertion["passed"]
    } for item in report["tasks"])

    workflow_metrics = report["metrics"]["workflows"]
    assert workflow_metrics["task_count"] == 3
    assert workflow_metrics["skill_load_rate"] == 1.0
    assert workflow_metrics["selection_rate"] == 1.0
    assert workflow_metrics["add_feature_plan_rate"] == 1.0
    assert workflow_metrics["add_feature_plan_completion_rate"] == 1.0
    assert workflow_metrics["fresh_verification_rate"] == 1.0
    assert workflow_metrics["review_read_only_rate"] == 1.0
    assert workflow_metrics["recovery_stage_consistency_rate"] == 1.0
    assert workflow_metrics["average_steps"] > 0
    assert workflow_metrics["average_tokens"] > 0
