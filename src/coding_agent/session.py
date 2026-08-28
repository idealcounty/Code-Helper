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
    recalled_memories: list[dict[str, Any]] = field(default_factory=list)
    recalled_user_memories: list[dict[str, Any]] = field(default_factory=list)
    current_objective: str = ""
    repair_attempts: int = 0
    max_repair_attempts: int = 3
    run_budget: dict[str, Any] = field(default_factory=dict)

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
        self.recalled_memories.clear()
        self.recalled_user_memories.clear()
        self.current_objective = ""
        self.repair_attempts = 0
        self.run_budget.clear()

    @property
    def verification_is_fresh(self) -> bool:
        return (
            bool(self.changed_files)
            and self.last_successful_verification_sequence
            > self.last_mutation_sequence
        )

    def restore_from_events(self, events: list[dict[str, Any]]) -> None:
        """Rehydrate durable conversation and observable state after a restart."""
        self.messages.clear()
        self.plan.clear()
        self.changed_files.clear()
        self.token_usage.clear()
        self.tool_stats.clear()
        self.pending_approval = None
        self.context_summary = ""
        self.recalled_memories.clear()
        self.recalled_user_memories.clear()
        self.current_objective = ""
        self.repair_attempts = 0
        self.run_budget.clear()
        for event in events:
            payload = event.get("payload") or {}
            event_type = event.get("type")
            if event.get("turn_id"):
                self.turn_id = str(event["turn_id"])
            if event_type == "turn_started":
                self.current_objective = str(payload.get("message", ""))
                self.messages.append({"role": "user", "content": self.current_objective})
                self.step = 0
            elif event_type == "step_started":
                self.step = int(payload.get("step", self.step))
            elif event_type == "assistant_response":
                calls = payload.get("tool_calls") or []
                self.messages.append({
                    "role": "assistant",
                    "content": payload.get("content") or "",
                    **({"tool_calls": calls} if calls else {}),
                })
                usage = payload.get("usage") or {}
                for key, value in usage.items():
                    if isinstance(value, int):
                        self.token_usage[key] = self.token_usage.get(key, 0) + value
            elif event_type == "tool_result":
                result = payload.get("result") or {}
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": payload.get("id", "recovered"),
                    "name": payload.get("name", "unknown"),
                    "content": json.dumps(result, ensure_ascii=False),
                })
                name = str(payload.get("name", "unknown"))
                stats = self.tool_stats.setdefault(name, {"calls": 0, "successes": 0, "failures": 0, "duration_ms": 0})
                stats["calls"] += 1
                stats["successes" if result.get("ok") else "failures"] += 1
                duration = (result.get("metadata") or {}).get("duration_ms", 0)
                if isinstance(duration, int):
                    stats["duration_ms"] += duration
                metadata = result.get("metadata") or {}
                self.changed_files.update(map(str, metadata.get("mutated_files", [])))
            elif event_type == "plan_updated":
                self.plan = list(payload.get("plan") or [])
            elif event_type == "context_compacted":
                self.context_summary = str(payload.get("summary") or "")
            elif event_type == "repair_attempt":
                self.repair_attempts = max(self.repair_attempts, int(payload.get("attempt", 0)))
            elif event_type in {"run_budget_started", "run_budget_updated", "run_budget_exhausted"}:
                self.run_budget = dict(payload.get("budget") or self.run_budget)
            elif event_type == "turn_finished":
                try:
                    self.status = AgentStatus(payload.get("status", self.status))
                except ValueError:
                    pass
                self.token_usage.update(payload.get("token_usage") or {})
                self.tool_stats.update(payload.get("tool_stats") or {})
