from __future__ import annotations

from evals.retrieval_benchmark import render_markdown, run_benchmark


def test_retrieval_benchmark_reports_gold_metrics() -> None:
    report = run_benchmark()
    assert report["task_count"] == 9
    lexical = report["metrics"]["lexical"]
    graph = report["metrics"]["dependency_graph"]
    assert graph["recall_at_5"] >= lexical["recall_at_5"]
    assert graph["first_relevant_rate"] >= lexical["first_relevant_rate"]
    assert all(row["gold_files"] for row in report["tasks"])
    hidden = next(row for row in report["tasks"] if row["task_id"] == "dependency_centrality_hidden")
    assert hidden["lexical"]["first_relevant"] is False
    assert hidden["dependency_graph"]["first_relevant"] is True


def test_retrieval_benchmark_markdown_is_human_readable() -> None:
    markdown = render_markdown(run_benchmark())
    assert "Recall@5" in markdown
    assert "词法+依赖图" in markdown
