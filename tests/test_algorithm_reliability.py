from __future__ import annotations

from pathlib import Path

from coding_agent.algorithm.reliability import (
    build_report,
    get_report,
    list_reports,
    persist_report,
    render_markdown,
)


def _tool_result() -> dict[str, object]:
    return {
        "ok": False,
        "code": "ALGORITHM_JUDGE_FAILED",
        "message": "1 case failed",
        "data": {
            "judge": {
                "seed": 7,
                "total": 2,
                "passed": 1,
                "failed": 1,
                "ok": False,
                "cases": [
                    {"label": "small", "status": "passed", "expected": "1", "actual": "1", "detail": ""},
                    {"label": "edge", "status": "wrong_answer", "expected": "2", "actual": "3", "detail": "normalized output differs"},
                ],
                "first_failure": {"label": "edge", "status": "wrong_answer", "detail": "normalized output differs"},
                "minimized_input": "0\n",
            }
        },
        "metadata": {"purpose": "verify"},
    }


def test_report_is_bounded_explainable_and_persisted(tmp_path: Path) -> None:
    (tmp_path / "solution.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
    report = build_report(
        session_id="session",
        turn_id="turn",
        step=3,
        event_sequence=9,
        arguments={
            "path": "solution.cpp",
            "command": "solution.exe",
            "cases": [
                {"label": "small", "input": "1\n"},
                {"label": "edge", "input": "0\n"},
            ],
        },
        result=_tool_result(),
        workspace_root=tmp_path,
    )
    assert report is not None
    assert report["summary"]["failed"] == 1
    assert report["cases"][1]["input"] == "0\n"
    assert report["evidence"]["level"] == "deterministic"
    assert report["summary"]["status"] == "FAILURE_FOUND"
    assert len(report["source"]["sha256"]) == 64
    assert len(report["cases"][1]["expected_hash"]) == 64

    target = persist_report(tmp_path, report)
    assert target.is_file()
    assert get_report(tmp_path, report["report_id"])["report_id"] == report["report_id"]
    assert list_reports(tmp_path)[0]["source"]["path"] == "solution.cpp"
    markdown = render_markdown(report)
    assert "## First failure" in markdown
    assert "0" in markdown
    assert "Source SHA-256" in markdown


def test_invalid_judge_payload_does_not_create_report() -> None:
    assert build_report(
        session_id="session",
        turn_id="turn",
        step=1,
        event_sequence=1,
        arguments={},
        result={"ok": True, "data": {}},
    ) is None
