from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from .base import ToolError, ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry
from .workspace import Workspace


MAX_FILE_BYTES = 1_000_000


def register_filesystem_tools(registry: ToolRegistry, workspace: Workspace) -> None:
    async def list_files(arguments: dict[str, Any]) -> ToolResult:
        base = workspace.resolve(arguments.get("path", "."), must_exist=True)
        if not base.is_dir():
            raise ToolError("NOT_A_DIRECTORY", f"Not a directory: {arguments.get('path')}")
        max_depth = arguments.get("max_depth", 2)
        entries: list[str] = []
        for path in sorted(base.rglob("*")):
            if workspace.is_ignored(path) or workspace.is_sensitive(path):
                continue
            depth = len(path.relative_to(base).parts)
            if depth > max_depth:
                continue
            label = workspace.relative(path) + ("/" if path.is_dir() else "")
            entries.append(label)
            if len(entries) >= 500:
                break
        return ToolResult.success(
            f"Listed {len(entries)} entries",
            data={"entries": entries, "truncated": len(entries) >= 500},
        )

    async def read_file(arguments: dict[str, Any]) -> ToolResult:
        path = workspace.resolve(arguments["path"], must_exist=True)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ToolError("FILE_TOO_LARGE", f"File exceeds {MAX_FILE_BYTES} bytes")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("BINARY_FILE", "File is not valid UTF-8 text") from exc
        observation = workspace.observe(path)
        lines = text.splitlines()
        start = max(arguments.get("start_line", 1), 1)
        end = min(arguments.get("end_line", start + 399), len(lines))
        selected = "\n".join(lines[start - 1 : end])
        if text.endswith("\n") and end == len(lines):
            selected += "\n"
        return ToolResult.success(
            f"Read lines {start}-{end} from {workspace.relative(path)}",
            data={
                "path": workspace.relative(path),
                "start_line": start,
                "end_line": end,
                "total_lines": len(lines),
                "content": selected,
                "sha256": observation.sha256,
                "summary": workspace.file_summary(path, observation),
            },
        )

    async def search_files(arguments: dict[str, Any]) -> ToolResult:
        pattern = arguments["pattern"]
        matches: list[str] = []
        for path in workspace.root.rglob("*"):
            if workspace.is_ignored(path) or workspace.is_sensitive(path) or not path.is_file():
                continue
            relative = workspace.relative(path)
            if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern):
                matches.append(relative)
            if len(matches) >= arguments.get("max_results", 100):
                break
        return ToolResult.success(
            f"Found {len(matches)} matching files", data={"matches": matches}
        )

    async def search_text(arguments: dict[str, Any]) -> ToolResult:
        query = arguments["query"]
        case_sensitive = arguments.get("case_sensitive", False)
        needle = query if case_sensitive else query.lower()
        max_results = arguments.get("max_results", 100)
        matches: list[dict[str, Any]] = []
        for path in workspace.root.rglob("*"):
            if workspace.is_ignored(path) or workspace.is_sensitive(path) or not path.is_file():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, start=1):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    matches.append(
                        {
                            "path": workspace.relative(path),
                            "line": number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= max_results:
                        return ToolResult.success(
                            f"Found at least {len(matches)} matches",
                            data={"matches": matches, "truncated": True},
                        )
        return ToolResult.success(
            f"Found {len(matches)} matches",
            data={"matches": matches, "truncated": False},
        )

    async def apply_patch(arguments: dict[str, Any]) -> ToolResult:
        path = workspace.resolve(arguments["path"], must_exist=True)
        workspace.require_fresh_observation(path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("BINARY_FILE", "File is not valid UTF-8 text") from exc
        old_text = arguments["old_text"]
        count = text.count(old_text)
        if count == 0:
            raise ToolError("EDIT_NOT_FOUND", "old_text was not found in the file")
        if count != 1:
            raise ToolError(
                "EDIT_NOT_UNIQUE", f"old_text matched {count} locations; provide more context"
            )
        updated = text.replace(old_text, arguments["new_text"], 1)
        path.write_text(updated, encoding="utf-8", newline="")
        observation = workspace.observe(path)
        relative = workspace.relative(path)
        return ToolResult.success(
            f"Updated {relative}",
            data={"path": relative, "sha256": observation.sha256},
            metadata={"mutated_files": [relative]},
        )

    async def write_file(arguments: dict[str, Any]) -> ToolResult:
        path = workspace.resolve(arguments["path"], must_exist=False)
        if path.exists():
            raise ToolError("FILE_EXISTS", "Refusing to overwrite an existing file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arguments["content"], encoding="utf-8", newline="")
        observation = workspace.observe(path)
        relative = workspace.relative(path)
        return ToolResult.success(
            f"Created {relative}",
            data={"path": relative, "sha256": observation.sha256},
            metadata={"mutated_files": [relative]},
        )

    registry.register(
        ToolSpec(
            "list_files",
            "List files and directories inside the workspace.",
            _schema(
                {
                    "path": {"type": "string", "default": "."},
                    "max_depth": {"type": "integer", "default": 2},
                }
            ),
            ToolRisk.READ,
            list_files,
        )
    )
    registry.register(
        ToolSpec(
            "read_file",
            "Read a UTF-8 file by line range. A file must be read before editing.",
            _schema(
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "default": 1},
                    "end_line": {"type": "integer", "default": 400},
                },
                required=["path"],
            ),
            ToolRisk.READ,
            read_file,
        )
    )
    registry.register(
        ToolSpec(
            "search_files",
            "Find files by glob pattern within the workspace.",
            _schema(
                {
                    "pattern": {"type": "string"},
                    "max_results": {"type": "integer", "default": 100},
                },
                required=["pattern"],
            ),
            ToolRisk.READ,
            search_files,
        )
    )
    registry.register(
        ToolSpec(
            "search_text",
            "Search UTF-8 text files in the workspace.",
            _schema(
                {
                    "query": {"type": "string"},
                    "case_sensitive": {"type": "boolean", "default": False},
                    "max_results": {"type": "integer", "default": 100},
                },
                required=["query"],
            ),
            ToolRisk.READ,
            search_text,
        )
    )
    registry.register(
        ToolSpec(
            "apply_patch",
            "Replace one unique text block in a file that was previously read.",
            _schema(
                {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                required=["path", "old_text", "new_text"],
            ),
            ToolRisk.WRITE,
            apply_patch,
        )
    )
    registry.register(
        ToolSpec(
            "write_file",
            "Create a new UTF-8 file. Existing files are never overwritten.",
            _schema(
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                required=["path", "content"],
            ),
            ToolRisk.WRITE,
            write_file,
        )
    )


def _schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
