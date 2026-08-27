from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .model import ModelResponse
from .session import AgentState


class CompletionStatus(StrEnum):
    COMPLETED = "completed"
    CONTINUE = "continue"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    status: CompletionStatus
    reason: str


class Verifier:
    """Deterministic completion gate independent of the model's confidence."""

    def evaluate(
        self, state: AgentState, response: ModelResponse
    ) -> CompletionDecision:
        incomplete_plan = any(
            item.get("status") in {"pending", "in_progress"} for item in state.plan
        )
        if incomplete_plan:
            return self._continue_or_partial(state, "The task plan is not complete")

        if state.pending_approval:
            return CompletionDecision(
                CompletionStatus.CONTINUE, "A tool approval is still pending"
            )

        if state.changed_files and not state.verification_is_fresh:
            return self._continue_or_partial(
                state,
                "Files changed after the latest successful verification. "
                "Run an appropriate command with purpose='verify'.",
            )

        return CompletionDecision(
            CompletionStatus.COMPLETED,
            response.content or "Task completed",
        )

    @staticmethod
    def _continue_or_partial(state: AgentState, reason: str) -> CompletionDecision:
        state.completion_rejections += 1
        if state.completion_rejections >= 3:
            return CompletionDecision(CompletionStatus.PARTIAL, reason)
        return CompletionDecision(CompletionStatus.CONTINUE, reason)
