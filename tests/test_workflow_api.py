from __future__ import annotations

from pathlib import Path
import asyncio

from fastapi.testclient import TestClient

from coding_agent.config import AppConfig
from coding_agent.events import AgentEvent
from coding_agent.web.app import create_app


def test_session_intelligence_and_report_expose_workflow_state(tmp_path: Path) -> None:
    app = create_app(AppConfig(api_key="test-key", base_url="https://example.invalid/v1"))
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path), "mode": "act"})
        session_id = created.json()["session_id"]
        runtime = app.state.session_manager.get(session_id).runtime
        runtime.state.workflow_name = "add-feature"
        runtime.state.workflow_stage = "plan"
        runtime.state.loaded_skills.add("add-feature")
        runtime.state.plan = [{"step": "实现功能", "status": "in_progress", "acceptance": "验证通过"}]

        details = client.get(f"/api/sessions/{session_id}").json()
        intelligence = client.get(f"/api/sessions/{session_id}/intelligence").json()
        report = client.get(f"/api/sessions/{session_id}/report").json()

    expected = {
        "name": "add-feature",
        "stage": "plan",
        "loaded_skills": ["add-feature"],
    }
    assert details["workflow"] == expected
    assert intelligence["workflow"] == {
        **expected,
        "acceptance": ["验证通过"],
        "active_steps": ["实现功能"],
    }
    assert report["workflow"] == {
        **expected,
        "acceptance": ["验证通过"],
        "active_steps": ["实现功能"],
    }


def test_workflow_projection_survives_session_manager_recreation(
    tmp_path: Path,
) -> None:
    application = create_app(AppConfig(api_key="test-key", base_url="https://example.invalid/v1"))
    with TestClient(application) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path), "mode": "act"})
        session_id = created.json()["session_id"]
        manager = application.state.session_manager
        session = manager.get(session_id)
        state = session.runtime.state

        async def publish_workflow_events() -> None:
            for event_type, payload in (
                ("turn_started", {"message": "修复缺陷"}),
                ("skill_loaded", {"name": "bug-fix"}),
                (
                    "workflow_selected",
                    {"name": "bug-fix", "stage": "inspect"},
                ),
                (
                    "workflow_stage_changed",
                    {
                        "from": "inspect",
                        "to": "implement",
                        "reason": "first mutation",
                    },
                ),
            ):
                event = await session.runtime.event_bus.publish(
                    AgentEvent(
                        type=event_type,
                        session_id=session_id,
                        turn_id=state.turn_id,
                        payload=payload,
                    )
                )
                state.apply_event(event.to_dict())

        asyncio.run(publish_workflow_events())
        del manager.sessions[session_id]
        manager.create(str(tmp_path), "act", session_id=session_id)

        restored = client.get(f"/api/sessions/{session_id}").json()

    assert restored["workflow"] == {
        "name": "bug-fix",
        "stage": "implement",
        "loaded_skills": ["bug-fix"],
    }
