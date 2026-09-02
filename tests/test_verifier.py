from __future__ import annotations

from coding_agent.model import ModelResponse
from coding_agent.session import AgentState
from coding_agent.verifier import CompletionStatus, Verifier


def test_add_feature_requires_acceptance_and_fresh_verification() -> None:
    state = AgentState.create()
    state.workflow_name = "add-feature"
    state.workflow_stage = "verify"
    state.plan = [{"step": "实现功能", "status": "completed"}]
    state.changed_files.add("app.py")

    decision = Verifier().evaluate(state, ModelResponse(content="done"))

    assert decision.status is CompletionStatus.CONTINUE
    assert "acceptance" in decision.reason


def test_bug_fix_with_fresh_verification_can_finish() -> None:
    state = AgentState.create()
    state.workflow_name = "bug-fix"
    state.workflow_stage = "finish"
    state.changed_files.add("app.py")
    state.last_mutation_sequence = 2
    state.last_successful_verification_sequence = 3

    decision = Verifier().evaluate(state, ModelResponse(content="fixed"))

    assert decision.status is CompletionStatus.COMPLETED


def test_code_review_can_finish_without_changes() -> None:
    state = AgentState.create()
    state.workflow_name = "code-review"
    state.workflow_stage = "finish"

    decision = Verifier().evaluate(state, ModelResponse(content="No findings."))

    assert decision.status is CompletionStatus.COMPLETED


def test_code_review_cannot_finish_after_a_file_change() -> None:
    state = AgentState.create()
    state.workflow_name = "code-review"
    state.workflow_stage = "verify"
    state.changed_files.add("app.py")
    state.last_mutation_sequence = 1
    state.last_successful_verification_sequence = 2

    decision = Verifier().evaluate(state, ModelResponse(content="reviewed"))

    assert decision.status is not CompletionStatus.COMPLETED
    assert "read-only" in decision.reason
