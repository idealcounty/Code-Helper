from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tools.base import ToolResult

PreToolHook = Callable[[str, dict[str, Any]], Awaitable[None] | None]
PostToolHook = Callable[[str, dict[str, Any], ToolResult], Awaitable[ToolResult | None] | ToolResult | None]
LifecycleHook = Callable[[dict[str, Any]], Awaitable[Any] | Any]


@dataclass(frozen=True, slots=True)
class ExternalHookSpec:
    """A deliberately small, no-shell hook command definition."""

    event: str
    argv: tuple[str, ...]
    workspace_root: Path
    matcher: str | None = None
    timeout: float = 5.0
    cwd: Path | None = None
    name: str = "external-hook"

    def matches(self, tool_name: str | None = None) -> bool:
        return not self.matcher or self.matcher == tool_name

    async def run(self, payload: dict[str, Any]) -> "HookDecision":
        try:
            process = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(self.cwd or self.workspace_root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_safe_hook_environment(),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                timeout=self.timeout,
            )
        except TimeoutError:
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            return HookDecision(False, "External hook timed out", "", "HOOK_TIMEOUT", self.name)
        except Exception as exc:
            return HookDecision(False, f"External hook failed: {type(exc).__name__}: {exc}", "", "HOOK_FAILED", self.name)

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = stderr_text[:1_000] or f"exit code {process.returncode}"
            return HookDecision(False, f"External hook rejected: {detail}", "", "HOOK_DENIED", self.name)
        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return HookDecision(hook=self.name)
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            return HookDecision(False, "External hook returned invalid JSON", "", "HOOK_FAILED", self.name)
        return HookDecision.from_result(value, hook=self.name)


def _safe_hook_environment() -> dict[str, str]:
    blocked = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in blocked)
    }


@dataclass(frozen=True, slots=True)
class HookDecision:
    """A hook result that can add context or deny a tool without changing policy."""

    allow: bool = True
    reason: str = ""
    additional_context: str = ""
    code: str = "HOOK_DENIED"
    hook: str = ""

    @classmethod
    def from_result(cls, value: Any, *, hook: str) -> "HookDecision":
        if value is None:
            return cls(hook=hook)
        if isinstance(value, cls):
            return cls(
                allow=value.allow,
                reason=value.reason,
                additional_context=value.additional_context,
                code=value.code,
                hook=value.hook or hook,
            )
        if isinstance(value, bool):
            return cls(allow=value, reason="hook denied" if not value else "", hook=hook)
        if isinstance(value, dict):
            return cls(
                allow=bool(value.get("allow", True)),
                reason=str(value.get("reason") or ""),
                additional_context=str(value.get("additional_context") or ""),
                code=str(value.get("code") or "HOOK_DENIED"),
                hook=hook,
            )
        return cls(hook=hook)


class HookManager:
    """Small explicit hook pipeline; hooks never decide agent policy."""

    def __init__(
        self,
        *,
        pre: list[PreToolHook] | None = None,
        post: list[PostToolHook] | None = None,
        verification: list[LifecycleHook] | None = None,
        task_end: list[LifecycleHook] | None = None,
        external: list[ExternalHookSpec] | None = None,
    ) -> None:
        self.pre = pre or []
        self.post = post or []
        self.verification = verification or []
        self.task_end = task_end or []
        self.external = external or []

    async def before(self, name: str, arguments: dict[str, Any]) -> HookDecision:
        for hook in self.pre:
            hook_name = _hook_name(hook)
            try:
                result = hook(name, arguments)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception as exc:
                return HookDecision(
                    allow=False,
                    reason=f"{type(exc).__name__}: {exc}",
                    code="HOOK_FAILED",
                    hook=hook_name,
                )
            decision = HookDecision.from_result(result, hook=hook_name)
            if not decision.allow:
                return decision
        for hook in self.external:
            if hook.event != "pre_tool" or not hook.matches(name):
                continue
            decision = await hook.run(
                {"event": "PreToolUse", "tool": name, "arguments": arguments}
            )
            if not decision.allow:
                return decision
        return HookDecision()

    async def after(self, name: str, arguments: dict[str, Any], result: ToolResult) -> ToolResult:
        current = result
        for hook in self.post:
            replacement = hook(name, arguments, current)
            if hasattr(replacement, "__await__"):
                replacement = await replacement
            if replacement is not None:
                current = replacement
        for hook in self.external:
            if hook.event != "post_tool" or not hook.matches(name):
                continue
            decision = await hook.run(
                {
                    "event": "PostToolUse",
                    "tool": name,
                    "arguments": arguments,
                    "result": current.to_dict(),
                }
            )
            if not decision.allow:
                return ToolResult.failure(
                    decision.code,
                    decision.reason or f"External hook {hook.name} denied {name}",
                    data={"hook": hook.name, "additional_context": decision.additional_context},
                )
        return current

    async def on_verification(self, evidence: dict[str, Any]) -> list[HookDecision]:
        return await self._run_lifecycle(self.verification, evidence)

    async def on_task_end(self, summary: dict[str, Any]) -> list[HookDecision]:
        return await self._run_lifecycle(self.task_end, summary)

    async def _run_lifecycle(
        self, hooks: list[LifecycleHook], payload: dict[str, Any]
    ) -> list[HookDecision]:
        decisions: list[HookDecision] = []
        for hook in hooks:
            hook_name = _hook_name(hook)
            try:
                result = hook(payload)
                if hasattr(result, "__await__"):
                    result = await result
                decisions.append(HookDecision.from_result(result, hook=hook_name))
            except Exception as exc:
                decisions.append(
                    HookDecision(
                        allow=False,
                        reason=f"{type(exc).__name__}: {exc}",
                        code="HOOK_FAILED",
                        hook=hook_name,
                    )
                )
        event_name = "verification" if hooks is self.verification else "task_end"
        for hook in self.external:
            if hook.event != event_name:
                continue
            decisions.append(await hook.run({"event": event_name, **payload}))
        return decisions


def _hook_name(hook: Any) -> str:
    return str(getattr(hook, "__name__", hook.__class__.__name__))
