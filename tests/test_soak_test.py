from __future__ import annotations

from scripts.soak_test import _process_metrics, render_markdown, run_soak


def test_process_metrics_has_portable_rss_and_cpu_fields() -> None:
    metrics = _process_metrics()
    assert set(metrics) == {"rss_bytes", "cpu_time_seconds"}
    assert metrics["rss_bytes"] is None or metrics["rss_bytes"] >= 0
    assert metrics["cpu_time_seconds"] is None or metrics["cpu_time_seconds"] >= 0


def test_soak_runs_multiple_deterministic_rounds() -> None:
    report = run_soak(duration_seconds=0.2, round_sessions=1, concurrency=1, timeout=5)
    assert report["rounds"] >= 1
    assert report["failures"] == 0
    assert "完成率" in render_markdown(report)
