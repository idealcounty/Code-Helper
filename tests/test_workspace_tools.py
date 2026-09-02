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


def test_file_summary_cache_invalidates_after_change(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("one\ntwo\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    first = workspace.file_summary(path)
    assert workspace.file_summary(path) == first
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    workspace.observe(path)
    second = workspace.file_summary(path)
    assert second["lines"] == 3 and second["sha256"] != first["sha256"]


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


def test_patch_rejects_identical_old_and_new_text_without_touching_file(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    path = tmp_path / "sample.txt"
    original = "typedef long ll;\n"
    path.write_text(original, encoding="utf-8")
    before = path.stat().st_mtime_ns

    result = asyncio.run(
        executor.execute(
            "apply_patch",
            {
                "path": "sample.txt",
                "old_text": "typedef long ll;",
                "new_text": "typedef long ll;",
            },
        )
    )

    assert result.ok is False
    assert result.code == "NO_CHANGES"
    assert "must differ" in result.message
    assert result.metadata.get("mutated_files") is None
    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == before


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


def test_git_metadata_is_reserved(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("private metadata", encoding="utf-8")

    result = asyncio.run(
        executor.execute("read_file", {"path": ".git/config"})
    )

    assert result.ok is False
    assert result.code == "RESERVED_PATH"


def test_list_files_respects_depth_and_filters_sensitive_runtime_entries(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    (tmp_path / "top.txt").write_text("top", encoding="utf-8")
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    (nested / "code.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hidden", encoding="utf-8")
    (tmp_path / ".code-helper").mkdir()
    (tmp_path / ".code-helper" / "state.json").write_text("{}", encoding="utf-8")

    result = asyncio.run(
        executor.execute("list_files", {"path": ".", "max_depth": 1})
    )

    assert result.ok is True
    assert result.data["entries"] == ["src/", "top.txt"]
    assert result.data["truncated"] is False


def test_list_and_read_tools_normalize_ranges_and_directory_errors(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    (tmp_path / "sample.txt").write_text("first\nsecond\n", encoding="utf-8")

    listed = asyncio.run(executor.execute("list_files", {"path": "sample.txt"}))
    read = asyncio.run(
        executor.execute(
            "read_file", {"path": "sample.txt", "start_line": 0, "end_line": 99}
        )
    )

    assert listed.ok is False and listed.code == "NOT_A_DIRECTORY"
    assert read.ok is True
    assert read.data["start_line"] == 1
    assert read.data["end_line"] == 2
    assert read.data["content"] == "first\nsecond\n"


def test_read_file_rejects_binary_and_oversized_content(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor], monkeypatch: object
) -> None:
    _, executor = file_tools
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"\xff\xfe")
    binary_result = asyncio.run(executor.execute("read_file", {"path": "binary.dat"}))

    import coding_agent.tools.filesystem as filesystem

    large = tmp_path / "large.txt"
    large.write_text("x", encoding="utf-8")
    monkeypatch.setattr(filesystem, "MAX_FILE_BYTES", 0)
    large_result = asyncio.run(executor.execute("read_file", {"path": "large.txt"}))

    assert binary_result.ok is False and binary_result.code == "BINARY_FILE"
    assert large_result.ok is False and large_result.code == "FILE_TOO_LARGE"


def test_search_files_and_text_support_globs_case_and_truncation(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    (tmp_path / "one.py").write_text("Needle here\nneedle again\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle third\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text("needle secret", encoding="utf-8")

    files = asyncio.run(executor.execute("search_files", {"pattern": "*.py"}))
    insensitive = asyncio.run(
        executor.execute("search_text", {"query": "NEEDLE", "max_results": 1})
    )
    sensitive = asyncio.run(
        executor.execute(
            "search_text", {"query": "Needle", "case_sensitive": True}
        )
    )

    assert files.ok and files.data["matches"] == ["one.py"]
    assert insensitive.ok and insensitive.data["truncated"] is True
    assert len(insensitive.data["matches"]) == 1
    assert sensitive.ok and len(sensitive.data["matches"]) == 1
    assert all(item["path"] != "credentials.json" for item in sensitive.data["matches"])


def test_apply_patch_and_write_file_success_and_existing_file_guard(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    path = tmp_path / "sample.txt"
    path.write_text("before\n", encoding="utf-8")
    asyncio.run(executor.execute("read_file", {"path": "sample.txt"}))

    patched = asyncio.run(
        executor.execute(
            "apply_patch",
            {"path": "sample.txt", "old_text": "before", "new_text": "after"},
        )
    )
    created = asyncio.run(
        executor.execute("write_file", {"path": "new.txt", "content": "created"})
    )
    duplicate = asyncio.run(
        executor.execute("write_file", {"path": "new.txt", "content": "again"})
    )

    assert patched.ok and patched.data["path"] == "sample.txt"
    assert path.read_text(encoding="utf-8") == "after\n"
    assert created.ok and created.data["path"] == "new.txt"
    assert duplicate.ok is False and duplicate.code == "FILE_EXISTS"


def test_apply_patch_reports_missing_old_text(
    tmp_path: Path, file_tools: tuple[Workspace, ToolExecutor]
) -> None:
    _, executor = file_tools
    (tmp_path / "sample.txt").write_text("value = 1\n", encoding="utf-8")
    asyncio.run(executor.execute("read_file", {"path": "sample.txt"}))

    result = asyncio.run(
        executor.execute(
            "apply_patch",
            {"path": "sample.txt", "old_text": "absent", "new_text": "new"},
        )
    )

    assert result.ok is False and result.code == "EDIT_NOT_FOUND"
