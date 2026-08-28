from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .tools.base import ToolResult

PreToolHook = Callable[[str, dict[str, Any]], Awaitable[None] | None]
PostToolHook = Callable[[str, dict[str, Any], ToolResult], Awaitable[ToolResult | None] | ToolResult | None]


class HookManager:
    """Small explicit hook pipeline; hooks never decide agent policy."""

    def __init__(self, *, pre: list[PreToolHook] | None = None, post: list[PostToolHook] | None = None) -> None:
        self.pre = pre or []
        self.post = post or []

    async def before(self, name: str, arguments: dict[str, Any]) -> None:
        for hook in self.pre:
            result = hook(name, arguments)
            if hasattr(result, "__await__"):
                await result

    async def after(self, name: str, arguments: dict[str, Any], result: ToolResult) -> ToolResult:
        current = result
        for hook in self.post:
            replacement = hook(name, arguments, current)
            if hasattr(replacement, "__await__"):
                replacement = await replacement
            if replacement is not None:
                current = replacement
        return current
