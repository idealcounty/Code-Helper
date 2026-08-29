"""Load workspace-local external Hook definitions without executing config code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hooks import ExternalHookSpec


CONFIG_PATH = Path(".code-helper") / "hooks.json"
MAX_HOOKS = 16
EVENTS = {"pre_tool", "post_tool", "verification", "task_end"}


@dataclass(frozen=True, slots=True)
class HookConfig:
    hooks: tuple[ExternalHookSpec, ...] = ()
    diagnostics: tuple[str, ...] = ()


def load_hook_config(workspace_root: Path) -> HookConfig:
    path = workspace_root / CONFIG_PATH
    if not path.is_file():
        return HookConfig()
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return HookConfig(diagnostics=(f"Unable to load {CONFIG_PATH}: {exc}",))
    if not isinstance(raw, dict) or not isinstance(raw.get("hooks", []), list):
        return HookConfig(diagnostics=("hooks.json must contain an object with a 'hooks' array",))

    hooks: list[ExternalHookSpec] = []
    diagnostics: list[str] = []
    entries = raw["hooks"]
    for index, entry in enumerate(entries[:MAX_HOOKS]):
        prefix = f"hooks[{index}]"
        if not isinstance(entry, dict):
            diagnostics.append(f"{prefix} must be an object")
            continue
        event = str(entry.get("event") or "").strip().lower()
        if event in {"pretooluse", "pre_tool_use"}:
            event = "pre_tool"
        elif event in {"posttooluse", "post_tool_use"}:
            event = "post_tool"
        elif event in {"onverification", "on_verification"}:
            event = "verification"
        elif event in {"ontaskend", "on_task_end"}:
            event = "task_end"
        if event not in EVENTS:
            diagnostics.append(f"{prefix}.event must be one of {sorted(EVENTS)}")
            continue
        argv = entry.get("argv")
        if not isinstance(argv, list) or not argv or any(
            not isinstance(item, str) or not item.strip() or "\x00" in item for item in argv
        ):
            diagnostics.append(f"{prefix}.argv must be a non-empty string array")
            continue
        try:
            timeout = float(entry.get("timeout", 5.0))
        except (TypeError, ValueError):
            diagnostics.append(f"{prefix}.timeout must be a number")
            continue
        if not 0.1 <= timeout <= 10.0:
            diagnostics.append(f"{prefix}.timeout must be between 0.1 and 10 seconds")
            continue
        cwd_value = entry.get("cwd", ".")
        if not isinstance(cwd_value, str) or not cwd_value.strip():
            diagnostics.append(f"{prefix}.cwd must be a relative directory")
            continue
        cwd = (workspace_root / cwd_value).resolve()
        if not cwd.is_relative_to(workspace_root.resolve()):
            diagnostics.append(f"{prefix}.cwd must stay inside the workspace")
            continue
        matcher = entry.get("matcher")
        if isinstance(matcher, dict):
            matcher = matcher.get("tool")
        if matcher is not None and (not isinstance(matcher, str) or not matcher.strip()):
            diagnostics.append(f"{prefix}.matcher must be a tool name string")
            continue
        hooks.append(
            ExternalHookSpec(
                event=event,
                argv=tuple(argv),
                workspace_root=workspace_root.resolve(),
                matcher=str(matcher).strip() if matcher else None,
                timeout=timeout,
                cwd=cwd,
                name=str(entry.get("name") or f"external:{event}:{len(hooks) + 1}"),
            )
        )
    if len(entries) > MAX_HOOKS:
        diagnostics.append(f"only the first {MAX_HOOKS} hooks are used")
    return HookConfig(tuple(hooks), tuple(diagnostics))
