from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from coding_agent.config import AppConfig
from coding_agent.events import AgentEvent
from coding_agent.model import ModelResponse, ToolCall
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
    assert 'id="userMenuButton"' in index.text
    assert 'id="settingsPage"' in index.text
    assert 'id="settingsApiKey"' in index.text
    assert 'id="settingsSkillsList"' in index.text
    assert 'id="settingsLayoutMode"' in index.text
    assert 'id="focusSessionList"' in index.text
    assert 'id="focusFilesButton"' in index.text
    assert modern_styles.status_code == 200
    assert "silver-white engineering workspace" in modern_styles.text
    assert ".layout-focus .assistant-pane { grid-column: 3; grid-row: 1;" in modern_styles.text
    assert "grid-template-rows: 64px 42px minmax(0, 1fr) 25px" in modern_styles.text
    assert rendering_script.status_code == 200
    assert "renderMarkdown" in rendering_script.text
    assert "highlightCode" in rendering_script.text
    assert frontend_bundle.status_code == 200
    assert frontend_bundle.headers["cache-control"] == "no-store, max-age=0"
    assert frontend_bundle.text.index("CodeHelperRendering") < frontend_bundle.text.index("const state")
    assert "file-preview-notice" in frontend_bundle.text
    assert "grantButton" in frontend_bundle.text
    assert 'case "model_progress"' in frontend_bundle.text
    assert "reconcileRunState(sessionId," in frontend_bundle.text
    assert "elements.newSessionButton.disabled = !state.workspace;" in frontend_bundle.text
    assert "阶段耗时" in frontend_bundle.text
    assert "exportTraceButton" in index.text
    assert "SESSION TOKENS" in frontend_bundle.text
    assert "session-token" in modern_styles.text
    assert "runEpoch" in frontend_bundle.text
    assert "pendingUserEchoes" in frontend_bundle.text
    assert "approvalPolicySelect" in frontend_bundle.text
    assert 'case "approval_policy_changed"' in frontend_bundle.text
    assert "请求批准" in index.text
    assert "帮我批准" in index.text
    assert "完全放开" in index.text
    assert "本会话允许" in index.text
    assert 'data-resize="explorer"' in index.text
    assert 'data-resize="assistant"' in index.text
    assert 'data-resize="threads"' in index.text
    assert "panel-resizer" in modern_styles.text
    assert "code-helper.panel-layout.v1" in frontend_bundle.text
    assert "initializePanelResizers" in frontend_bundle.text
    assert "localStorage.setItem(PANEL_LAYOUT_KEY" in frontend_bundle.text
    assert "code-helper.workspace-state.v1" in frontend_bundle.text
    assert "saveWorkspaceState" in frontend_bundle.text
    assert "restoreLastWorkspace" in frontend_bundle.text
    assert "已恢复上次工作区" in frontend_bundle.text
    assert 'api("/api/settings")' in frontend_bundle.text
    assert "saveSettings" in frontend_bundle.text
    assert "applyLayoutMode" in frontend_bundle.text
    assert 'classList.toggle("layout-focus"' in frontend_bundle.text
    assert 'return "focusSidebar"' in frontend_bundle.text
    assert 'return "focusFile"' in frontend_bundle.text
    assert "setPointerCapture" in frontend_bundle.text
    assert "browseDefaultWorkspace" in frontend_bundle.text
    assert "approvalPolicy" not in frontend_bundle.text.split("function saveWorkspaceState()", 1)[1].split("function clearWorkspaceState()", 1)[0]
    assert "copyTextToClipboard" in frontend_bundle.text
    assert "window.pywebview?.api?.copy_text" in frontend_bundle.text
    assert ':scope > .tree-children' not in frontend_bundle.text


def test_shared_password_protects_http_and_websocket() -> None:
    application = create_app(
        _config(),
        access_username="reviewer",
        access_password="long-test-password",
    )
    token = base64.b64encode(b"reviewer:long-test-password").decode("ascii")
    with TestClient(application) as client:
        missing = client.get("/")
        wrong = client.get("/", auth=("reviewer", "wrong"))
        allowed = client.get("/", auth=("reviewer", "long-test-password"))
        with pytest.raises(WebSocketDisconnect) as denied_socket:
            with client.websocket_connect("/ws/sessions/missing"):
                pass
        with pytest.raises(WebSocketDisconnect) as missing_session:
            with client.websocket_connect(
                "/ws/sessions/missing",
                headers={"Authorization": f"Basic {token}"},
            ):
                pass

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Basic")
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert denied_socket.value.code == 4401
    assert missing_session.value.code == 4404


def test_server_workspace_root_blocks_other_directories(tmp_path: Path) -> None:
    allowed_root = tmp_path / "workspaces"
    project = allowed_root / "demo"
    outside = tmp_path / "private"
    project.mkdir(parents=True)
    outside.mkdir()
    config = AppConfig(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        server_workspace_root=allowed_root,
    )

    with TestClient(create_app(config)) as client:
        root_listing = client.get("/api/fs/browse")
        blocked_browse = client.get("/api/fs/browse", params={"path": str(outside)})
        blocked_session = client.post(
            "/api/sessions", json={"workspace": str(outside), "mode": "ask"}
        )
        allowed_session = client.post(
            "/api/sessions", json={"workspace": str(project), "mode": "ask"}
        )

    assert root_listing.status_code == 200
    assert root_listing.json()["path"] == str(allowed_root.resolve())
    assert root_listing.json()["parent"] is None
    assert [item["name"] for item in root_listing.json()["entries"]] == ["demo"]
    assert blocked_browse.status_code == 400
    assert blocked_session.status_code == 400
    assert "server workspace root" in blocked_session.json()["detail"]
    assert allowed_session.status_code == 200


def test_settings_api_persists_defaults_without_returning_secret(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    with TestClient(create_app(_config(), settings_path=settings_path)) as client:
        initial = client.get("/api/settings")
        skill_names = [item["name"] for item in initial.json()["skills"]]
        selected = skill_names[:1]
        updated = client.post(
            "/api/settings",
            json={
                "api_key": "new-private-key",
                "default_workspace": str(tmp_path),
                "default_mode": "plan",
                "default_reasoning_profile": "balanced",
                "default_task_profile": "algorithm",
                "default_approval_policy": "auto",
                "default_layout_mode": "focus",
                "enabled_skills": selected,
            },
        )
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        health = client.get("/api/health")

    assert updated.status_code == 200
    assert updated.json()["api_key_configured"] is True
    assert updated.json()["default_layout_mode"] == "focus"
    assert updated.json()["api_key_hint"].endswith("-key")
    assert "new-private-key" not in updated.text
    assert created.json()["mode"] == "plan"
    assert created.json()["reasoning_profile"] == "balanced"
    assert created.json()["task_profile"] == "algorithm"
    assert created.json()["approval_policy"] == "auto"
    assert health.json()["api_key_configured"] is True
    persisted = settings_path.read_text(encoding="utf-8")
    assert "new-private-key" in persisted
    assert '"enabled_skills"' in persisted
    assert '"default_layout_mode": "focus"' in persisted


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


def test_trace_export_endpoint_returns_redacted_span_document(tmp_path: Path) -> None:
    with TestClient(create_app(_config())) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"})
        session_id = created.json()["session_id"]
        runtime = client.app.state.session_manager.get(session_id).runtime
        runtime.event_store.append(
            AgentEvent(
                type="span_started",
                session_id=session_id,
                turn_id=runtime.state.turn_id,
                payload={"span_id": "span-1", "kind": "model_request", "secret": "x"},
            )
        )
        runtime.event_store.append(
            AgentEvent(
                type="span_finished",
                session_id=session_id,
                turn_id=runtime.state.turn_id,
                payload={"span_id": "span-1", "kind": "model_request", "duration_ms": 4},
            )
        )
        response = client.get(f"/api/sessions/{session_id}/trace")

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["source"] == "code-helper"
    assert body["metadata"]["span_count"] == 1
    assert body["traceEvents"][0]["dur"] == 4000.0
    assert "secret" not in body["traceEvents"][0]["args"]


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
    assert intelligence.json()["observability"]["spans"] == []
    assert intelligence.json()["verification_config"] == {
        "commands": [],
        "rules": [],
        "active_commands": [],
        "diagnostics": [],
    }
    assert intelligence.json()["storage"]["events"]["max_files"] == 256
    assert intelligence.json()["storage"]["tool_results"]["max_files"] == 512
    assert changed.json() == {"profile": "fast", "reasoning_effort": "low"}
    assert details.json()["reasoning_profile"] == "fast"


def test_intelligence_aggregates_span_durations(tmp_path: Path) -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        session_id = created.json()["session_id"]
        runtime = app.state.session_manager.get(session_id).runtime
        runtime.event_store.append(
            AgentEvent(
                type="span_started",
                session_id=session_id,
                turn_id="turn-test",
                payload={"span_id": "span-1", "kind": "model_request"},
            )
        )
        runtime.event_store.append(
            AgentEvent(
                type="span_finished",
                session_id=session_id,
                turn_id="turn-test",
                payload={"span_id": "span-1", "kind": "model_request", "duration_ms": 12.5},
            )
        )
        intelligence = client.get(f"/api/sessions/{session_id}/intelligence").json()

    assert intelligence["observability"]["active_spans"] == 0
    assert intelligence["observability"]["spans"] == [
        {
            "kind": "model_request",
            "count": 1,
            "total_duration_ms": 12.5,
            "average_duration_ms": 12.5,
            "max_duration_ms": 12.5,
            "p50_duration_ms": 12.5,
            "p95_duration_ms": 12.5,
        }
    ]


def test_intelligence_keeps_lifecycle_hook_spans_separate(tmp_path: Path) -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        session_id = created.json()["session_id"]
        runtime = app.state.session_manager.get(session_id).runtime
        for span_id, hook, duration in (
            ("span-hook-a", "verification_hook", 3.0),
            ("span-hook-b", "task_end_hook", 5.0),
        ):
            runtime.event_store.append(
                AgentEvent(
                    type="span_started",
                    session_id=session_id,
                    turn_id="turn-test",
                    payload={
                        "span_id": span_id,
                        "kind": "hook",
                        "lifecycle": "OnVerification",
                        "hook": hook,
                    },
                )
            )
            runtime.event_store.append(
                AgentEvent(
                    type="span_finished",
                    session_id=session_id,
                    turn_id="turn-test",
                    payload={
                        "span_id": span_id,
                        "kind": "hook",
                        "lifecycle": "OnVerification",
                        "hook": hook,
                        "duration_ms": duration,
                    },
                )
            )
        intelligence = client.get(f"/api/sessions/{session_id}/intelligence").json()

    spans = intelligence["observability"]["spans"]
    assert [(item["hook"], item["count"], item["total_duration_ms"]) for item in spans] == [
        ("task_end_hook", 1, 5.0),
        ("verification_hook", 1, 3.0),
    ]


def test_intelligence_reports_cancellation_latency(tmp_path: Path) -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        session_id = created.json()["session_id"]
        runtime = app.state.session_manager.get(session_id).runtime
        runtime.event_store.append(
            AgentEvent(
                type="run_cancel_requested",
                session_id=session_id,
                turn_id="turn-test",
                timestamp="2026-08-30T00:00:00+00:00",
            )
        )
        runtime.event_store.append(
            AgentEvent(
                type="run_cancelled",
                session_id=session_id,
                turn_id="turn-test",
                timestamp="2026-08-30T00:00:00.125+00:00",
            )
        )
        intelligence = client.get(f"/api/sessions/{session_id}/intelligence").json()

    assert intelligence["observability"]["cancellation"] == {
        "requests": 1,
        "completed": 1,
        "samples_ms": [125.0],
        "p50_ms": 125.0,
        "p95_ms": 125.0,
        "average_ms": 125.0,
        "max_ms": 125.0,
    }


def test_session_scoped_approval_grant_is_limited_and_revocable(tmp_path: Path) -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        session = app.state.session_manager.get(session_id)
        session.runtime.state.pending_approval = {
            "call": {
                "id": "call-1",
                "name": "write_file",
                "arguments": {"path": "src/output.py", "content": "value = 1\n"},
            },
            "remaining": [],
            "reason": "File changes require approval",
        }

        resolved = client.post(
            f"/api/sessions/{session_id}/approval",
            json={"tool_call_id": "call-1", "approved": True, "scope": "session"},
        )
        grants = client.get(f"/api/sessions/{session_id}/permissions")
        grant_id = grants.json()["grants"][0]["grant_id"]
        revoked = client.delete(f"/api/sessions/{session_id}/permissions/{grant_id}")

    assert resolved.status_code == 200
    assert resolved.json()["scope"] == "session"
    assert resolved.json()["grant"]["capabilities"] == ["workspace.write"]
    granted_path = Path(resolved.json()["grant"]["path_prefix"])
    assert granted_path.name == "output.py"
    assert granted_path.parent.name == "src"
    assert grants.status_code == 200
    assert len(grants.json()["grants"]) == 1
    assert revoked.json() == {"revoked": True, "grant_id": grant_id}


def test_session_approval_policy_is_configurable_and_observable(tmp_path: Path) -> None:
    app = create_app(_config())
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        changed = client.post(
            f"/api/sessions/{session_id}/approval-policy",
            json={"policy": "auto"},
        )
        details = client.get(f"/api/sessions/{session_id}")
        permissions = client.get(f"/api/sessions/{session_id}/permissions")
        intelligence = client.get(f"/api/sessions/{session_id}/intelligence")
        events = client.get(f"/api/sessions/{session_id}/events").json()

    assert created.json()["approval_policy"] == "ask"
    assert changed.json()["approval_policy"] == "auto"
    assert changed.json()["approved_pending"] is False
    assert details.json()["approval_policy"] == "auto"
    assert permissions.json()["approval_policy"] == "auto"
    assert intelligence.json()["permissions"]["approval_policy"] == "auto"
    assert events[-1]["type"] == "approval_policy_changed"


def test_switching_to_auto_approves_the_current_pending_operation(
    tmp_path: Path,
) -> None:
    model = ApprovalWriteModel()
    app = create_app(_config(), model_client_factory=lambda: model)
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        sent = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "Create generated.md"},
        )
        assert sent.status_code == 202

        deadline = time.monotonic() + 2
        details = client.get(f"/api/sessions/{session_id}").json()
        while details["pending_approval"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
            details = client.get(f"/api/sessions/{session_id}").json()

        changed = client.post(
            f"/api/sessions/{session_id}/approval-policy",
            json={"policy": "auto"},
        )
        while not (tmp_path / "generated.md").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        client.post(f"/api/sessions/{session_id}/cancel")
        events = client.get(f"/api/sessions/{session_id}/events").json()

    assert details["pending_approval"]["call"]["id"] == "write-1"
    assert changed.status_code == 200
    assert changed.json()["approved_pending"] is True
    assert (tmp_path / "generated.md").read_text(encoding="utf-8") == "# Generated\n"
    approval = next(event for event in events if event["type"] == "approval_result")
    assert approval["payload"]["approved"] is True


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


class ApprovalWriteModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        "write-1",
                        "write_file",
                        {"path": "generated.md", "content": "# Generated\n"},
                    )
                ]
            )
        await asyncio.sleep(60)
        return ModelResponse(content="done")


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
        original_session = application.state.session_manager.get(session_id)
        original_task = original_session.task

        reopened = client.post(
            "/api/sessions",
            json={
                "workspace": str(tmp_path),
                "mode": "ask",
                "session_id": session_id,
            },
        )
        assert reopened.status_code == 200
        assert application.state.session_manager.get(session_id) is original_session
        assert original_session.task is original_task

        cancelled = client.post(f"/api/sessions/{session_id}/cancel")
        deadline = time.monotonic() + 2
        details = client.get(f"/api/sessions/{session_id}").json()
        while details["running"] and time.monotonic() < deadline:
            time.sleep(0.02)
            details = client.get(f"/api/sessions/{session_id}").json()
        events = client.get(f"/api/sessions/{session_id}/events").json()

    assert cancelled.status_code == 200
    assert cancelled.json()["force_after_seconds"] == 2.0
    assert details["running"] is False
    assert details["status"] == "cancelled"
    assert model.closed.is_set()
    event_types = [event["type"] for event in events]
    assert "run_cancel_requested" in event_types
    assert "run_cancelled" in event_types


def test_cancel_endpoint_is_noop_after_run_finished(tmp_path: Path) -> None:
    application = create_app(_config())
    with TestClient(application) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"}
        )
        session_id = created.json()["session_id"]

        cancelled = client.post(f"/api/sessions/{session_id}/cancel")
        events = client.get(f"/api/sessions/{session_id}/events").json()

    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is False
    assert cancelled.json()["already_finished"] is True
    assert events == []


def test_restore_endpoint_requires_second_confirmation_for_external_edits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sample.py"
    path.write_text("before\n", encoding="utf-8")
    application = create_app(_config())
    with TestClient(application) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "act"}
        )
        session_id = created.json()["session_id"]
        session = application.state.session_manager.get(session_id)
        state = session.runtime.state
        checkpoint = session.runtime.checkpoint_manager
        checkpoint.capture(state.turn_id, "sample.py")
        path.write_text("agent edit\n", encoding="utf-8")
        checkpoint.record_mutation(
            state.turn_id, "sample.py", sequence=3, tool="apply_patch"
        )
        path.write_text("user edit\n", encoding="utf-8")

        rejected = client.post(
            f"/api/sessions/{session_id}/restore", json={"force": False}
        )
        conflict = rejected.json()["detail"]["conflicts"][0]
        forced = client.post(
            f"/api/sessions/{session_id}/restore",
            json={
                "force": True,
                "confirmed_hashes": {"sample.py": conflict["current_sha256"]},
            },
        )
        events = client.get(f"/api/sessions/{session_id}/events").json()

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "RESTORE_CONFLICT"
    assert rejected.json()["detail"]["conflicts"][0]["path"] == "sample.py"
    assert forced.status_code == 200
    assert forced.json()["forced"] is True
    assert path.read_text(encoding="utf-8") == "before\n"
    restored_event = next(
        event for event in events if event["type"] == "checkpoint_restored"
    )
    assert restored_event["payload"]["forced"] is True


def test_intelligence_exposes_structured_verification_evidence(tmp_path: Path) -> None:
    application = create_app(_config())
    with TestClient(application) as client:
        created = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"}
        )
        session_id = created.json()["session_id"]
        session = application.state.session_manager.get(session_id)
        session.runtime.state.verification_evidence.append(
            {
                "command": "python -m pytest -q",
                "kind": "test",
                "source": "related_test_inferred",
                "accepted": True,
                "reason": "Recognized test command",
            }
        )

        intelligence = client.get(
            f"/api/sessions/{session_id}/intelligence"
        ).json()

    assert intelligence["verification"]["evidence"][0]["kind"] == "test"
    assert intelligence["verification"]["evidence"][0]["accepted"] is True


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
            # Span telemetry adds lifecycle events; wait for the terminal
            # event instead of assuming a fixed event count.
            for _ in range(64):
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


def test_websocket_supports_multiple_turns_in_same_session(tmp_path: Path) -> None:
    application = create_app(_config(), model_client_factory=FinalAnswerModel)
    with TestClient(application) as client:
        response = client.post(
            "/api/sessions", json={"workspace": str(tmp_path), "mode": "ask"}
        )
        session_id = response.json()["session_id"]

        observed_messages: list[str] = []
        finished_turns = 0
        with client.websocket_connect(f"/ws/sessions/{session_id}") as websocket:
            for content in ("First question", "Second question"):
                sent = client.post(
                    f"/api/sessions/{session_id}/messages",
                    json={"content": content},
                )
                assert sent.status_code == 202
                while True:
                    event = websocket.receive_json()
                    if event["type"] == "turn_started":
                        observed_messages.append(event["payload"]["message"])
                    if event["type"] == "turn_finished":
                        finished_turns += 1
                        break

    assert observed_messages == ["First question", "Second question"]
    assert finished_turns == 2
