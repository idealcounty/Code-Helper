from __future__ import annotations

import asyncio
from typing import Any

from evals.superpowers_comparison import run_comparison


def test_superpowers_comparison_reports_enabled_and_disabled_runs() -> None:
    report = asyncio.run(
        run_comparison(
            mode="deterministic",
            task_ids={"workflow_add_feature", "workflow_bug_fix", "workflow_code_review"},
        )
    )

    assert report["mode"] == "deterministic"
    assert set(report["runs"]) == {"enabled", "disabled"}
    assert report["task_ids"] == [
        "workflow_add_feature",
        "workflow_bug_fix",
        "workflow_code_review",
    ]
    assert report["runs"]["enabled"]["metrics"]["task_count"] == 3
    assert report["runs"]["disabled"]["metrics"]["task_count"] == 3
    assert report["runs"]["enabled"]["metrics"]["workflows"]["skill_load_rate"] == 1.0
    assert report["runs"]["disabled"]["metrics"]["workflows"]["skill_load_rate"] == 0.0
    assert report["runs"]["enabled"]["metrics"]["completion_rate"] == 1.0
    assert report["runs"]["disabled"]["metrics"]["completion_rate"] == 1.0
    assert report["runs"]["enabled"]["metrics"]["verification_rate"] == 1.0
    assert report["runs"]["disabled"]["metrics"]["verification_rate"] == 1.0
    assert report["delta"]["unrelated_modifications"] == 0


def test_superpowers_comparison_averages_nested_workflow_metrics(
    monkeypatch,
) -> None:
    import evals.superpowers_comparison as comparison

    calls = {True: 0, False: 0}

    async def fake_run_suite(**kwargs: Any) -> dict[str, Any]:
        enabled = bool(kwargs["workflow_enabled"])
        calls[enabled] += 1
        run_number = calls[enabled]
        steps = float(run_number if enabled else run_number + 2)
        tokens = float(run_number * 10 if enabled else run_number * 20)
        return {
            "mode": "deterministic",
            "workflow_enabled": enabled,
            "metrics": {
                "task_count": 3,
                "completion_rate": 1.0,
                "verification_rate": 1.0,
                "average_steps": steps,
                "workflows": {
                    "average_steps": steps,
                    "average_tokens": tokens,
                    "average_tool_calls": 1.0,
                },
            },
            "tasks": [],
        }

    monkeypatch.setattr(comparison, "run_suite", fake_run_suite)
    report = asyncio.run(
        comparison.run_comparison(
            mode="deterministic",
            task_ids={"workflow_code_review"},
            repetitions=2,
        )
    )

    assert report["runs"]["enabled"]["repetitions"] == 2
    assert report["runs"]["enabled"]["metrics"]["workflows"]["average_steps"] == 1.5
    assert report["runs"]["enabled"]["metrics"]["workflows"]["average_tokens"] == 15.0
    assert report["runs"]["disabled"]["metrics"]["workflows"]["average_steps"] == 3.5
    assert report["delta"]["average_tokens"] == -15.0
    assert set(report["delta"]) >= {
        "completion_rate",
        "verification_rate",
        "average_steps",
        "average_tokens",
    }
    assert report["quality_evidence"]["status"] == "real_model_required"
