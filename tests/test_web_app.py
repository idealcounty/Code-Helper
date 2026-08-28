from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from coding_agent.config import AppConfig
from coding_agent.model import ModelResponse
from coding_agent.web.app import create_app
from coding_agent.session import AgentStatus


def _config() -> AppConfig:
    return AppConfig(api_key="test-key", base_url="https://example.invalid/v1")


def test_health_and_static_index() -> None:
    with TestClient(create_app(_config())) as client:
        health = client.get("/api/health")
        index = client.get("/")
        modern_styles = client.get("/static/modern.css")
        rendering_script = client.get("/static/rendering.js")
        frontend_bundle = client.get("/assets/app.bundle.js")

    assert health.status_code == 200
    assert health.json()["api_key_configured"] is True
    assert health.json()["provider"] == "deepseek"
    assert index.status_code == 200
    assert "Code Helper" in index.text
    assert 'href="/static/modern.css?v=' in index.text
    assert 'src="/assets/app.bundle.js?v=' in index.text
    assert index.headers["cache-control"] == "no-store, max-age=0"
    assert "浏览文件夹" in index.text
    assert "代码编辑区" in index.text
    assert modern_styles.status_code == 200
    assert "silver-white engineering workspace" in modern_styles.text
    assert rendering_script.status_code == 200
    assert "renderMarkdown" in rendering_script.text
    assert "highlightCode" in rendering_script.text
    assert frontend_bundle.status_code == 200
    assert frontend_bundle.headers["cache-control"] == "no-store, max-age=0"
    assert frontend_bundle.text.index("CodeHelperRendering") < frontend_bundle.text.index("const state")
    assert "file-preview-notice" in frontend_bundle.text


def test_create_session_for_local_workspace(tmp_path: Path) -> None:
    with TestClient(create_app(_config())) as client:
        response = client.post(
            "/api/sessions",
            json={"workspace": str(tmp_path), "mode": "act"},
        )

        assert response.status_code == 200
        session_id = response.json()["session_id"]
        details = client.get(f"/api/sessions/{session_id}")

    assert details.status_code == 200
    assert details.json()["mode"] == "act"
    assert details.json()["running"] is False


def test_memory_control_endpoints(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(api_key="test-key", base_url="https://example.invalid/v1", user_memory_dir=tmp_path / "global-memory")
    app = create_app(config)
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"workspace": str(workspace), "mode": "act"})
        session_id = created.json()["session_id"]
        runtime = app.state.session_manager.get(session_id).runtime
        runtime.state.current_objective = "我希望以后先执行最小测试"
        runtime.summary_store.create(runtime.state, AgentStatus.COMPLETED, "done", runtime.memory_store)
        intelligence = client.get(f"/api/sessions/{session_id}/intelligence").json()
        candidate_id = intelligence["memory"]["summaries"]["candidates"][0]["id"]
        confirmed = client.post(f"/api/sessions/{session_id}/memory/candidates/{candidate_id}", json={"action": "confirm"})
        enabled = client.post(f"/api/sessions/{session_id}/user-memory/enabled", json={"enabled": True})
        exported = client.get(f"/api/sessions/{session_id}/user-memory/export")
        cleared = client.delete(f"/api/sessions/{session_id}/user-memory")

    assert confirmed.status_code == 200 and runtime.memory_store.stats()["count"] == 1
    assert enabled.json()["enabled"] is True
    assert exported.json()["scope"] == "user"
    assert cleared.status_code == 200


def test_session_report_exposes_completion_evidence(tmp_path: Path) -> None:
    with TestClient(create_app(_config())) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"})
        session_id = created.json()["session_id"]
        report = client.get(f"/api/sessions/{session_id}/report")
    assert report.status_code == 200
    body = report.json()
    assert body["session_id"] == session_id
    assert {"verification", "tool_stats", "plan"}.issubset(body)


def test_missing_workspace_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with TestClient(create_app(_config())) as client:
        response = client.post(
            "/api/sessions",
            json={"workspace": str(missing), "mode": "act"},
        )

    assert response.status_code == 400


def test_file_explorer_lists_safe_workspace_entries(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    with TestClient(create_app(_config())) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        root = client.get(f"/api/sessions/{session_id}/files")
        nested = client.get(
            f"/api/sessions/{session_id}/files", params={"path": "src"}
        )

    assert root.status_code == 200
    assert root.json()["entries"] == [
        {"name": "src", "path": "src", "kind": "directory"},
        {"name": "README.md", "path": "README.md", "kind": "file"},
    ]
    assert nested.json()["entries"] == [
        {"name": "app.py", "path": "src/app.py", "kind": "file"}
    ]


def test_file_explorer_cannot_escape_workspace(tmp_path: Path) -> None:
    with TestClient(create_app(_config())) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        response = client.get(
            f"/api/sessions/{session_id}/files", params={"path": ".."}
        )

    assert response.status_code == 400


def test_workspace_file_endpoint_returns_text_content(tmp_path: Path) -> None:
    source = tmp_path / "hello.py"
    source.write_bytes(b"print('hello')\n")

    with TestClient(create_app(_config())) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"}
        )
        session_id = created.json()["session_id"]
        response = client.get(
            f"/api/sessions/{session_id}/file", params={"path": "hello.py"}
        )

    assert response.status_code == 200
    assert response.json() == {
        "name": "hello.py",
        "path": "hello.py",
        "content": "print('hello')\n",
        "size": 15,
        "line_count": 1,
        "language": "python",
        "truncated": False,
    }


def test_binary_workspace_file_returns_previewable_status(tmp_path: Path) -> None:
    (tmp_path / "sample.bin").write_bytes(b"header\x00payload")

    with TestClient(create_app(_config())) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"}
        )
        session_id = created.json()["session_id"]
        response = client.get(
            f"/api/sessions/{session_id}/file", params={"path": "sample.bin"}
        )

    assert response.status_code == 415
    assert response.json()["detail"] == "二进制文件暂不支持文本预览"


def test_directory_browser_and_workspace_session_listing(tmp_path: Path) -> None:
    child = tmp_path / "project"
    child.mkdir()

    with TestClient(create_app(_config())) as client:
        browsed = client.get("/api/fs/browse", params={"path": str(tmp_path)})
        created = client.post(
            "/api/sessions", json={"workspace": str(child), "mode": "act"}
        )
        sessions = client.get(
            "/api/workspaces/sessions", params={"workspace": str(child)}
        )

    assert browsed.status_code == 200
    assert browsed.json()["entries"] == [
        {"name": "project", "path": str(child), "kind": "directory"}
    ]
    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == created.json()["session_id"]


def test_reasoning_profile_and_intelligence_endpoint(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_bytes(b"def main():\n    return 1\n")
    (tmp_path / "test_app.py").write_bytes(b"def test_main():\n    assert True\n")

    with TestClient(create_app(_config())) as client:
        created = client.post(
            "/api/sessions",
            json={
                "workspace": str(tmp_path),
                "mode": "act",
                "reasoning_profile": "deep",
            },
        )
        session_id = created.json()["session_id"]
        intelligence = client.get(f"/api/sessions/{session_id}/intelligence")
        changed = client.post(
            f"/api/sessions/{session_id}/reasoning",
            json={"profile": "fast"},
        )
        details = client.get(f"/api/sessions/{session_id}")

    assert created.json()["reasoning_profile"] == "deep"
    assert intelligence.status_code == 200
    assert intelligence.json()["repo_map"]["totals"]["files_seen"] == 2
    assert len(intelligence.json()["skills"]["available"]) == 3
    assert intelligence.json()["hooks"]["pipeline_enabled"] is True
    assert intelligence.json()["budget"]["max_seconds"] == 600.0
    assert intelligence.json()["budget"]["max_steps"] == 20
    assert changed.json() == {"profile": "fast", "reasoning_effort": "low"}
    assert details.json()["reasoning_profile"] == "fast"


def test_intelligence_endpoint_exposes_project_memory(tmp_path: Path) -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        session = app.state.session_manager.get(session_id)
        memory = session.runtime.memory_store.remember(
            category="decision",
            content="Use the local Web UI as the primary interface.",
            keywords=["web", "ui"],
            importance=4,
            source_session_id="older-session",
        )
        session.runtime.state.messages.append(
            {"role": "user", "content": "How should the web UI work?"}
        )
        session.runtime.context_manager.build(session.runtime.state, [])
        intelligence = client.get(f"/api/sessions/{session_id}/intelligence")

    body = intelligence.json()["memory"]
    assert intelligence.status_code == 200
    assert body["count"] == 1
    assert body["categories"]["decision"] == 1
    assert body["recent"][0]["id"] == memory.id
    assert body["recalled"][0]["id"] == memory.id


class FinalAnswerModel:
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        return ModelResponse(content="Hello from the self-written agent loop.")


class BlockingWebModel:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.closed = threading.Event()

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        self.started.set()
        try:
            await asyncio.sleep(60)
            return ModelResponse(content="too late")
        finally:
            self.closed.set()


def test_cancel_endpoint_interrupts_active_web_run(tmp_path: Path) -> None:
    model = BlockingWebModel()
    application = create_app(_config(), model_client_factory=lambda: model)
    with TestClient(application) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"}
        )
        session_id = created.json()["session_id"]
        sent = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "wait forever"},
        )
        assert sent.status_code == 202
        assert model.started.wait(timeout=1)

        cancelled = client.post(f"/api/sessions/{session_id}/cancel")
        deadline = time.monotonic() + 2
        details = client.get(f"/api/sessions/{session_id}").json()
        while details["running"] and time.monotonic() < deadline:
            time.sleep(0.02)
            details = client.get(f"/api/sessions/{session_id}").json()
        events = client.get(f"/api/sessions/{session_id}/events").json()

    assert cancelled.status_code == 200
    assert details["running"] is False
    assert details["status"] == "cancelled"
    assert model.closed.is_set()
    event_types = [event["type"] for event in events]
    assert "run_cancel_requested" in event_types
    assert "run_cancelled" in event_types


def test_websocket_receives_agent_events(tmp_path: Path) -> None:
    application = create_app(_config(), model_client_factory=FinalAnswerModel)
    with TestClient(application) as client:
        response = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"}
        )
        session_id = response.json()["session_id"]

        with client.websocket_connect(f"/ws/sessions/{session_id}") as websocket:
            sent = client.post(
                f"/api/sessions/{session_id}/messages",
                json={"content": "Say hello"},
            )
            assert sent.status_code == 202

            event_types: list[str] = []
            assistant_content = ""
            for _ in range(8):
                event = websocket.receive_json()
                event_types.append(event["type"])
                if event["type"] == "assistant_response":
                    assistant_content = event["payload"]["content"]
                if event["type"] == "turn_finished":
                    break

    assert "turn_started" in event_types
    assert "assistant_response" in event_types
    assert "turn_finished" in event_types
    assert assistant_content == "Hello from the self-written agent loop."
