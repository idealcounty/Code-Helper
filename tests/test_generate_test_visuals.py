from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_test_visuals import build_snapshot, format_bytes, load_json


def test_load_json_returns_empty_mapping_for_missing_file(tmp_path: Path) -> None:
    assert load_json(tmp_path / "missing.json") == {}


def test_format_bytes_uses_human_readable_units() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"


def test_build_snapshot_extracts_metrics_from_evidence_files(tmp_path: Path) -> None:
    quality = tmp_path / "quality"
    quality.mkdir()
    (quality / "manifest.json").write_text(
        json.dumps({"run_id": "run-1", "git_commit": "abc", "git_snapshot_sha256": "snap", "commands": [
            {"name": "pytest", "status": "passed", "duration_ms": 12},
            {"name": "security", "status": "passed", "duration_ms": 4},
        ]}), encoding="utf-8"
    )
    (quality / "coverage.xml").write_text(
        '<coverage line-rate="0.8" branch-rate="0.7" lines-valid="10" '
        'lines-covered="8" branches-valid="20" branches-covered="14"/>',
        encoding="utf-8",
    )
    (quality / "junit.xml").write_text('<testsuites><testsuite tests="3" failures="0"/></testsuites>', encoding="utf-8")
    performance = tmp_path / "performance.json"
    performance.write_text(json.dumps({"requests": 10, "concurrency": 2, "error_rate": 0.0,
                                       "throughput_per_second": 5.0,
                                       "latency_ms": {"p50": 10, "p95": 20, "p99": 30, "max": 40}}), encoding="utf-8")
    concurrency = tmp_path / "concurrency.json"
    concurrency.write_text(json.dumps({"sessions": 4, "concurrency": 2, "completion_rate": 1.0,
                                       "event_session_mismatches": 0, "throughput_per_second": 2.0}), encoding="utf-8")
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"repo_map": {"files_seen": 12, "selected": 4, "selected_chars": 100,
                                    "budget_chars": 200}, "context": {"history_chars_after_compaction": 150,
                                    "budget_chars": 200, "dropped_messages": 3}}), encoding="utf-8")
    desktop = tmp_path / "desktop.json"
    desktop.write_text(json.dumps({"passed": True, "file_count": 9, "executable_bytes": 2048,
                                   "launch_smoke": {"passed": True, "startup_ms": 321}}), encoding="utf-8")
    soak = tmp_path / "soak.json"
    soak.write_text(json.dumps({"duration_seconds": 60, "sessions": 100, "failures": 0,
                                "completion_rate": 1.0, "rss_peak_bytes": 1024 * 1024}), encoding="utf-8")

    snapshot = build_snapshot(
        quality_dir=quality,
        performance_path=performance,
        concurrency_path=concurrency,
        context_path=context,
        desktop_path=desktop,
        soak_paths=[soak],
    )

    assert snapshot["quality"]["passed"] == 2
    assert snapshot["quality"]["total"] == 2
    assert snapshot["tests"] == 3
    assert snapshot["coverage"]["line_percent"] == 80.0
    assert snapshot["coverage"]["branch_percent"] == 70.0
    assert snapshot["performance"]["p95_ms"] == 20.0
    assert snapshot["concurrency"]["completion_percent"] == 100.0
    assert snapshot["context"]["history_percent"] == 75.0
    assert snapshot["desktop"]["startup_ms"] == 321.0
    assert snapshot["soak"]["sessions"] == 100
