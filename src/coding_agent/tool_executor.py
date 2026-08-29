from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from time import perf_counter
from typing import Any
from collections.abc import Awaitable, Callable

from .tools.base import ToolError, ToolResult
from .tools.registry import ToolRegistry
from .hooks import HookManager
from .redaction import Redactor
from uuid import uuid4


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        hooks: HookManager | None = None,
        result_store: Path | None = None,
        redactor: Redactor | None = None,
        result_store_max_bytes: int | None = 50_000_000,
        result_store_max_files: int | None = 512,
    ) -> None:
        self.registry = registry
        self.hooks = hooks or HookManager()
        self.result_store = result_store
        self.redactor = redactor or Redactor()
        self.result_store_max_bytes = result_store_max_bytes
        self.result_store_max_files = result_store_max_files
        self._result_store_lock = threading.Lock()

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        output_callback: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> ToolResult:
        started = perf_counter()
        try:
            spec = self.registry.get(name)
            spec.validate(arguments)
            decision = await self.hooks.before(name, arguments)
            if not decision.allow:
                result = ToolResult.failure(
                    decision.code,
                    decision.reason or f"Hook {decision.hook or 'pre-tool'} denied {name}",
                    data={"hook": decision.hook, "additional_context": decision.additional_context},
                )
            else:
                handler_arguments = dict(arguments)
                if output_callback is not None and name == "run_command":
                    handler_arguments["_output_callback"] = output_callback
                result = await asyncio.wait_for(spec.handler(handler_arguments), timeout=spec.timeout)
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
        try:
            result = await self.hooks.after(name, arguments, result)
        except Exception as exc:
            result = ToolResult.failure(
                "HOOK_FAILED", f"Post-tool hook failed: {type(exc).__name__}: {exc}"
            )
        self._persist_full_output(result)
        result.metadata.setdefault("duration_ms", round((perf_counter() - started) * 1000))
        return result

    def _persist_full_output(self, result: ToolResult) -> None:
        if self.result_store is None:
            result.metadata.pop("_full_stdout", None)
            result.metadata.pop("_full_stderr", None)
            return
        full = {
            key: result.metadata.pop(key, "")
            for key in ("_full_stdout", "_full_stderr")
        }
        if not any(full.values()):
            return
        self.result_store.mkdir(parents=True, exist_ok=True)
        reference = f"tool-result-{uuid4().hex}.json"
        safe_full = self.redactor.redact(full)
        encoded = json.dumps(safe_full, ensure_ascii=False).encode("utf-8")
        with self._result_store_lock:
            pruned = self._prune_result_store(len(encoded))
            if (
                self.result_store_max_bytes is not None
                and len(encoded) > self.result_store_max_bytes
            ):
                result.data["result_store_error"] = "RESULT_STORE_QUOTA"
                result.data["result_store_limit_bytes"] = self.result_store_max_bytes
                return
            (self.result_store / reference).write_bytes(encoded)
        if pruned:
            result.data["result_store_pruned"] = pruned
        result.data["result_reference"] = str(Path(".code-helper") / "tool-results" / reference)

    def _prune_result_store(self, incoming_bytes: int) -> int:
        if self.result_store is None:
            return 0
        files = sorted(
            (
                path
                for path in self.result_store.glob("tool-result-*.json")
                if path.is_file()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        total = sum(path.stat().st_size for path in files)
        removed = 0
        max_bytes = self.result_store_max_bytes
        max_files = self.result_store_max_files
        while files and (
            (max_bytes is not None and total + incoming_bytes > max_bytes)
            or (max_files is not None and len(files) >= max_files)
        ):
            oldest = files.pop(0)
            try:
                size = oldest.stat().st_size
                oldest.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        return removed
