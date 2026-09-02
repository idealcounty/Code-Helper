from __future__ import annotations

from pathlib import Path

from scripts.coverage_baseline import executable_lines


def test_executable_line_detection_uses_positive_source_lines(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n\nif value:\n    value += 1\n", encoding="utf-8")

    lines = executable_lines(source)

    assert 0 not in lines
    assert {1, 3, 4} <= lines
