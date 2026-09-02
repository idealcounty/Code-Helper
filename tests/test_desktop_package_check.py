from __future__ import annotations

from pathlib import Path

from scripts.desktop_package_check import inspect_package, launch_smoke


def test_desktop_package_check_reports_missing_files(tmp_path: Path) -> None:
    report = inspect_package(tmp_path)
    assert report["passed"] is False
    assert {item["code"] for item in report["findings"]} >= {"MISSING_FILE", "MISSING_RESOURCE"}


def test_desktop_package_check_accepts_required_layout(tmp_path: Path) -> None:
    (tmp_path / "code-helper.exe").write_bytes(b"fake exe")
    (tmp_path / ".env.example").write_text("CODE_HELPER_API_KEY=<YOUR_API_KEY>\n", encoding="utf-8")
    (tmp_path / "_internal").mkdir()
    (tmp_path / "coding_agent").mkdir()
    report = inspect_package(tmp_path)
    assert report["passed"] is True
    assert report["executable_sha256"]


def test_launch_smoke_reports_missing_executable_without_spawning(tmp_path: Path) -> None:
    report = launch_smoke(tmp_path, timeout_seconds=0.2)
    assert report["requested"] is True
    assert report["passed"] is False
    assert report["error"] in {"code-helper.exe is missing", "launch smoke is only supported on Windows"}
