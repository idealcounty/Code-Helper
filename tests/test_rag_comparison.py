from __future__ import annotations

import asyncio

from evals.rag_comparison import (
    _stable_cross_file_improvement,
    render_markdown,
    run_comparison,
)


def test_rag_comparison_reports_cross_file_metrics() -> None:
    report = asyncio.run(run_comparison(mode="deterministic"))

    assert report["quality_evidence"]["status"] == "real_model_required"
    assert report["runs"]["repo_map"]["cross_file"]["metrics"]["task_count"] == 1
    assert report["runs"]["no_rag"]["cross_file"]["metrics"]["task_count"] == 1
    assert "completion_rate" in report["cross_file_delta"]
    assert report["cross_file_repetitions"]["count"] == 1
    assert report["cross_file_repetitions"]["non_negative_count"] == 1
    assert report["cross_file_repetitions"]["all_non_negative"] is True
    assert report["quality_evidence"]["cross_file_improvement_stable"] is False

    markdown = render_markdown(report)
    assert "Cross-file completion" in markdown
    assert "real-model" in markdown or "real model" in markdown.lower()


def test_rag_quality_gate_requires_repeatable_non_regression() -> None:
    assert _stable_cross_file_improvement(
        2, {"all_non_negative": True, "mean_delta": 0.25}
    ) is True
    assert _stable_cross_file_improvement(
        1, {"all_non_negative": True, "mean_delta": 0.25}
    ) is False
    assert _stable_cross_file_improvement(
        3, {"all_non_negative": False, "mean_delta": 0.25}
    ) is False
