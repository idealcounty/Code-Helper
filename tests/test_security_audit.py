from __future__ import annotations

from pathlib import Path

from scripts.security_audit import audit_history_text, audit_text, render_markdown


def test_security_audit_allows_documented_placeholders(tmp_path: Path) -> None:
    path = tmp_path / "example.env"
    findings = audit_text(
        path,
        "CODE_HELPER_API_KEY=<YOUR_API_KEY>\nCODE_HELPER_BASE_URL=${CODE_HELPER_BASE_URL}\n",
        root=tmp_path,
    )
    assert findings == []


def test_security_audit_finds_plausible_key_without_printing_value(tmp_path: Path) -> None:
    path = tmp_path / "config.txt"
    value = "ds_" + "A1" * 20
    findings = audit_text(path, f"CODE_HELPER_API_KEY={value}\n", root=tmp_path)
    assert findings
    assert findings[0].code == "POSSIBLE_SECRET"
    assert value not in render_markdown(findings, passed=False)


def test_security_audit_reports_personal_paths_as_nonblocking(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    findings = audit_text(path, "C:" + r"\Users\someone\workspace\Code-Helper", root=tmp_path)
    assert [item.code for item in findings] == ["PERSONAL_PATH"]
    assert findings[0].severity == "medium"


def test_security_audit_scans_history_without_echoing_secret() -> None:
    value = "ds_" + "B2" * 20
    findings = audit_history_text(f"+ CODE_HELPER_API_KEY={value}\n")
    assert findings
    assert findings[0].code == "HISTORY_POSSIBLE_SECRET"
    assert value not in render_markdown(findings, passed=False)
