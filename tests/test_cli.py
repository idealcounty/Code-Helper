from __future__ import annotations

import asyncio
from types import SimpleNamespace

from coding_agent import cli
from coding_agent.config import AppConfig
from coding_agent.events import AgentEvent
from coding_agent.model import ToolCall
from coding_agent.permissions import PermissionResult


def test_build_parser_accepts_workspace_and_mode(tmp_path) -> None:
    args = cli.build_parser().parse_args(["--workspace", str(tmp_path), "--mode", "plan"])
    assert args.workspace == tmp_path
    assert args.mode == "plan"


def test_print_event_covers_user_visible_event_families(capsys) -> None:
    def event(event_type: str, payload: dict) -> AgentEvent:
        return AgentEvent(
            type=event_type,
            session_id="session",
            turn_id="turn",
            payload=payload,
        )

    events = [
        event("assistant_response", {"content": "answer"}),
        event("assistant_response", {"content": ""}),
        event("tool_started", {"name": "read_file"}),
        event("tool_result", {"name": "read_file", "result": {"ok": True, "message": "ok"}}),
        event("tool_result", {"name": "run_command", "result": {"ok": False, "message": "failed"}}),
        event("verification_required", {"reason": "run tests"}),
        event("unknown", {}),
    ]
    for event in events:
        cli._print_event(event)
    output = capsys.readouterr().out
    assert "Agent> answer" in output
    assert "→ read_file" in output
    assert "✓ read_file: ok" in output
    assert "✗ run_command: failed" in output
    assert "! run tests" in output


def test_ask_for_approval_accepts_only_yes(monkeypatch) -> None:
    answers = iter(["y", "no"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))
    call = ToolCall("call-1", "write_file", {"path": "a.txt"})
    permission = PermissionResult(False, "workspace write")
    assert asyncio.run(cli._ask_for_approval(call, permission)) is True
    assert asyncio.run(cli._ask_for_approval(call, permission)) is False


def test_run_rejects_missing_api_key(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(cli.AppConfig, "from_env", lambda: AppConfig(api_key=""))
    result = asyncio.run(cli._run(SimpleNamespace(workspace=tmp_path, mode="act")))
    assert result == 2
    assert "Missing API key" in capsys.readouterr().out


def test_run_handles_commands_and_one_turn(monkeypatch, capsys, tmp_path) -> None:
    config = AppConfig(api_key="test-key", provider="deepseek", model="test-model")
    monkeypatch.setattr(cli.AppConfig, "from_env", lambda: config)
    state = SimpleNamespace(mode="act")

    class FakeRunner:
        async def run_turn(self, current_state, user_input):
            assert current_state is state
            assert user_input == "hello"
            return SimpleNamespace(status="completed", message="done")

    runtime = SimpleNamespace(
        workspace=SimpleNamespace(root=tmp_path),
        state=state,
        runner=FakeRunner(),
    )
    monkeypatch.setattr(cli, "create_runtime", lambda **_kwargs: runtime)
    inputs = iter(["", "/mode invalid", "/mode plan", "hello", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(inputs))
    result = asyncio.run(cli._run(SimpleNamespace(workspace=tmp_path, mode="act")))
    output = capsys.readouterr().out
    assert result == 0
    assert state.mode == "plan"
    assert "Mode must be ask, plan, or act" in output
    assert "Mode changed to plan" in output
    assert "[completed] done" in output
