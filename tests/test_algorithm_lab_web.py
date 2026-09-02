from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent.algorithm.reliability import persist_report
from coding_agent.config import AppConfig
from coding_agent.web.app import create_app


def test_algorithm_lab_spec_and_report_routes(tmp_path: Path) -> None:
    app = create_app(AppConfig(api_key="test-key", base_url="https://example.invalid/v1"))
    with TestClient(app) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        parsed = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/spec",
            json={"text": "# Two Sum\n\nInput:\narray\n\nOutput:\nindex\n\nConstraints:\n- n <= 100"},
        )
        assert parsed.status_code == 200
        assert parsed.json()["spec"]["constraints"] == ["n <= 100"]
        assert parsed.json()["evidence"]["level"] == "estimated"
        assert parsed.json()["suggested_cases"]
        generated = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/cases",
            json={"text": "Constraints:\n1 <= n <= 4"},
        )
        assert generated.status_code == 200
        assert any(item["source"] == "boundary" for item in generated.json()["cases"])
        parsed_alias = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/spec/parse",
            json={"text": "Input:\nn <= 4"},
        )
        assert parsed_alias.status_code == 200

        # The run API also accepts the roadmap's descriptive source_path name.
        (tmp_path / "missing.py").write_text("print(1)\n", encoding="utf-8")
        alias_run = client.post(
            f"/api/sessions/{session_id}/algorithm-lab/runs",
            json={
                "source_path": "missing.py",
                "cases": [{"input": "1\n", "expected": "1\n"}],
                "candidate_command": "python missing.py",
            },
        )
        assert alias_run.status_code == 202
        alias_id = alias_run.json()["run_id"]
        for _ in range(100):
            alias_status = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs/{alias_id}/status")
            if not alias_status.json()["running"]:
                break
        assert alias_status.status_code == 200

        report = {
            "schema_version": 1,
            "report_id": "abc123",
            "created_at": "2026-08-31T00:00:00+00:00",
            "session_id": session_id,
            "turn_id": "turn",
            "step": 1,
            "event_sequence": 1,
            "source": {"path": "solution.cpp", "command": "solution.exe", "seed": 1},
            "summary": {"status": "verified_for_cases", "total": 1, "passed": 1, "failed": 0},
            "cases": [{"label": "sample", "status": "passed", "input": "1\n", "expected": "1", "actual": "1", "detail": ""}],
            "complexity": None,
            "evidence": {"level": "deterministic", "kind": "algorithm_judge"},
        }
        persist_report(tmp_path, report)
        runs = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs")
        assert runs.status_code == 200
        assert runs.json()["total"] >= 1
        assert any(item["report_id"] == "abc123" for item in runs.json()["runs"])

        detail = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs/abc123")
        assert detail.status_code == 200
        assert detail.json()["report"]["summary"]["passed"] == 1
        markdown = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs/abc123/markdown")
        assert markdown.status_code == 200
        assert "Algorithm Reliability Report" in markdown.text
        markdown_alias = client.get(f"/api/sessions/{session_id}/algorithm-lab/runs/abc123/report.md")
        assert markdown_alias.status_code == 200

        replay = client.get(f"/api/sessions/{session_id}/agent-lab/replay")
        assert replay.status_code == 200
        assert "steps" in replay.json()
        replay_view = client.get(f"/api/sessions/{session_id}/observability/replay")
        assert replay_view.status_code == 200
        assert replay_view.json()["presentation"]["summary"]["title"] in {"运行回放", "还没有工作记录"}
        bookmark = client.post(
            f"/api/sessions/{session_id}/replay/bookmarks",
            json={"turn_id": "turn", "step": 1, "label": "root cause"},
        )
        assert bookmark.status_code == 200
        assert client.get(f"/api/sessions/{session_id}/replay").json()["bookmarks"][0]["step"] == 1
        other = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        assert other.status_code == 200
        compared = client.post(
            "/api/replay/compare",
            json={"left_session_id": session_id, "right_session_id": other.json()["session_id"]},
        )
        assert compared.status_code == 200
        assert "estimated_context_tokens" in compared.json()["delta"]
        context = client.get(f"/api/sessions/{session_id}/context-compiler")
        assert context.status_code == 200
        assert {item["id"] for item in context.json()["sources"]} >= {"history", "repo_map", "tools"}
        context_view = client.get(f"/api/sessions/{session_id}/observability/context")
        assert context_view.status_code == 200
        assert context_view.json()["presentation"]["summary"]["title"] == "上下文编译"
        preferences = client.get(f"/api/sessions/{session_id}/context/preferences")
        assert preferences.status_code == 200
        changed = client.put(
            f"/api/sessions/{session_id}/context/preferences",
            json={"source_id": "repo_map", "enabled": False},
        )
        assert changed.status_code == 200
        assert changed.json()["sources"]["repo_map"]["enabled"] is False
        what_if = client.post(f"/api/sessions/{session_id}/context-compiler/what-if")
        assert what_if.status_code == 200
        assert "without_repo_map" in what_if.json()
        memory = client.get(f"/api/sessions/{session_id}/memory-governance")
        assert memory.status_code == 200
        assert memory.json()["evidence"]["kind"] == "append_only_memory_store"
        memory_view = client.get(f"/api/sessions/{session_id}/observability/memory")
        assert memory_view.status_code == 200
        assert memory_view.json()["presentation"]["summary"]["title"] == "记忆治理"
        assert client.get(f"/api/sessions/{session_id}/observability/unknown").status_code == 404
        assert client.get(f"/api/sessions/{session_id}/memory/governance").status_code == 200
        stored = app.state.session_manager.get(session_id).runtime.memory_store.remember(
            category="fact", content="Temporary workbench fact"
        )
        expiry = client.patch(
            f"/api/sessions/{session_id}/memory/{stored.id}",
            json={"action": "set_expiry", "expires_at": "2099-01-01T00:00:00+00:00"},
        )
        assert expiry.status_code == 200
        assert expiry.json()["memory"]["expires_at"].startswith("2099-")
        cleared = client.patch(
            f"/api/sessions/{session_id}/memory/{stored.id}",
            json={"action": "clear_expiry"},
        )
        assert cleared.status_code == 200
        assert cleared.json()["memory"]["expires_at"] is None
        assert client.post(
            f"/api/sessions/{session_id}/memory/candidates/bulk-resolve",
            json={"action": "reject", "candidate_ids": [], "confirm": True},
        ).status_code == 200
