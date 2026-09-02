from __future__ import annotations

from scripts.performance_smoke import percentile, render_markdown


def test_percentile_is_interpolated_and_bounded() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0) == 10
    assert percentile(values, 1) == 40
    assert percentile(values, 0.5) == 25


def test_performance_report_markdown_contains_latency_metrics() -> None:
    report = {
        "url": "http://localhost/api/health",
        "requests": 3,
        "concurrency": 2,
        "successes": 3,
        "errors": 0,
        "error_rate": 0,
        "throughput_per_second": 100,
        "latency_ms": {"p50": 2, "p95": 4, "p99": 5, "max": 6},
    }
    text = render_markdown(report)
    assert "P95" in text
    assert "错误率" in text
