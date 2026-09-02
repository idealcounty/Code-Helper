"""Run a deterministic HTTP/WebSocket contract smoke test.

The probe exercises the public Web API with a temporary workspace and a
ScriptedModel.  It records status-code/shape checks without writing response
bodies to the report, so temporary paths and test credentials cannot leak into
the evidence.  It never calls DeepSeek or touches a user workspace.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.evidence_metadata import collect_metadata
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from evidence_metadata import collect_metadata

from coding_agent.config import AppConfig
from coding_agent.model import ModelResponse
from coding_agent.web.app import create_app


class ScriptedModel:
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        del messages, tools, reasoning_effort
        return ModelResponse(content="contract smoke completed")


def _check(
    checks: list[dict[str, Any]],
    name: str,
    response: Any,
    expected_status: int,
    validator: Callable[[Any], bool] | None = None,
) -> Any:
    """Record a safe contract result without serializing response content."""

    passed = response.status_code == expected_status
    payload: Any = None
    if passed and validator is not None:
        try:
            payload = response.json()
            passed = bool(validator(payload))
        except (ValueError, TypeError):
            passed = False
    item: dict[str, Any] = {
        "name": name,
        "passed": passed,
        "status_code": response.status_code,
        "expected_status": expected_status,
    }
    if not passed:
        item["error"] = "status or JSON shape mismatch"
    checks.append(item)
    return payload


def run_contract(*, timeout: float = 5.0) -> dict[str, Any]:
    """Exercise core HTTP routes and one complete WebSocket turn."""

    from fastapi.testclient import TestClient

    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="code-helper-contract-") as directory:
        workspace = Path(directory)
        (workspace / "src" / "nested").mkdir(parents=True)
        (workspace / "README.md").write_text("# Contract fixture\n", encoding="utf-8")
        (workspace / "src" / "nested" / "sample.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace.parent / "outside-contract.txt").write_text("outside\n", encoding="utf-8")
        config = AppConfig(api_key="scripted-test-key", base_url="https://example.invalid/v1")
        application = create_app(config, model_client_factory=ScriptedModel)

        with TestClient(application) as client:
            health = _check(
                checks,
                "health_contract",
                client.get("/api/health"),
                200,
                lambda body: isinstance(body, dict) and body.get("ok") is True and "api_key" not in body,
            )
            _check(
                checks,
                "settings_read_contract",
                client.get("/api/settings"),
                200,
                lambda body: isinstance(body, dict) and "api_key_configured" in body,
            )
            created = _check(
                checks,
                "session_create_contract",
                client.post(
                    "/api/sessions",
                    json={
                        "workspace": directory,
                        "mode": "ask",
                        "reasoning_profile": "fast",
                        "task_profile": "project",
                        "approval_policy": "ask",
                    },
                ),
                200,
                lambda body: isinstance(body, dict) and isinstance(body.get("session_id"), str),
            )
            session_id = str((created or {}).get("session_id") or "")
            if not session_id:
                raise RuntimeError("session creation did not return a session id")

            details = _check(
                checks,
                "session_detail_contract",
                client.get(f"/api/sessions/{session_id}"),
                200,
                lambda body: isinstance(body, dict)
                and {"session_id", "mode", "task_profile", "budget", "running"}.issubset(body),
            )
            _check(
                checks,
                "workspace_files_contract",
                client.get(f"/api/sessions/{session_id}/files"),
                200,
                lambda body: isinstance(body, dict) and isinstance(body.get("entries"), list),
            )
            _check(
                checks,
                "nested_files_contract",
                client.get(f"/api/sessions/{session_id}/files", params={"path": "src"}),
                200,
                lambda body: isinstance(body, dict) and any(item.get("name") == "nested" for item in body.get("entries", [])),
            )
            _check(
                checks,
                "file_read_contract",
                client.get(f"/api/sessions/{session_id}/file", params={"path": "README.md"}),
                200,
                lambda body: isinstance(body, dict) and body.get("language") == "markdown" and body.get("truncated") is False,
            )
            _check(
                checks,
                "workspace_boundary_contract",
                client.get(f"/api/sessions/{session_id}/file", params={"path": "../outside-contract.txt"}),
                400,
            )
            _check(
                checks,
                "mode_switch_contract",
                client.post(f"/api/sessions/{session_id}/mode", json={"mode": "act"}),
                200,
                lambda body: isinstance(body, dict) and body.get("mode") == "act",
            )
            _check(
                checks,
                "reasoning_switch_contract",
                client.post(f"/api/sessions/{session_id}/reasoning", json={"profile": "balanced"}),
                200,
                lambda body: isinstance(body, dict) and body.get("profile") == "balanced",
            )
            _check(
                checks,
                "approval_policy_contract",
                client.post(f"/api/sessions/{session_id}/approval-policy", json={"policy": "ask"}),
                200,
                lambda body: isinstance(body, dict) and body.get("approval_policy") == "ask",
            )

            for name, path in (
                ("intelligence_contract", "intelligence"),
                ("context_compiler_contract", "context-compiler"),
                ("memory_governance_contract", "memory-governance"),
                ("trace_contract", "trace"),
                ("replay_contract", "replay"),
                ("report_contract", "report"),
                ("permissions_contract", "permissions"),
                ("checkpoint_contract", "checkpoint"),
                ("algorithm_runs_contract", "algorithm-lab/runs"),
            ):
                _check(
                    checks,
                    name,
                    client.get(f"/api/sessions/{session_id}/{path}"),
                    200,
                    lambda body: isinstance(body, (dict, list)),
                )
            _check(
                checks,
                "missing_session_contract",
                client.get("/api/sessions/not-a-real-session"),
                404,
            )
            _check(
                checks,
                "invalid_mode_contract",
                client.post(f"/api/sessions/{session_id}/mode", json={"mode": "invalid"}),
                422,
            )
            _check(
                checks,
                "invalid_message_contract",
                client.post(f"/api/sessions/{session_id}/messages", json={"content": ""}),
                422,
            )
            _check(
                checks,
                "invalid_approval_payload_contract",
                client.post(f"/api/sessions/{session_id}/approval", json={"approved": "yes"}),
                422,
            )
            _check(
                checks,
                "stale_approval_contract",
                client.post(f"/api/sessions/{session_id}/approval", json={"approved": False}),
                409,
            )

            history_types: list[str] = []
            live_types: list[str] = []
            live_sequences: list[int] = []
            with client.websocket_connect(f"/ws/sessions/{session_id}") as websocket:
                for _ in range(32):
                    event = websocket.receive_json()
                    event_type = str(event.get("type") or "")
                    history_types.append(event_type)
                    if event_type == "history_end":
                        break
                sent = client.post(
                    f"/api/sessions/{session_id}/messages",
                    json={"content": "Say hello"},
                )
                _check(
                    checks,
                    "message_accept_contract",
                    sent,
                    202,
                    lambda body: isinstance(body, dict) and body.get("accepted") is True,
                )
                for _ in range(128):
                    event = websocket.receive_json()
                    event_type = str(event.get("type") or "")
                    live_types.append(event_type)
                    if isinstance(event.get("sequence"), int):
                        live_sequences.append(int(event["sequence"]))
                    if event_type == "turn_finished":
                        break
            checks.append({
                "name": "websocket_history_contract",
                "passed": history_types[:1] == ["history_start"] and history_types[-1:] == ["history_end"],
                "history_event_count": len(history_types),
            })
            checks.append({
                "name": "websocket_live_contract",
                "passed": {"turn_started", "assistant_response", "turn_finished"}.issubset(live_types)
                and live_sequences == sorted(set(live_sequences)),
                "live_event_count": len(live_types),
            })
            deadline = time.monotonic() + timeout
            details_after_turn = client.get(f"/api/sessions/{session_id}").json()
            while details_after_turn.get("running") and time.monotonic() < deadline:
                time.sleep(0.01)
                details_after_turn = client.get(f"/api/sessions/{session_id}").json()
            stored_events = client.get(f"/api/sessions/{session_id}/events").json()
            stored_event_count = len(stored_events) if isinstance(stored_events, list) else 0
            reconnect_types: list[str] = []
            reconnect_sequences: list[int] = []
            reconnect_history_types: list[str] = []
            reconnect_history_sequences: list[int] = []
            with client.websocket_connect(f"/ws/sessions/{session_id}") as websocket:
                for _ in range(128):
                    event = websocket.receive_json()
                    event_type = str(event.get("type") or "")
                    reconnect_types.append(event_type)
                    if isinstance(event.get("sequence"), int):
                        reconnect_sequences.append(int(event["sequence"]))
                    if event_type == "history_chunk":
                        history_payload = event.get("payload")
                        history_events = history_payload.get("events") if isinstance(history_payload, dict) else []
                        for history_event in history_events if isinstance(history_events, list) else []:
                            if not isinstance(history_event, dict):
                                continue
                            reconnect_history_types.append(str(history_event.get("type") or ""))
                            if isinstance(history_event.get("sequence"), int):
                                reconnect_history_sequences.append(int(history_event["sequence"]))
                    if event_type == "history_end":
                        break
            checks.append({
                "name": "websocket_reconnect_history_contract",
                "passed": reconnect_types[:1] == ["history_start"]
                and reconnect_types[-1:] == ["history_end"]
                and "history_chunk" in reconnect_types
                and {"turn_started", "assistant_response", "turn_finished"}.issubset(reconnect_history_types)
                and reconnect_history_sequences == sorted(set(reconnect_history_sequences)),
                "history_event_count": len(reconnect_types),
                "stored_event_count": stored_event_count,
                "history_event_types": reconnect_history_types,
            })
            final_details = _check(
                checks,
                "completed_turn_contract",
                client.get(f"/api/sessions/{session_id}"),
                200,
                lambda body: isinstance(body, dict) and body.get("running") is False and body.get("status") == "completed",
            )
            second = _check(
                checks,
                "session_isolation_contract",
                client.post("/api/sessions", json={"workspace": directory, "mode": "ask"}),
                200,
                lambda body: isinstance(body, dict) and body.get("session_id") not in {None, session_id},
            )
            session_list = _check(
                checks,
                "session_list_contract",
                client.get("/api/workspaces/sessions", params={"workspace": directory}),
                200,
                lambda body: isinstance(body, dict) and len(body.get("sessions", [])) >= 2,
            )
            del health, details, final_details, second, session_list

    failed = [item for item in checks if not item.get("passed")]
    return {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "passed": not failed,
        "checks": checks,
        "failed_checks": [item["name"] for item in failed],
        "redacted": True,
        **collect_metadata(),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# API 契约与 WebSocket 集成烟测",
        "",
        "> 使用临时工作区、FastAPI TestClient 和 ScriptedModel；不调用 DeepSeek，报告不写入响应正文。",
        "",
        f"- Git Commit：`{report.get('git_commit') or 'unknown'}` · 工作区修改：`{'是' if report.get('git_dirty') else '否'}`",
        f"- 工作区快照 SHA-256：`{report.get('git_snapshot_sha256') or 'unknown'}`",
        f"- 结果：**{'Passed' if report.get('passed') else 'Failed'}** · 耗时：`{report.get('duration_ms', 0)}`ms",
        f"- 检查数：`{len(report.get('checks', []))}` · 失败：`{len(report.get('failed_checks', []))}`",
        "",
        "| 检查 | 结果 | HTTP | 附加数据 |",
        "| --- | --- | ---: | --- |",
    ]
    for item in report.get("checks", []):
        extra = item.get("history_event_count", item.get("live_event_count", "—"))
        lines.append(
            f"| `{item['name']}` | {'PASS' if item.get('passed') else 'FAIL'} | "
            f"{item.get('status_code', '—')} | {extra} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        report = run_contract(timeout=args.timeout)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "passed": False,
            "error": type(exc).__name__,
            **collect_metadata(),
        }
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "api-contract.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "api-contract.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"passed": report.get("passed", False), "checks": len(report.get("checks", []))}, ensure_ascii=False))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
