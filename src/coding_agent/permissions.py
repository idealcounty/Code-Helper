from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import time
from typing import Any, Iterable
from uuid import uuid4

from .tools.base import ToolRisk, ToolSpec


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolCapability(StrEnum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_WRITE = "workspace.write"
    PROCESS_EXEC = "process.exec"
    NETWORK_EGRESS = "network.egress"
    PATH_OUTSIDE_WORKSPACE = "path.outside_workspace"
    DEPENDENCY_INSTALL = "dependency.install"
    DESTRUCTIVE_RESTORE = "destructive.restore"


@dataclass(frozen=True, slots=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    capabilities: frozenset[str]
    path_prefix: str | None = None
    command_prefix: str | None = None
    expires_at: float | None = None
    grant_id: str = field(default_factory=lambda: f"grant-{uuid4().hex}")


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
            r"\b(?:ri|rmdir|erase)\b[^\r\n]*(?:-[^\r\n]*r|/[^\r\n]*[sq])",
            r"\b(?:diskpart|cipher\s+/w)\b",
        )
    )

    _network_pattern = re.compile(
        r"(?i)(?:https?|ssh|git)://|\b(?:curl|wget|Invoke-WebRequest|iwr)\b"
    )
    _install_pattern = re.compile(
        r"(?i)\b(?:pip|pip3|npm|pnpm|yarn|poetry|uv)\s+(?:install|add)\b"
    )

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.workspace_root = workspace_root.resolve() if workspace_root else None
        self._grants: dict[str, CapabilityGrant] = {}

    def evaluate(
        self,
        *,
        mode: str,
        spec: ToolSpec,
        arguments: dict[str, Any],
    ) -> PermissionResult:
        capabilities = self.capabilities_for(spec, arguments)
        capability_names = tuple(sorted(item.value for item in capabilities))
        if ToolCapability.PATH_OUTSIDE_WORKSPACE in capabilities:
            return PermissionResult(
                PermissionDecision.DENY,
                "Path is outside the configured workspace boundary",
                capability_names,
            )

        if spec.risk is ToolRisk.READ:
            return PermissionResult(
                PermissionDecision.ALLOW,
                "Read-only workspace tool",
                capability_names,
            )

        if mode != "act":
            return PermissionResult(
                PermissionDecision.DENY,
                f"{spec.name} is unavailable in {mode!r} mode",
                capability_names,
            )

        if spec.risk is ToolRisk.COMMAND:
            command = str(arguments.get("command", ""))
            if any(pattern.search(command) for pattern in self._dangerous_command_patterns):
                return PermissionResult(
                    PermissionDecision.DENY,
                    "Command matches a destructive-operation policy",
                    capability_names,
                )
            if self._matching_grant(spec, arguments, capabilities):
                return PermissionResult(
                    PermissionDecision.ALLOW,
                    "Allowed by a scoped, time-limited session grant",
                    capability_names,
                )
            return PermissionResult(
                PermissionDecision.ASK,
                "Commands require explicit user approval (" + ", ".join(capability_names) + ")",
                capability_names,
            )

        if spec.risk in {ToolRisk.WRITE, ToolRisk.DESTRUCTIVE}:
            if self._matching_grant(spec, arguments, capabilities):
                return PermissionResult(
                    PermissionDecision.ALLOW,
                    "Allowed by a scoped, time-limited session grant",
                    capability_names,
                )
            return PermissionResult(
                PermissionDecision.ASK,
                "File changes require explicit user approval (" + ", ".join(capability_names) + ")",
                capability_names,
            )
        return PermissionResult(
            PermissionDecision.DENY,
            "Unsupported risk classification",
            capability_names,
        )

    def capabilities_for(
        self, spec: ToolSpec, arguments: dict[str, Any]
    ) -> frozenset[ToolCapability]:
        if spec.risk is ToolRisk.READ:
            capabilities = {ToolCapability.WORKSPACE_READ}
        elif spec.risk is ToolRisk.WRITE:
            capabilities = {ToolCapability.WORKSPACE_WRITE}
        elif spec.risk is ToolRisk.COMMAND:
            capabilities = {ToolCapability.PROCESS_EXEC}
            command = str(arguments.get("command") or "")
            if self._network_pattern.search(command):
                capabilities.add(ToolCapability.NETWORK_EGRESS)
            if self._install_pattern.search(command):
                capabilities.add(ToolCapability.DEPENDENCY_INSTALL)
            try:
                if float(arguments.get("timeout", 0)) > 60:
                    capabilities.add(ToolCapability.PROCESS_EXEC)
            except (TypeError, ValueError):
                pass
        elif spec.risk is ToolRisk.DESTRUCTIVE:
            capabilities = {ToolCapability.DESTRUCTIVE_RESTORE}
        else:
            capabilities = set()

        path = arguments.get("path")
        if self.workspace_root is not None and isinstance(path, str):
            try:
                candidate = Path(path)
                if not candidate.is_absolute():
                    candidate = self.workspace_root / candidate
                if not candidate.resolve().is_relative_to(self.workspace_root):
                    capabilities.add(ToolCapability.PATH_OUTSIDE_WORKSPACE)
            except (OSError, ValueError):
                capabilities.add(ToolCapability.PATH_OUTSIDE_WORKSPACE)
        return frozenset(capabilities)

    def grant(
        self,
        capabilities: Iterable[ToolCapability | str],
        *,
        path_prefix: str | None = None,
        command_prefix: str | None = None,
        ttl_seconds: float = 3600,
    ) -> CapabilityGrant:
        grant = CapabilityGrant(
            capabilities=frozenset(str(item) for item in capabilities),
            path_prefix=self._normalize_path(path_prefix),
            command_prefix=command_prefix.strip() if command_prefix else None,
            expires_at=time() + max(1.0, ttl_seconds),
        )
        self._grants[grant.grant_id] = grant
        return grant

    def revoke(self, grant_id: str) -> bool:
        return self._grants.pop(grant_id, None) is not None

    def _matching_grant(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        capabilities: frozenset[ToolCapability],
    ) -> bool:
        now = time()
        for grant_id, grant in tuple(self._grants.items()):
            if grant.expires_at is not None and grant.expires_at <= now:
                self._grants.pop(grant_id, None)
                continue
            if not {item.value for item in capabilities}.issubset(grant.capabilities):
                continue
            if grant.path_prefix is not None:
                path = arguments.get("path")
                if not isinstance(path, str):
                    continue
                normalized = self._normalize_path(path)
                if normalized is None:
                    continue
                try:
                    if not Path(normalized).is_relative_to(Path(grant.path_prefix)):
                        continue
                except ValueError:
                    continue
            if grant.command_prefix is not None:
                command = str(arguments.get("command") or "").strip()
                if not command.casefold().startswith(grant.command_prefix.casefold()):
                    continue
            return True
        return False

    def _normalize_path(self, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if self.workspace_root is not None and not path.is_absolute():
            path = self.workspace_root / path
        try:
            return str(path.resolve())
        except OSError:
            return str(path.absolute())
