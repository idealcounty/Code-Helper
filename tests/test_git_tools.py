from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry
from coding_agent.tools.git_tools import _truncate, register_git_tools
from coding_agent.tools.workspace import Workspace


@pytest.mark.skipif(shutil.which("git") is None, reason="git executable is required")
def test_get_diff_reports_failure_empty_and_staged_changes(tmp_path) -> None:
    non_repo = tmp_path / "non-repo"
    non_repo.mkdir()
    registry = ToolRegistry()
    register_git_tools(registry, Workspace(non_repo))
    executor = ToolExecutor(registry)
    failed = asyncio.run(executor.execute("get_diff", {}))
    assert failed.ok is False
    assert failed.code == "COMMAND_FAILED"

    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    register_git_tools(registry := ToolRegistry(), Workspace(repository))
    executor = ToolExecutor(registry)
    empty = asyncio.run(executor.execute("get_diff", {}))
    assert empty.ok and empty.message == "Diff is empty"

    source = repository / "example.txt"
    source.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "example.txt"], cwd=repository, check=True, capture_output=True)
    staged = asyncio.run(executor.execute("get_diff", {"staged": True}))
    assert staged.ok and staged.message == "Diff collected"
    assert staged.data["staged"] is True
    assert "example.txt" in staged.data["diff"]

    source.write_text("two\n", encoding="utf-8")
    unstaged = asyncio.run(executor.execute("get_diff", {}))
    assert unstaged.ok and "example.txt" in unstaged.data["diff"]


def test_git_diff_truncation_is_bounded() -> None:
    assert _truncate("short", 10) == "short"
    assert _truncate("0123456789", 4) == "0123"
