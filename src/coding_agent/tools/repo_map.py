from __future__ import annotations

from typing import Any

from ..repo_map import RepoMapBuilder
from .base import ToolResult, ToolRisk, ToolSpec
from .registry import ToolRegistry
from .workspace import Workspace


def register_repo_map_tool(registry: ToolRegistry, workspace: Workspace) -> None:
    builder = RepoMapBuilder(workspace)

    async def get_repo_map(arguments: dict[str, Any]) -> ToolResult:
        max_files = min(int(arguments.get("max_files", 60)), 120)
        query = arguments.get("query", "")
        data = builder.build(query=query, max_files=max_files)
        return ToolResult.success(
            f"Mapped {len(data['files'])} relevant files",
            data=data,
        )

    registry.register(
        ToolSpec(
            "get_repo_map",
            "Summarize the workspace structure and key Python symbols.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": ""},
                    "max_files": {"type": "integer", "default": 60},
                },
                "required": [],
                "additionalProperties": False,
            },
            ToolRisk.READ,
            get_repo_map,
        )
    )
