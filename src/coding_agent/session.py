from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class AgentStatus(StrEnum):
    READY = "ready"
    BUILDING_CONTEXT = "building_context"
    CALLING_MODEL = "calling_model"
    EXECUTING_TOOL = "executing_tool"
    WAITING_APPROVAL = "waiting_approval"
    OBSERVING = "observing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class AgentState:
    session_id: str
    turn_id: str
    mode: str = "act"
    reasoning_mode: str | None = None
    status: AgentStatus = AgentStatus.READY
    step: int = 0
    max_steps: int = 20
    messages: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    changed_files: set[str] = field(default_factory=set)
    last_mutation_sequence: int = 0
    last_successful_verification_sequence: int = 0
    pending_approval: dict[str, Any] | None = None
    recent_actions: list[dict[str, Any]] = field(default_factory=list)
    completion_rejections: int = 0
    cancel_requested: bool = False
    token_usage: dict[str, int] = field(default_factory=dict)
    tool_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    context_summary: str = ""
    context_summary_meta: dict[str, Any] = field(default_factory=dict)
    recalled_memories: list[dict[str, Any]] = field(default_factory=list)
    recalled_user_memories: list[dict[str, Any]] = field(default_factory=list)
    current_objective: str = ""
    repair_attempts: int = 0
    max_repair_attempts: int = 3
    run_budget: dict[str, Any] = field(default_factory=dict)
    verification_evidence: list[dict[str, Any]] = field(default_factory=list)
    interrupted_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    recovery_warnings: list[dict[str, Any]] = field(default_factory=list)
    completed_tool_call_ids: set[str] = field(default_factory=set)
    last_applied_event_sequence: int = 0
    _reducer: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        from .session_reducer import SessionReducer

        self._reducer = SessionReducer(self)

    @classmethod
    def create(
        cls,
        *,
        max_steps: int = 20,
        mode: str = "act",
        reasoning_mode: str | None = None,
        session_id: str | None = None,
    ) -> "AgentState":
        return cls(
            session_id=session_id or uuid4().hex,
            turn_id=uuid4().hex,
            max_steps=max_steps,
            mode=mode,
            reasoning_mode=reasoning_mode,
        )

    def begin_new_turn(self) -> None:
        self.turn_id = uuid4().hex
        self.status = AgentStatus.READY
        self.step = 0
        self.changed_files.clear()
        self.last_mutation_sequence = 0
        self.last_successful_verification_sequence = 0
        self.pending_approval = None
        self.recent_actions.clear()
        self.completion_rejections = 0
        self.cancel_requested = False
        self.tool_stats.clear()
        self.context_summary = ""
        self.context_summary_meta.clear()
        self.recalled_memories.clear()
        self.recalled_user_memories.clear()
        self.current_objective = ""
        self.repair_attempts = 0
        self.run_budget.clear()
        self.verification_evidence.clear()
        self.interrupted_tool_calls.clear()
        self.recovery_warnings.clear()
        self.completed_tool_call_ids.clear()
        self.last_applied_event_sequence = 0
        if self._reducer is not None:
            self._reducer.reset()

    @property
    def verification_is_fresh(self) -> bool:
        return (
            bool(self.changed_files)
            and self.last_successful_verification_sequence
            > self.last_mutation_sequence
        )

    def apply_event(self, event: dict[str, Any]) -> None:
        """Apply one durable event through the same reducer used by recovery."""
        self._reducer.apply(event)

    def restore_from_events(
        self,
        events: list[dict[str, Any]],
        *,
        recovery_diagnostics: list[dict[str, Any]] | None = None,
    ) -> None:
        """Rehydrate durable conversation and observable state after a restart."""
        self.messages.clear()
        self.plan.clear()
        self.changed_files.clear()
        self.token_usage.clear()
        self.tool_stats.clear()
        self.pending_approval = None
        self.context_summary = ""
        self.context_summary_meta.clear()
        self.recalled_memories.clear()
        self.recalled_user_memories.clear()
        self.current_objective = ""
        self.repair_attempts = 0
        self.run_budget.clear()
        self.verification_evidence.clear()
        self.interrupted_tool_calls.clear()
        self.recovery_warnings = list(recovery_diagnostics or [])
        self.completed_tool_call_ids.clear()
        self.last_applied_event_sequence = 0
        self._reducer.reset()
        for event in events:
            self._reducer.apply(event)
        self._reducer.finalize_recovery()
