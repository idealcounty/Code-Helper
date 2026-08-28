from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from .tools.base import ToolError, ToolResult
from .tools.registry import ToolRegistry
from .hooks import HookManager
from uuid import uuid4


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, hooks: HookManager | None = None, result_store: Path | None = None) -> None:
        self.registry = registry
        self.hooks = hooks or HookManager()
        self.result_store = result_store

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        started = perf_counter()
        try:
            spec = self.registry.get(name)
            spec.validate(arguments)
            await self.hooks.before(name, arguments)
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
        result = await self.hooks.after(name, arguments, result)
        self._persist_full_output(result)
        result.metadata.setdefault("duration_ms", round((perf_counter() - started) * 1000))
        return result

    def _persist_full_output(self, result: ToolResult) -> None:
        if self.result_store is None:
            result.metadata.pop("_full_stdout", None)
            result.metadata.pop("_full_stderr", None)
            return
        full = {key: result.metadata.pop(key, "") for key in ("_full_stdout", "_full_stderr")}
        if not any(full.values()):
            return
        self.result_store.mkdir(parents=True, exist_ok=True)
        reference = f"tool-result-{uuid4().hex}.json"
        (self.result_store / reference).write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
        result.data["result_reference"] = str(Path(".code-helper") / "tool-results" / reference)
