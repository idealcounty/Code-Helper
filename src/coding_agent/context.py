from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .session import AgentState


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
    def __init__(self, system_prompt: str = BASE_SYSTEM_PROMPT) -> None:
        self.system_prompt = system_prompt.strip()

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
        if state.plan:
            system += f"\n\nCurrent plan: {state.plan!r}"
        return ModelContext(
            messages=[{"role": "system", "content": system}, *state.messages],
            allowed_tools=tool_schemas,
        )

