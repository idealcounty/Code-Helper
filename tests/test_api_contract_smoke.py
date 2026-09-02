from __future__ import annotations

from scripts.api_contract_smoke import render_markdown


def test_api_contract_report_is_redacted_and_human_readable() -> None:
    report = {
        "git_commit": "abc123",
        "git_dirty": True,
        "git_snapshot_sha256": "0" * 64,
        "passed": True,
        "duration_ms": 12.3,
        "checks": [
            {"name": "health_contract", "passed": True, "status_code": 200},
            {"name": "websocket_live_contract", "passed": True, "live_event_count": 3},
        ],
        "failed_checks": [],
    }

    markdown = render_markdown(report)

    assert "API 契约与 WebSocket 集成烟测" in markdown
    assert "health_contract" in markdown
    assert "websocket_live_contract" in markdown
    assert "abc123" in markdown
    assert "api_key" not in markdown.lower()
    assert "secret" not in markdown.lower()


def test_api_contract_report_does_not_emit_response_payloads() -> None:
    markdown = render_markdown(
        {
            "passed": False,
            "checks": [
                {
                    "name": "invalid_message_contract",
                    "passed": False,
                    "status_code": 422,
                    "error": "status or JSON shape mismatch",
                }
            ],
            "failed_checks": ["invalid_message_contract"],
        }
    )

    assert "status or JSON shape mismatch" not in markdown
    assert "422" in markdown
