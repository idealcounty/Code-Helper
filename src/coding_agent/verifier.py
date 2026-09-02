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
        workflow = state.workflow_name
        if workflow == "add-feature":
            if not state.plan:
                return self._continue_or_partial(
                    state, "Add-feature requires a non-empty plan before completion"
                )
            if not any(str(item.get("acceptance") or "").strip() for item in state.plan):
                return self._continue_or_partial(
                    state, "Add-feature plan must include at least one acceptance condition"
                )

        incomplete_plan = any(
            item.get("status") in {"pending", "in_progress"} for item in state.plan
        )
        if incomplete_plan:
            return self._continue_or_partial(state, "The task plan is not complete")

        if state.pending_approval:
            return CompletionDecision(
                CompletionStatus.CONTINUE, "A tool approval is still pending"
            )

        if workflow == "code-review" and state.changed_files:
            return self._continue_or_partial(
                state, "Code-review workflow is read-only and cannot complete after a file change"
            )

        if workflow in {"add-feature", "bug-fix"} and not state.changed_files:
            return self._continue_or_partial(
                state, f"{workflow} must record at least one changed file before completion"
            )

        if state.changed_files and not state.verification_is_fresh:
            if state.repair_attempts >= state.max_repair_attempts:
                return CompletionDecision(
                    CompletionStatus.PARTIAL,
                    f"Verification failed after {state.max_repair_attempts} repair attempts",
                )
            return self._continue_or_partial(
                state,
                "Files changed after the latest successful verification. "
                "Run an appropriate command with purpose='verify'.",
            )

        if workflow in {"add-feature", "bug-fix", "code-review"} and state.workflow_stage != "finish":
            return self._continue_or_partial(
                state,
                f"{workflow} is not in the finish stage; complete the workflow gates before claiming completion",
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
