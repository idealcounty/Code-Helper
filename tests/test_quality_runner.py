from __future__ import annotations

from pathlib import Path

from scripts.evidence_metadata import collect_metadata
from scripts.run_quality_tests import _default_commands, _write_summary, run_command


def test_quality_runner_records_command_and_log(tmp_path: Path) -> None:
    result = run_command(
        "smoke",
        ["python", "-c", "print('ok')"],
        tmp_path,
        timeout_seconds=10,
    )
    assert result["status"] == "passed"
    assert result["return_code"] == 0
    assert (tmp_path / "smoke.log").read_text(encoding="utf-8").strip() == "ok"


def test_quality_runner_summary_contains_counts(tmp_path: Path) -> None:
    manifest = {
        "run_id": "demo",
        "started_at": "now",
        "finished_at": "later",
        "git_commit": "abc",
        "git_dirty": False,
        "environment": {"os": "test", "python": "3.x", "provider": "scripted", "model": None},
        "commands": [{"name": "smoke", "status": "passed", "duration_ms": 1, "log": "smoke.log"}],
    }
    target = tmp_path / "summary.md"
    _write_summary(manifest, target)
    text = target.read_text(encoding="utf-8")
    assert "通过：**1**" in text
    assert "总体结论：**Passed**" in text


def test_evidence_metadata_is_safe_and_traceable() -> None:
    metadata = collect_metadata()
    assert set(metadata) >= {"git_commit", "git_dirty", "git_snapshot_sha256", "environment"}
    assert set(metadata["environment"]) >= {"os", "python", "cpu_count"}
    assert "api_key" not in str(metadata).lower()


def test_quality_runner_requests_branch_and_html_coverage_when_available(
    tmp_path: Path, monkeypatch
) -> None:
    import scripts.run_quality_tests as quality_runner

    original_find_spec = quality_runner.importlib.util.find_spec
    monkeypatch.setattr(
        quality_runner.importlib.util,
        "find_spec",
        lambda name: object() if name == "pytest_cov" else original_find_spec(name),
    )
    commands = {
        name: (command, required)
        for name, command, required in _default_commands(tmp_path)
    }
    pytest_command = commands["pytest"]

    assert "--cov-branch" in pytest_command[0]
    assert any("--cov-report=html:" in value for value in pytest_command[0])


def test_quality_runner_includes_superpowers_comparison_in_eval_commands(
    tmp_path: Path,
) -> None:
    from scripts.run_quality_tests import _eval_commands

    commands = {name: command for name, command, _ in _eval_commands(tmp_path)}
    assert "superpowers-comparison" in commands
    assert commands["superpowers-comparison"][1:3] == ["-m", "evals.superpowers_comparison"]
