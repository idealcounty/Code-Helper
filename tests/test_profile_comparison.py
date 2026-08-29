from __future__ import annotations

import asyncio

from evals.profile_comparison import render_markdown, run_comparison


def test_profile_comparison_runs_same_tasks_under_both_profiles() -> None:
    report = asyncio.run(run_comparison())
    assert report["task_ids"] == ["algorithm_profile_repair", "cross_file_feature"]
    for profile in ("project", "algorithm"):
        metrics = report["profiles"][profile]["metrics"]
        assert metrics["task_count"] == 2
        assert metrics["contract_pass_rate"] == 1.0
        assert metrics["verification_rate"] == 1.0
        assert all(task["task_profile"] == profile for task in report["profiles"][profile]["tasks"])


def test_profile_comparison_markdown_states_scope() -> None:
    markdown = render_markdown(asyncio.run(run_comparison()))
    assert "project" in markdown and "algorithm" in markdown
    assert "does not establish real-model quality" in markdown
