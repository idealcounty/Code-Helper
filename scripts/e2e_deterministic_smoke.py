"""Run a deterministic, user-journey E2E smoke test against the Web API.

This is the browser-independent counterpart of the manual WebView smoke test.
It exercises the same session lifecycle with a ScriptedModel: ask -> switch to
act -> request a file change -> approve -> verify -> archive -> restore ->
rehydrate after an app restart.  It never calls DeepSeek or touches a user
workspace and writes only into a temporary directory plus the report output.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.evidence_metadata import collect_metadata
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from evidence_metadata import collect_metadata

from coding_agent.config import AppConfig
from coding_agent.model import ModelResponse, ToolCall
from coding_agent.web.app import create_app


VERIFY_COMMAND = "python -c \"from pathlib import Path; assert Path('generated.md').read_text(encoding='utf-8') == '# E2E generated\\n'\""


class WorkflowModel:
    """Return one response for each phase of the deterministic journey."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        del messages, tools, reasoning_effort
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(content="ask phase completed")
        if self.calls == 2:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        "e2e-write-1",
                        "write_file",
                        {"path": "generated.md", "content": "# E2E generated\n"},
                    )
                ]
            )
        if self.calls == 3:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        "e2e-verify-1",
                        "run_command",
                        {
                            "command": VERIFY_COMMAND,
                            "purpose": "verify",
                        },
                    )
                ]
            )
        return ModelResponse(content="act phase completed and verified")


def _wait_for_terminal(client: Any, session_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    details = client.get(f"/api/sessions/{session_id}").json()
    while details.get("running") and time.monotonic() < deadline:
        time.sleep(0.01)
        details = client.get(f"/api/sessions/{session_id}").json()
    if details.get("running"):
        raise RuntimeError(f"session did not finish within {timeout}s")
    return details


def _finish_with_pending_approvals(
    client: Any,
    session_id: str,
    timeout: float,
    approved_ids: set[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Wait for completion while approving each explicit operation in order."""

    deadline = time.monotonic() + timeout
    approvals = 0
    seen_approvals = approved_ids if approved_ids is not None else set()
    details = client.get(f"/api/sessions/{session_id}").json()
    while details.get("running") and time.monotonic() < deadline:
        pending = details.get("pending_approval") or {}
        call = pending.get("call") or {}
        call_id = str(call.get("id") or "").strip()
        if call_id and call_id not in seen_approvals:
            response = client.post(
                f"/api/sessions/{session_id}/approval",
                json={"tool_call_id": call_id, "approved": True, "scope": "once"},
            )
            _require(response, 200, f"approve pending operation {call_id}")
            approvals += 1
            seen_approvals.add(call_id)
        else:
            time.sleep(0.01)
        details = client.get(f"/api/sessions/{session_id}").json()
    if details.get("running"):
        raise RuntimeError(f"session did not finish within {timeout}s")
    return details, approvals


def _require(response: Any, expected: int, label: str) -> dict[str, Any]:
    if response.status_code != expected:
        raise RuntimeError(f"{label}: expected HTTP {expected}, got {response.status_code}: {response.text[:300]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label}: response is not an object")
    return payload


def run_e2e(*, timeout: float = 5.0) -> dict[str, Any]:
    """Execute the complete deterministic journey and return evidence."""

    from fastapi.testclient import TestClient

    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="code-helper-e2e-") as directory:
        workspace = Path(directory)
        model = WorkflowModel()
        config = AppConfig(api_key="scripted-test-key", base_url="https://example.invalid/v1")
        verification_path = workspace / ".code-helper" / "verification.json"
        verification_path.parent.mkdir(parents=True, exist_ok=True)
        verification_path.write_text(
            json.dumps({"commands": [VERIFY_COMMAND]}, ensure_ascii=False),
            encoding="utf-8",
        )
        app = create_app(config, model_client_factory=lambda: model)
        with TestClient(app) as client:
            created = _require(
                client.post("/api/sessions", json={"workspace": directory, "mode": "ask"}),
                200,
                "create session",
            )
            session_id = str(created["session_id"])
            checks.append({"name": "create_workspace_session", "passed": True})

            _require(
                client.post(f"/api/sessions/{session_id}/messages", json={"content": "Explain the task"}),
                202,
                "ask message",
            )
            ask_details = _wait_for_terminal(client, session_id, timeout)
            if ask_details.get("status") != "completed":
                raise RuntimeError(f"ask phase ended as {ask_details.get('status')}")
            checks.append({"name": "ask_turn_completed", "passed": True})

            changed_mode = _require(
                client.post(f"/api/sessions/{session_id}/mode", json={"mode": "act"}),
                200,
                "switch mode",
            )
            if changed_mode.get("mode") != "act":
                raise RuntimeError("mode switch did not take effect")
            checks.append({"name": "switch_ask_to_act", "passed": True})

            _require(
                client.post(f"/api/sessions/{session_id}/messages", json={"content": "Create generated.md"}),
                202,
                "act message",
            )
            deadline = time.monotonic() + timeout
            details = client.get(f"/api/sessions/{session_id}").json()
            while details.get("pending_approval") is None and time.monotonic() < deadline:
                time.sleep(0.01)
                details = client.get(f"/api/sessions/{session_id}").json()
            pending = details.get("pending_approval") or {}
            pending_call = pending.get("call") or {}
            if pending_call.get("id") != "e2e-write-1":
                raise RuntimeError(f"expected pending approval, got {pending_call}")
            checks.append({"name": "approval_requested", "passed": True})

            _require(
                client.post(
                    f"/api/sessions/{session_id}/approval",
                    json={"tool_call_id": "e2e-write-1", "approved": True, "scope": "once"},
                ),
                200,
                "approve write",
            )
            act_details, follow_up_approvals = _finish_with_pending_approvals(
                client, session_id, timeout, {"e2e-write-1"}
            )
            generated = workspace / "generated.md"
            if act_details.get("status") != "completed" or not generated.is_file():
                raise RuntimeError(
                    "act phase failed: "
                    f"status={act_details.get('status')} "
                    f"step={act_details.get('step')} "
                    f"changed_files={act_details.get('changed_files')} "
                    f"pending={act_details.get('pending_approval')} "
                    f"verification={act_details.get('verification_evidence')} "
                    f"last_error={act_details.get('last_error')}"
                )
            checks.append({"name": "approved_write_and_completion", "passed": True, "follow_up_approvals": follow_up_approvals})

            event_types = [item.get("type") for item in client.get(f"/api/sessions/{session_id}/events").json()]
            required_events = {"mode_changed", "approval_requested", "tool_result", "turn_finished"}
            missing_events = sorted(required_events - set(event_types))
            if missing_events:
                raise RuntimeError(f"missing lifecycle events: {missing_events}")
            checks.append({"name": "lifecycle_events_recorded", "passed": True, "event_count": len(event_types)})

            _require(
                client.post(
                    f"/api/workspaces/sessions/{session_id}/archive",
                    json={"workspace": directory},
                ),
                200,
                "archive session",
            )
            archived = client.get(f"/api/workspaces/sessions?workspace={directory}").json()
            if not any(item.get("session_id") == session_id for item in archived.get("archived_sessions", [])):
                raise RuntimeError("archived session not listed")
            _require(
                client.post(
                    f"/api/workspaces/sessions/{session_id}/restore",
                    json={"workspace": directory},
                ),
                200,
                "restore session",
            )
            checks.append({"name": "archive_and_restore", "passed": True})

        # A new app instance must be able to discover and rehydrate the same
        # durable event stream after the original app has been closed.
        restarted = create_app(config, model_client_factory=lambda: WorkflowModel())
        with TestClient(restarted) as client:
            listed = client.get(f"/api/workspaces/sessions?workspace={directory}").json()
            if not any(item.get("session_id") == session_id for item in listed.get("sessions", [])):
                raise RuntimeError("restored session was not found after restart")
            reopened = _require(
                client.post(
                    "/api/sessions",
                    json={"workspace": directory, "session_id": session_id, "mode": "act"},
                ),
                200,
                "rehydrate session",
            )
            if reopened.get("session_id") != session_id:
                raise RuntimeError("rehydrated session id changed")
            checks.append({"name": "restart_rehydrates_history", "passed": True})

    return {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "passed": True,
        "checks": checks,
        "model_calls": model.calls,
        **collect_metadata(),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 确定性用户旅程 E2E 烟测",
        "",
        "> 使用 FastAPI TestClient + ScriptedModel，不调用 DeepSeek，不写入用户工作区。",
        "",
        f"- Git Commit：`{report.get('git_commit') or 'unknown'}` · 工作区修改：`{'是' if report.get('git_dirty') else '否'}`",
        f"- 工作区快照 SHA-256：`{report.get('git_snapshot_sha256') or 'unknown'}`",
        f"- 结果：**{'Passed' if report.get('passed') else 'Failed'}** · 耗时：`{report.get('duration_ms', 0)}`ms · 模型调用：`{report.get('model_calls', 0)}`",
        "",
        "| 检查项 | 结果 | 附加数据 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| `{item['name']}` | {'PASS' if item['passed'] else 'FAIL'} | {item.get('event_count', '—')} |"
        for item in report.get("checks", [])
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
        report = run_e2e(timeout=args.timeout)
    except Exception as exc:
        report = {"schema_version": 1, "passed": False, "error": f"{type(exc).__name__}: {exc}", **collect_metadata()}
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "e2e.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "e2e.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"passed": report.get("passed", False), "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
