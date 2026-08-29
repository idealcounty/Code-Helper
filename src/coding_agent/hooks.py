from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .tools.base import ToolResult

PreToolHook = Callable[[str, dict[str, Any]], Awaitable[None] | None]
PostToolHook = Callable[[str, dict[str, Any], ToolResult], Awaitable[ToolResult | None] | ToolResult | None]
LifecycleHook = Callable[[dict[str, Any]], Awaitable[Any] | Any]


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
    ) -> None:
        self.pre = pre or []
        self.post = post or []
        self.verification = verification or []
        self.task_end = task_end or []

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
        return HookDecision()

    async def after(self, name: str, arguments: dict[str, Any], result: ToolResult) -> ToolResult:
        current = result
        for hook in self.post:
            replacement = hook(name, arguments, current)
            if hasattr(replacement, "__await__"):
                replacement = await replacement
            if replacement is not None:
                current = replacement
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
        return decisions


def _hook_name(hook: Any) -> str:
    return str(getattr(hook, "__name__", hook.__class__.__name__))
