from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent.config import AppConfig
from coding_agent.model import ModelResponse
from coding_agent.web.app import create_app


def _config() -> AppConfig:
    return AppConfig(api_key="test-key", base_url="https://example.invalid/v1")


def test_abandon_interrupted_call_is_explicit_and_persisted(tmp_path: Path) -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        runtime = app.state.session_manager.get(session_id).runtime
        runtime.state.interrupted_tool_calls = [
            {
                "id": "write-1",
                "name": "write_file",
                "arguments": {"path": "out.txt", "content": "unsafe\n"},
                "code": "INTERRUPTED_UNKNOWN",
            }
        ]

        response = client.post(
            f"/api/sessions/{session_id}/recovery",
            json={"action": "abandon", "tool_call_id": "write-1"},
        )
        details = client.get(f"/api/sessions/{session_id}").json()
        events = client.get(f"/api/sessions/{session_id}/events").json()

    assert response.status_code == 202
    assert details["interrupted_tool_calls"] == []
    assert not (tmp_path / "out.txt").exists()
    assert any(event["type"] == "recovery_abandoned" for event in events)


def test_retry_write_requires_explicit_confirmation(tmp_path: Path) -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        runtime = app.state.session_manager.get(session_id).runtime
        runtime.state.interrupted_tool_calls = [
            {
                "id": "write-1",
                "name": "write_file",
                "arguments": {"path": "out.txt", "content": "unsafe\n"},
                "code": "INTERRUPTED_UNKNOWN",
            }
        ]

        response = client.post(
            f"/api/sessions/{session_id}/recovery",
            json={"action": "retry", "tool_call_id": "write-1"},
        )

    assert response.status_code == 400
    assert "confirm explicitly" in response.json()["detail"]
    assert not (tmp_path / "out.txt").exists()


class _FinalAnswerModel:
    async def complete(self, **_: object) -> ModelResponse:
        return ModelResponse(content="Recovered and ready to continue.")


def test_retry_read_call_runs_once_and_continues_the_session(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    app = create_app(
        _config(), model_client_factory=lambda: _FinalAnswerModel()
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        runtime = app.state.session_manager.get(session_id).runtime
        runtime.state.interrupted_tool_calls = [
            {
                "id": "read-1",
                "name": "read_file",
                "arguments": {"path": "sample.py"},
                "code": "INTERRUPTED_UNKNOWN",
            }
        ]

        response = client.post(
            f"/api/sessions/{session_id}/recovery",
            json={"action": "retry", "tool_call_id": "read-1"},
        )
        for _ in range(50):
            details = client.get(f"/api/sessions/{session_id}").json()
            if not details["running"]:
                break

        events = client.get(f"/api/sessions/{session_id}/events").json()

    assert response.status_code == 202
    assert details["status"] == "completed"
    assert [event["type"] for event in events].count("tool_result") == 1
    assert any(event["type"] == "recovery_retry_requested" for event in events)
