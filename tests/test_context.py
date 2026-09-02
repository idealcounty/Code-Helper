from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coding_agent.context import ContextManager, _natural_language_paths
from coding_agent.session import AgentState
from coding_agent.tools import Workspace
from coding_agent.verification_config import VerificationConfig, VerificationRule


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


def test_natural_language_paths_support_quoted_spaces() -> None:
    query = 'Refactor "src/legacy code/app.py" and `docs/user guide.md`.'

    assert _natural_language_paths(query) == [
        "src/legacy code/app.py",
        "docs/user guide.md",
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


def test_project_rules_report_conflicting_same_heading_sections(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "AGENTS.md").write_text(
        "# Project rules\n\n## Verification\nRun pytest before finishing.\n",
        encoding="utf-8",
    )
    (src / "AGENTS.md").write_text(
        "# Source rules\n\n## Verification\nRun npm test before finishing.\n",
        encoding="utf-8",
    )

    state = AgentState.create(session_id="session")
    state.messages = [{"role": "user", "content": "Update src/app.py."}]
    context = ContextManager(workspace=Workspace(tmp_path)).build(state, [])

    assert len(context.rule_conflicts) == 1
    conflict = context.rule_conflicts[0]
    assert conflict["heading"] == "Verification"
    assert conflict["source"] == "AGENTS.md"
    assert conflict["other_source"] == "src/AGENTS.md"
    assert conflict["target"] == "src"
    assert context.rule_sources[0]["conflicts"]


def test_context_only_injects_verification_commands_for_observed_scope() -> None:
    config = VerificationConfig(
        commands=("python -m pytest -q",),
        rules=(
            VerificationRule(
                commands=("python scripts/check_api.py",),
                task_profiles=("project",),
                paths=("src/api/**",),
            ),
            VerificationRule(
                commands=("python scripts/check_ui.py",),
                task_profiles=("project",),
                paths=("src/web/**",),
            ),
        ),
    )
    state = AgentState.create(session_id="session", task_profile="project")
    state.recent_actions.append(
        {
            "result_code": "OK",
            "signature": json.dumps(
                {
                    "name": "read_file",
                    "arguments": {"path": "src/api/users.py"},
                }
            )
        }
    )

    context = ContextManager(verification_config=config).build(state, [])

    system = context.messages[0]["content"]
    assert "python -m pytest -q" in system
    assert "python scripts/check_api.py" in system
    assert "python scripts/check_ui.py" not in system


def test_context_injects_bounded_workflow_state_and_manifest() -> None:
    state = AgentState.create()
    state.messages = [{"role": "user", "content": "implement the feature"}]
    state.workflow_name = "add-feature"
    state.workflow_stage = "implement"
    state.loaded_skills.update({"development-workflow", "add-feature"})

    context = ContextManager(max_context_chars=20_000).build(state, [])
    system = context.messages[0]["content"]

    assert "Current development workflow:" in system
    assert "name: add-feature" in system
    assert "stage: implement" in system
    assert any(item["kind"] == "workflow_state" for item in context.source_manifest)


def test_context_does_not_inject_workflow_block_when_idle() -> None:
    state = AgentState.create()
    state.messages = [{"role": "user", "content": "explain this"}]

    context = ContextManager().build(state, [])

    assert "Current development workflow:" not in context.messages[0]["content"]
    assert not any(item["kind"] == "workflow_state" for item in context.source_manifest)
