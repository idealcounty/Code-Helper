from __future__ import annotations

from scripts.agent_concurrency_smoke import render_markdown, run_probe


def test_agent_concurrency_probe_keeps_sessions_isolated() -> None:
    report = run_probe(sessions=3, concurrency=2, timeout=5)
    assert report["failures"] == 0
    assert report["event_session_mismatches"] == 0
    assert report["completion_rate"] == 1
    assert "事件串扰" in render_markdown(report)
