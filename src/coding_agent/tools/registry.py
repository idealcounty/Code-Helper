from __future__ import annotations

from .base import ToolError, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError("UNKNOWN_TOOL", f"Unknown tool: {name}") from exc

    def schemas(self, names: set[str] | None = None) -> list[dict]:
        specs = self._tools.values()
        if names is not None:
            specs = (spec for spec in specs if spec.name in names)
        return [spec.as_model_schema() for spec in specs]

    def names(self) -> set[str]:
        return set(self._tools)
