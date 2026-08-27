from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry, Workspace, register_filesystem_tools


@pytest.fixture
def file_tools(tmp_path: Path) -> tuple[Workspace, ToolExecutor]:
    workspace = Workspace(tmp_path)
    registry = ToolRegistry()
    register_filesystem_tools(registry, workspace)
    return workspace, ToolExecutor(registry)


def test_file_must_be_read_before_edit(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")

    result = asyncio.run(
        executor.execute(
            "apply_patch",
            {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
        )
    )

    assert result.ok is False
    assert result.code == "FILE_NOT_READ"


def test_external_change_invalidates_observation(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    path = tmp_path / "sample.py"
    path.write_text("value = 1\n", encoding="utf-8")

    read_result = asyncio.run(executor.execute("read_file", {"path": "sample.py"}))
    assert read_result.ok is True

    path.write_text("value = 9\n", encoding="utf-8")
    edit_result = asyncio.run(
        executor.execute(
            "apply_patch",
            {"path": "sample.py", "old_text": "value = 1", "new_text": "value = 2"},
        )
    )

    assert edit_result.ok is False
    assert edit_result.code == "FILE_CHANGED"
    assert path.read_text(encoding="utf-8") == "value = 9\n"


def test_path_cannot_escape_workspace(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    outside = tmp_path.parent / "outside-code-helper-test.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        result = asyncio.run(
            executor.execute(
                "read_file", {"path": "../outside-code-helper-test.txt"}
            )
        )
    finally:
        outside.unlink(missing_ok=True)

    assert result.ok is False
    assert result.code == "PATH_OUTSIDE_WORKSPACE"


def test_patch_requires_unique_match(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    path = tmp_path / "sample.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    asyncio.run(executor.execute("read_file", {"path": "sample.txt"}))

    result = asyncio.run(
        executor.execute(
            "apply_patch",
            {"path": "sample.txt", "old_text": "same", "new_text": "changed"},
        )
    )

    assert result.ok is False
    assert result.code == "EDIT_NOT_UNIQUE"


def test_runtime_directory_is_reserved(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    result = asyncio.run(
        executor.execute(
            "write_file",
            {"path": ".code-helper/injected.txt", "content": "blocked"},
        )
    )

    assert result.ok is False
    assert result.code == "RESERVED_PATH"
