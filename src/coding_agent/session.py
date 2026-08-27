from __future__ import annotations

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

    @property
    def verification_is_fresh(self) -> bool:
        return (
            bool(self.changed_files)
            and self.last_successful_verification_sequence
            > self.last_mutation_sequence
        )
