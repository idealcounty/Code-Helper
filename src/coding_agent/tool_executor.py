from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any

from .tools.base import ToolError, ToolResult
from .tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        started = perf_counter()
        try:
            spec = self.registry.get(name)
            spec.validate(arguments)
            result = await asyncio.wait_for(spec.handler(arguments), timeout=spec.timeout)
        except ToolError as exc:
            result = ToolResult.failure(exc.code, exc.message, data=exc.data)
        except TimeoutError:
            result = ToolResult.failure(
                "TOOL_TIMEOUT", f"Tool {name!r} exceeded its execution timeout"
            )
        except Exception as exc:  # Tool boundary: normalize unexpected local errors.
            result = ToolResult.failure(
                "TOOL_INTERNAL_ERROR",
                f"{type(exc).__name__}: {exc}",
            )
        result.metadata.setdefault("duration_ms", round((perf_counter() - started) * 1000))
        return result
