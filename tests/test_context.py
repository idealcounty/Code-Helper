from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coding_agent.context import ContextManager, _natural_language_paths
from coding_agent.session import AgentState
from coding_agent.tools import Workspace


def _assistant(*call_ids: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def _tool(call_id: str, content: str = "ok") -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "read_file",
        "content": content,
    }


def _assert_valid_tool_protocol(messages: list[dict[str, Any]]) -> None:
    index = 0
    while index < len(messages):
        message = messages[index]
        assert message.get("role") != "tool", "orphan tool result"
        calls = message.get("tool_calls") or []
        if not calls:
            index += 1
            continue
        expected = {str(call["id"]) for call in calls}
        actual: list[str] = []
        index += 1
        while index < len(messages) and messages[index].get("role") == "tool":
            actual.append(str(messages[index].get("tool_call_id") or ""))
            index += 1
        assert len(actual) == len(expected)
        assert set(actual) == expected


def test_message_limit_keeps_multi_tool_exchange_atomic() -> None:
    state = AgentState.create(session_id="session")
    state.messages = [
        {"role": "user", "content": "old"},
        _assistant("call-1", "call-2"),
        _tool("call-1"),
        _tool("call-2"),
        {"role": "user", "content": "latest"},
    ]

    context = ContextManager(max_messages=3).build(state, [])

    _assert_valid_tool_protocol(context.messages)
    assert not any(message.get("tool_calls") for message in context.messages)
    assert context.messages[-1] == {"role": "user", "content": "latest"}
    assert context.truncated is True


def test_newest_tool_exchange_can_exceed_soft_message_limit_but_stays_complete() -> None:
    state = AgentState.create(session_id="session")
    exchange = [
        _assistant("call-1", "call-2"),
        _tool("call-1"),
        _tool("call-2"),
    ]
    state.messages = [{"role": "user", "content": "old"}, *exchange]

    context = ContextManager(max_messages=1).build(state, [])

    _assert_valid_tool_protocol(context.messages)
    assert context.messages[-3:] == exchange
    assert context.truncated is True


def test_character_limit_drops_whole_tool_exchange() -> None:
    state = AgentState.create(session_id="session")
    state.messages = [
        {"role": "user", "content": "inspect"},
        _assistant("call-1", "call-2"),
        _tool("call-1", "a" * 200),
        _tool("call-2", "b" * 200),
        {"role": "user", "content": "answer now"},
    ]

    context = ContextManager(max_context_chars=250).build(state, [])

    _assert_valid_tool_protocol(context.messages)
    assert not any(message.get("tool_calls") for message in context.messages)
    assert context.messages[-1]["content"] == "answer now"


def test_incomplete_recovered_tool_exchange_and_orphans_are_removed() -> None:
    state = AgentState.create(session_id="session")
    state.messages = [
        {"role": "user", "content": "inspect"},
        _assistant("call-1", "call-2"),
        _tool("call-1"),
        {"role": "user", "content": "continue after interruption"},
        _tool("orphan"),
    ]

    context = ContextManager().build(state, [])

    _assert_valid_tool_protocol(context.messages)
    assert not any(message.get("tool_calls") for message in context.messages)
    assert not any(message.get("role") == "tool" for message in context.messages)
    assert context.context_summary_meta["protocol_removed_message_count"] == 3


def test_complete_multi_tool_exchange_is_preserved_without_mutation() -> None:
    state = AgentState.create(session_id="session")
    exchange = [
        _assistant("call-1", "call-2"),
        _tool("call-2", json.dumps({"ok": True})),
        _tool("call-1", json.dumps({"ok": True})),
    ]
    state.messages = [{"role": "user", "content": "inspect"}, *exchange]

    context = ContextManager().build(state, [])

    _assert_valid_tool_protocol(context.messages)
    assert context.messages[2:] == exchange
    assert context.truncated is False


def test_natural_language_paths_extract_multiple_targets() -> None:
    query = "Update src/app.py and docs/guide.md, but leave tests/test_app.py unchanged."

    assert _natural_language_paths(query) == [
        "src/app.py",
        "docs/guide.md",
        "tests/test_app.py",
    ]


def test_project_rules_follow_multiple_paths_named_in_user_request(tmp_path: Path) -> None:
    src = tmp_path / "src"
    docs = tmp_path / "docs"
    src.mkdir()
    docs.mkdir()
    (src / "AGENTS.md").write_text("src-only rule", encoding="utf-8")
    (docs / "AGENTS.md").write_text("docs-only rule", encoding="utf-8")

    state = AgentState.create(session_id="session")
    state.messages = [
        {
            "role": "user",
            "content": "Please update src/app.py and docs/guide.md.",
        }
    ]
    context = ContextManager(workspace=Workspace(tmp_path)).build(state, [])

    system = context.messages[0]["content"]
    assert "src-only rule" in system
    assert "docs-only rule" in system
    assert context.rule_candidates == 2
