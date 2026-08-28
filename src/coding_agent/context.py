from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .session import AgentState
from .skills import SkillLibrary
from .tools.workspace import Workspace


BASE_SYSTEM_PROMPT = """You are Code Helper, a local coding agent.
Work only through the provided tools. Inspect the project before changing it.
Use small, targeted edits. After modifying files, run a relevant verification
command with purpose='verify'. Do not claim success without fresh verification.
Respect tool errors and user approval decisions. Stop when the task is complete
or explain precisely why it is only partially complete.
"""


@dataclass(frozen=True, slots=True)
class ModelContext:
    messages: list[dict[str, Any]]
    allowed_tools: list[dict[str, Any]]


class ContextManager:
    def __init__(
        self,
        system_prompt: str = BASE_SYSTEM_PROMPT,
        *,
        workspace: Workspace | None = None,
        skill_library: SkillLibrary | None = None,
    ) -> None:
        self.system_prompt = system_prompt.strip()
        self.workspace = workspace
        self.skill_library = skill_library

    def build(
        self,
        state: AgentState,
        tool_schemas: list[dict[str, Any]],
    ) -> ModelContext:
        mode_rule = {
            "ask": "Ask mode: use read-only tools and do not modify files or run commands.",
            "plan": "Plan mode: inspect and plan only; do not modify files or run commands.",
            "act": "Act mode: modifications and commands are available subject to policy.",
        }.get(state.mode, f"Current mode: {state.mode}")

        system = f"{self.system_prompt}\n\n{mode_rule}"
        project_rules = self._project_rules()
        if project_rules:
            system += f"\n\nProject rules:\n{project_rules}"
        if state.plan:
            system += f"\n\nCurrent plan: {state.plan!r}"
        skills = self._skills_summary()
        if skills:
            system += "\n\nAvailable project skills (call load_skill only when applicable):\n" + skills
        return ModelContext(
            messages=[{"role": "system", "content": system}, *state.messages],
            allowed_tools=tool_schemas,
        )

    def _project_rules(self) -> str:
        if self.workspace is None:
            return ""
        blocks: list[str] = []
        for path in _find_agent_rule_files(self.workspace):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            text = text.strip()
            if not text:
                continue
            relative = self.workspace.relative(path)
            blocks.append(f"[{relative}]\n{text[:4_000]}")
            if len(blocks) >= 5:
                break
        return "\n\n".join(blocks)

    def _skills_summary(self) -> str:
        if self.skill_library is None:
            return ""
        return "\n".join(
            f"- {item.name}: {item.description} (use when: {item.when_to_use})"
            for item in self.skill_library.list_summaries()
        )


def _find_agent_rule_files(workspace: Workspace) -> list[Path]:
    files: list[Path] = []
    root_rule = workspace.root / "AGENTS.md"
    if root_rule.is_file():
        files.append(root_rule)
    for path in sorted(workspace.root.rglob("AGENTS.md")):
        if path == root_rule or workspace.is_ignored(path) or workspace.is_sensitive(path):
            continue
        files.append(path)
    return files
