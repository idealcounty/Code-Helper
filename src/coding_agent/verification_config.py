"""Optional, workspace-local verification command configuration.

The configuration only tells the verifier which commands are intentional
project checks. It never executes commands or changes the permission policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_RELATIVE_PATH = Path(".code-helper") / "verification.json"
MAX_COMMANDS = 32
MAX_COMMAND_LENGTH = 1_000


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    commands: tuple[str, ...] = ()
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

        commands: list[str] = []
        diagnostics: list[str] = []
        for index, entry in enumerate(entries[:MAX_COMMANDS], start=1):
            value = entry.get("command") if isinstance(entry, dict) else entry
            if not isinstance(value, str):
                diagnostics.append(f"commands[{index - 1}] must be a string or object with 'command'")
                continue
            command = " ".join(value.split())
            if not command:
                diagnostics.append(f"commands[{index - 1}] must not be empty")
                continue
            if len(command) > MAX_COMMAND_LENGTH:
                diagnostics.append(f"commands[{index - 1}] exceeds {MAX_COMMAND_LENGTH} characters")
                continue
            if command.casefold() not in {item.casefold() for item in commands}:
                commands.append(command)
        if len(entries) > MAX_COMMANDS:
            diagnostics.append(f"only the first {MAX_COMMANDS} commands are used")
        return cls(commands=tuple(commands), diagnostics=tuple(diagnostics))

    def matches(self, command: str) -> bool:
        normalized = " ".join(command.strip().split()).casefold()
        return bool(normalized) and any(item.casefold() == normalized for item in self.commands)

