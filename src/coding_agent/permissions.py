from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import time
from typing import Any, Iterable
from urllib.parse import urlsplit
from uuid import uuid4

from .tools.base import ToolRisk, ToolSpec


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalMode(StrEnum):
    ASK = "ask"
    AUTO = "auto"
    FULL = "full"


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

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        approval_mode: ApprovalMode | str = ApprovalMode.ASK,
        policy_path: Path | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve() if workspace_root else None
        self.approval_mode = ApprovalMode(approval_mode)
        self._grants: dict[str, CapabilityGrant] = {}
        self.policy_path = policy_path or (
            self.workspace_root / ".code-helper" / "policy.json"
            if self.workspace_root is not None
            else None
        )
        self.policy_diagnostics: list[dict[str, str]] = []
        self._configured_command_patterns: tuple[re.Pattern[str], ...] = ()
        self._blocked_network_domains: frozenset[str] = frozenset()
        self._protected_paths: tuple[str, ...] = ()
        self._load_policy()

    def set_approval_mode(self, value: ApprovalMode | str) -> ApprovalMode:
        self.approval_mode = ApprovalMode(value)
        return self.approval_mode

    def evaluate(
        self,
        *,
        mode: str,
        spec: ToolSpec,
        arguments: dict[str, Any],
    ) -> PermissionResult:
        capabilities = self.capabilities_for(spec, arguments)
        capability_names = tuple(sorted(item.value for item in capabilities))
        if self.approval_mode is ApprovalMode.FULL and mode == "act":
            return PermissionResult(
                PermissionDecision.ALLOW,
                "Allowed by the current session's full-access policy",
                capability_names,
            )
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
            command = _command_text(arguments)
            if self._command_is_blocked(command):
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
                PermissionDecision.ALLOW
                if self.approval_mode is ApprovalMode.AUTO
                else PermissionDecision.ASK,
                (
                    "Automatically approved by the current session policy ("
                    if self.approval_mode is ApprovalMode.AUTO
                    else "Commands require explicit user approval ("
                )
                + ", ".join(capability_names)
                + ")",
                capability_names,
            )

        if spec.risk in {ToolRisk.WRITE, ToolRisk.DESTRUCTIVE}:
            protected = self._protected_path_match(arguments.get("path"))
            if protected is not None:
                return PermissionResult(
                    PermissionDecision.DENY,
                    f"Path is protected by the workspace policy: {protected}",
                    capability_names,
                )
            if self._matching_grant(spec, arguments, capabilities):
                return PermissionResult(
                    PermissionDecision.ALLOW,
                    "Allowed by a scoped, time-limited session grant",
                    capability_names,
                )
            return PermissionResult(
                PermissionDecision.ALLOW
                if self.approval_mode is ApprovalMode.AUTO
                else PermissionDecision.ASK,
                (
                    "Automatically approved by the current session policy ("
                    if self.approval_mode is ApprovalMode.AUTO
                    else "File changes require explicit user approval ("
                )
                + ", ".join(capability_names)
                + ")",
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
            command = _command_text(arguments)
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

    def grants_snapshot(self) -> list[dict[str, Any]]:
        """Return non-expired, non-secret scope metadata for UI/audit views."""
        now = time()
        snapshot: list[dict[str, Any]] = []
        for grant_id, grant in tuple(self._grants.items()):
            if grant.expires_at is not None and grant.expires_at <= now:
                self._grants.pop(grant_id, None)
                continue
            snapshot.append(
                {
                    "grant_id": grant.grant_id,
                    "capabilities": sorted(grant.capabilities),
                    "path_prefix": grant.path_prefix,
                    "command_prefix": grant.command_prefix,
                    "expires_at": grant.expires_at,
                }
            )
        return sorted(snapshot, key=lambda item: str(item["grant_id"]))

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
                command = _command_text(arguments).strip()
                if not command.casefold().startswith(grant.command_prefix.casefold()):
                    continue
            return True
        return False

    def _command_is_blocked(self, command: str) -> bool:
        if any(pattern.search(command) for pattern in self._dangerous_command_patterns):
            return True
        if any(pattern.search(command) for pattern in self._configured_command_patterns):
            return True
        if self._blocked_network_domains:
            for match in re.finditer(r"(?i)\b(?:https?|ssh|git)://[^\s'\"]+", command):
                try:
                    hostname = (urlsplit(match.group(0)).hostname or "").casefold().rstrip(".")
                except ValueError:
                    continue
                if any(
                    hostname == domain or hostname.endswith("." + domain)
                    for domain in self._blocked_network_domains
                ):
                    return True
        return False

    def _protected_path_match(self, value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip() or self.workspace_root is None:
            return None
        normalized = self._normalize_path(value)
        if normalized is None:
            return None
        candidate = Path(normalized)
        for protected in self._protected_paths:
            root = Path(protected)
            try:
                if candidate == root or candidate.is_relative_to(root):
                    return protected
            except ValueError:
                continue
        return None

    def _load_policy(self) -> None:
        if self.policy_path is None:
            return
        try:
            if self.policy_path.stat().st_size > 64 * 1024:
                raise ValueError("policy file exceeds 64 KiB")
            payload = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.policy_diagnostics.append({"code": "POLICY_LOAD_FAILED", "message": str(exc)})
            return
        if not isinstance(payload, dict):
            self.policy_diagnostics.append({"code": "POLICY_INVALID", "message": "root must be an object"})
            return

        raw_patterns = payload.get("deny_command_patterns", [])
        if isinstance(raw_patterns, list):
            compiled: list[re.Pattern[str]] = []
            for index, item in enumerate(raw_patterns[:64]):
                if not isinstance(item, str) or not item.strip() or len(item) > 500:
                    self.policy_diagnostics.append({"code": "POLICY_PATTERN_IGNORED", "message": f"deny_command_patterns[{index}] is invalid"})
                    continue
                try:
                    compiled.append(re.compile(item, re.IGNORECASE))
                except re.error as exc:
                    self.policy_diagnostics.append({"code": "POLICY_PATTERN_IGNORED", "message": f"deny_command_patterns[{index}]: {exc}"})
            self._configured_command_patterns = tuple(compiled)
        elif "deny_command_patterns" in payload:
            self.policy_diagnostics.append({"code": "POLICY_INVALID", "message": "deny_command_patterns must be an array"})

        raw_domains = payload.get("deny_network_domains", [])
        if isinstance(raw_domains, list):
            domains: set[str] = set()
            for index, item in enumerate(raw_domains[:64]):
                if not isinstance(item, str):
                    self.policy_diagnostics.append({"code": "POLICY_DOMAIN_IGNORED", "message": f"deny_network_domains[{index}] is invalid"})
                    continue
                domain = item.strip().casefold().rstrip(".")
                if domain and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain):
                    domains.add(domain)
                else:
                    self.policy_diagnostics.append({"code": "POLICY_DOMAIN_IGNORED", "message": f"deny_network_domains[{index}] is invalid"})
            self._blocked_network_domains = frozenset(domains)
        elif "deny_network_domains" in payload:
            self.policy_diagnostics.append({"code": "POLICY_INVALID", "message": "deny_network_domains must be an array"})

        raw_paths = payload.get("protected_paths", [])
        if isinstance(raw_paths, list) and self.workspace_root is not None:
            paths: list[str] = []
            for index, item in enumerate(raw_paths[:64]):
                if not isinstance(item, str) or not item.strip():
                    self.policy_diagnostics.append({"code": "POLICY_PATH_IGNORED", "message": f"protected_paths[{index}] is invalid"})
                    continue
                try:
                    candidate = Path(item)
                    if not candidate.is_absolute():
                        candidate = self.workspace_root / candidate
                    resolved = candidate.resolve()
                    if not resolved.is_relative_to(self.workspace_root):
                        raise ValueError("path is outside workspace")
                    paths.append(str(resolved))
                except (OSError, ValueError) as exc:
                    self.policy_diagnostics.append({"code": "POLICY_PATH_IGNORED", "message": f"protected_paths[{index}]: {exc}"})
            self._protected_paths = tuple(paths)
        elif "protected_paths" in payload:
            self.policy_diagnostics.append({"code": "POLICY_INVALID", "message": "protected_paths must be an array"})

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


def _command_text(arguments: dict[str, Any]) -> str:
    """Normalize shell and structured invocations for policy classification."""
    argv = arguments.get("argv")
    if isinstance(argv, list):
        return " ".join(str(item) for item in argv)
    return str(arguments.get("command") or "")
