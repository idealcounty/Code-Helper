from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .tools.base import ToolRisk, ToolSpec


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str


class PermissionPolicy:
    _dangerous_command_patterns = tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\brm\s+-[^\r\n]*r[^\r\n]*f\b",
            r"\bgit\s+reset\s+--hard\b",
            r"\bgit\s+clean\s+-[^\r\n]*f",
            r"\bformat(?:\.com)?\s+[a-z]:",
            r"\bshutdown\b",
            r"\bRemove-Item\b[^\r\n]*\b-Recurse\b",
            r"\bdel\b[^\r\n]*/[sq]",
        )
    )

    def evaluate(
        self,
        *,
        mode: str,
        spec: ToolSpec,
        arguments: dict[str, Any],
    ) -> PermissionResult:
        if spec.risk is ToolRisk.READ:
            return PermissionResult(PermissionDecision.ALLOW, "Read-only workspace tool")

        if mode != "act":
            return PermissionResult(
                PermissionDecision.DENY,
                f"{spec.name} is unavailable in {mode!r} mode",
            )

        if spec.risk is ToolRisk.COMMAND:
            command = str(arguments.get("command", ""))
            if any(pattern.search(command) for pattern in self._dangerous_command_patterns):
                return PermissionResult(
                    PermissionDecision.DENY,
                    "Command matches a destructive-operation policy",
                )
            return PermissionResult(
                PermissionDecision.ASK,
                "Commands require explicit user approval",
            )

        if spec.risk in {ToolRisk.WRITE, ToolRisk.DESTRUCTIVE}:
            return PermissionResult(
                PermissionDecision.ASK,
                "File changes require explicit user approval",
            )
        return PermissionResult(PermissionDecision.DENY, "Unsupported risk classification")

