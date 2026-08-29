from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from coding_agent.context import ContextManager
from coding_agent.repo_map import RepoMapBuilder
from coding_agent.session import AgentState
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import (
    ToolRegistry,
    Workspace,
    register_git_tools,
    register_repo_map_tool,
)


def test_repo_map_ranks_query_matches_and_python_symbols(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "billing.py").write_text(
        "import decimal\n\nclass Invoice:\n    pass\n\ndef total():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")

    workspace = Workspace(tmp_path)
    data = RepoMapBuilder(workspace).build(query="billing invoice", max_files=5)

    first = data["files"][0]
    assert first["path"] == "src/billing.py"
    assert "class Invoice" in first["symbols"]
    assert "def total" in first["symbols"]
    assert "decimal" in first["imports"]


def test_get_repo_map_is_registered_as_read_tool(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    register_repo_map_tool(registry, workspace)
    executor = ToolExecutor(registry)

    result = asyncio.run(
        executor.execute("get_repo_map", {"query": "main", "max_files": 10})
    )

    assert result.ok is True
    assert result.data["files"][0]["path"] == "app.py"


def test_context_injects_project_rules(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "Always inspect tests before editing.", encoding="utf-8"
    )
    workspace = Workspace(tmp_path)
    context = ContextManager(workspace=workspace).build(AgentState.create(), [])

    system = context.messages[0]["content"]
    assert "Project rules:" in system
    assert "Always inspect tests before editing." in system


def test_context_uses_target_path_rule_chain_and_override(tmp_path: Path) -> None:
    src = tmp_path / "src"
    unrelated = tmp_path / "docs"
    src.mkdir()
    unrelated.mkdir()
    (tmp_path / "AGENTS.md").write_text("root rule", encoding="utf-8")
    (src / "AGENTS.md").write_text("src default rule", encoding="utf-8")
    (src / "AGENTS.override.md").write_text("src override rule", encoding="utf-8")
    (unrelated / "AGENTS.md").write_text("docs rule must stay out", encoding="utf-8")
    (src / "app.py").write_text("value = 1\n", encoding="utf-8")

    state = AgentState.create()
    state.changed_files.add("src/app.py")
    context = ContextManager(workspace=Workspace(tmp_path)).build(state, [])
    system = context.messages[0]["content"]

    assert "root rule" in system
    assert "src override rule" in system
    assert "src default rule" not in system
    assert "docs rule must stay out" not in system
    assert [item["path"] for item in context.rule_sources] == [
        "AGENTS.md",
        "src/AGENTS.override.md",
    ]
    assert context.rule_candidates == 3


def test_context_rule_budget_reports_truncation(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * 100, encoding="utf-8")
    state = AgentState.create()
    context = ContextManager(
        workspace=Workspace(tmp_path), max_rule_chars=24
    ).build(state, [])

    assert context.rule_truncated is True
    assert context.rule_chars == 24
    assert context.rule_sources[0]["truncated"] is True


def test_context_targets_recent_tool_path_when_no_file_changed(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "AGENTS.md").write_text("root rule", encoding="utf-8")
    (src / "AGENTS.md").write_text("src rule", encoding="utf-8")
    state = AgentState.create()
    state.recent_actions.append(
        {
            "signature": json.dumps(
                {"name": "read_file", "arguments": {"path": "src/app.py"}}
            )
        }
    )

    context = ContextManager(workspace=Workspace(tmp_path)).build(state, [])

    assert "src rule" in context.messages[0]["content"]


def test_get_diff_tool_returns_workspace_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    sample = tmp_path / "sample.txt"
    sample.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=tmp_path, check=True)
    sample.write_text("new\n", encoding="utf-8")

    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    register_git_tools(registry, workspace)
    executor = ToolExecutor(registry)

    result = asyncio.run(executor.execute("get_diff", {}))

    assert result.ok is True
    assert "-old" in result.data["diff"]
    assert "+new" in result.data["diff"]
