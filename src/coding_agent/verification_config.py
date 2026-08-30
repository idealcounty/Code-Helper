"""Optional, workspace-local verification command configuration.

The configuration only tells the verifier which commands are intentional
project checks. It never executes commands or changes the permission policy.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_RELATIVE_PATH = Path(".code-helper") / "verification.json"
MAX_COMMANDS = 32
MAX_COMMAND_LENGTH = 1_000
MAX_RULES = 32
MAX_RULE_PATHS = 32
MAX_PATTERN_LENGTH = 300
KNOWN_TASK_PROFILES = frozenset({"project", "algorithm"})


@dataclass(frozen=True, slots=True)
class VerificationRule:
    commands: tuple[str, ...]
    task_profiles: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def matches(self, *, task_profile: str, paths: tuple[str, ...]) -> bool:
        if self.task_profiles and task_profile.casefold() not in self.task_profiles:
            return False
        if not self.paths:
            return True
        normalized_paths = tuple(_normalize_path(path).casefold() for path in paths)
        return any(
            fnmatch.fnmatchcase(path, pattern.casefold())
            for path in normalized_paths
            for pattern in self.paths
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "commands": list(self.commands),
            "task_profiles": list(self.task_profiles),
            "paths": list(self.paths),
        }


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    commands: tuple[str, ...] = ()
    rules: tuple[VerificationRule, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @classmethod
    def load(cls, workspace_root: Path) -> "VerificationConfig":
        path = workspace_root / CONFIG_RELATIVE_PATH
        if not path.is_file():
            return cls()
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return cls(diagnostics=(f"Unable to load {CONFIG_RELATIVE_PATH}: {exc}",))
        if not isinstance(raw, dict):
            return cls(diagnostics=("verification.json must contain a JSON object",))

        entries = raw.get("commands", [])
        if not isinstance(entries, list):
            return cls(diagnostics=("verification.json 'commands' must be an array",))

        diagnostics: list[str] = []
        commands = _parse_commands(entries, "commands", diagnostics)

        raw_rules = raw.get("rules", [])
        if not isinstance(raw_rules, list):
            diagnostics.append("verification.json 'rules' must be an array")
            raw_rules = []
        rules: list[VerificationRule] = []
        for index, entry in enumerate(raw_rules[:MAX_RULES]):
            prefix = f"rules[{index}]"
            if not isinstance(entry, dict):
                diagnostics.append(f"{prefix} must be an object")
                continue
            raw_rule_commands = entry.get("commands", [])
            if not isinstance(raw_rule_commands, list):
                diagnostics.append(f"{prefix}.commands must be an array")
                continue
            rule_commands = _parse_commands(
                raw_rule_commands, f"{prefix}.commands", diagnostics
            )
            raw_profiles = entry.get("task_profiles", [])
            raw_paths = entry.get("paths", [])
            profiles = _parse_profiles(raw_profiles, prefix, diagnostics)
            paths = _parse_paths(raw_paths, prefix, diagnostics)
            invalid_profile_selector = not isinstance(raw_profiles, list) or (
                bool(raw_profiles) and not profiles
            )
            invalid_path_selector = not isinstance(raw_paths, list) or (
                bool(raw_paths) and not paths
            )
            if invalid_profile_selector or invalid_path_selector:
                diagnostics.append(
                    f"{prefix} is ignored because a declared selector has no valid values"
                )
                continue
            if not profiles and not paths:
                diagnostics.append(
                    f"{prefix} must select at least one task_profile or path"
                )
                continue
            if not rule_commands:
                diagnostics.append(f"{prefix} has no valid commands")
                continue
            rules.append(
                VerificationRule(
                    commands=rule_commands,
                    task_profiles=profiles,
                    paths=paths,
                )
            )
        if len(raw_rules) > MAX_RULES:
            diagnostics.append(f"only the first {MAX_RULES} rules are used")
        return cls(
            commands=commands,
            rules=tuple(rules),
            diagnostics=tuple(diagnostics),
        )

    def matches(self, command: str) -> bool:
        normalized = " ".join(command.strip().split()).casefold()
        return bool(normalized) and any(
            item.casefold() == normalized for item in self.commands
        )

    @property
    def all_commands(self) -> tuple[str, ...]:
        return _deduplicate(
            [*self.commands, *(command for rule in self.rules for command in rule.commands)]
        )

    def commands_for(
        self, *, task_profile: str, paths: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        selected = list(self.commands)
        for rule in self.rules:
            if rule.matches(task_profile=task_profile, paths=paths):
                selected.extend(rule.commands)
        return _deduplicate(selected)

    def commands_for_state(self, state: Any) -> tuple[str, ...]:
        """Select commands from reducer facts without trusting model prose."""
        paths = [str(path) for path in getattr(state, "changed_files", ())]
        for action in getattr(state, "recent_actions", ()):
            if str(action.get("result_code") or "") != "OK":
                continue
            try:
                signature = json.loads(str(action.get("signature") or "{}"))
            except (AttributeError, TypeError, ValueError):
                continue
            arguments = signature.get("arguments") or {}
            path = arguments.get("path") if isinstance(arguments, dict) else None
            if isinstance(path, str) and path.strip():
                paths.append(path)
        return self.commands_for(
            task_profile=str(getattr(state, "task_profile", "project") or "project"),
            paths=tuple(paths),
        )


def _parse_commands(
    entries: list[Any], prefix: str, diagnostics: list[str]
) -> tuple[str, ...]:
    commands: list[str] = []
    for index, entry in enumerate(entries[:MAX_COMMANDS]):
        value = entry.get("command") if isinstance(entry, dict) else entry
        if not isinstance(value, str):
            diagnostics.append(
                f"{prefix}[{index}] must be a string or object with 'command'"
            )
            continue
        command = " ".join(value.split())
        if not command:
            diagnostics.append(f"{prefix}[{index}] must not be empty")
            continue
        if len(command) > MAX_COMMAND_LENGTH:
            diagnostics.append(
                f"{prefix}[{index}] exceeds {MAX_COMMAND_LENGTH} characters"
            )
            continue
        commands.append(command)
    if len(entries) > MAX_COMMANDS:
        diagnostics.append(f"only the first {MAX_COMMANDS} {prefix} entries are used")
    return _deduplicate(commands)


def _parse_profiles(
    value: Any, prefix: str, diagnostics: list[str]
) -> tuple[str, ...]:
    if not isinstance(value, list):
        diagnostics.append(f"{prefix}.task_profiles must be an array")
        return ()
    profiles: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or item.casefold() not in KNOWN_TASK_PROFILES:
            diagnostics.append(
                f"{prefix}.task_profiles[{index}] must be project or algorithm"
            )
            continue
        profiles.append(item.casefold())
    return _deduplicate(profiles)


def _parse_paths(value: Any, prefix: str, diagnostics: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        diagnostics.append(f"{prefix}.paths must be an array")
        return ()
    patterns: list[str] = []
    for index, item in enumerate(value[:MAX_RULE_PATHS]):
        if not isinstance(item, str):
            diagnostics.append(f"{prefix}.paths[{index}] must be a string")
            continue
        pattern = _normalize_path(item)
        segments = pattern.split("/")
        if (
            not pattern
            or len(pattern) > MAX_PATTERN_LENGTH
            or pattern.startswith("/")
            or re.match(r"^[a-zA-Z]:", pattern)
            or ".." in segments
        ):
            diagnostics.append(
                f"{prefix}.paths[{index}] must be a relative workspace glob"
            )
            continue
        patterns.append(pattern.casefold())
    if len(value) > MAX_RULE_PATHS:
        diagnostics.append(
            f"only the first {MAX_RULE_PATHS} {prefix}.paths entries are used"
        )
    return _deduplicate(patterns)


def _normalize_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _deduplicate(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return tuple(result)
