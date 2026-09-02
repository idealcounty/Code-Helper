from __future__ import annotations

from pathlib import Path

from scripts.context_stress_smoke import _write_fixture, run_probe


def test_context_stress_probe_is_bounded_and_cache_aware() -> None:
    report = run_probe(
        file_count=12,
        message_count=24,
        message_chars=300,
        repo_map_chars=1_500,
        context_chars=2_000,
    )

    assert report["repo_map"]["files_seen"] == 14
    assert report["repo_map"]["selected_chars"] <= 1_500
    assert report["repo_map"]["warm_cache_hits"] >= 1
    assert report["file_summary"] == {
        "cache_hit": True,
        "invalidated_after_edit": True,
    }
    assert report["context"]["truncated"] is True
    assert report["context"]["summary_present"] is True
    assert report["context"]["dropped_messages"] > 0


def test_fixture_writer_keeps_files_inside_requested_root(tmp_path: Path) -> None:
    _write_fixture(tmp_path, 3)
    files = list(tmp_path.rglob("*.py"))
    assert len(files) == 5
    assert all(path.is_relative_to(tmp_path) for path in files)
