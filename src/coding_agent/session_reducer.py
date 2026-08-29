"""Deterministic event reduction shared by live runs and session recovery."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from .session import AgentState, AgentStatus


class SessionReducer:
    """Apply the durable event vocabulary to an :class:`AgentState`.

    The reducer deliberately never executes tools.  It only projects recorded
    facts into state, making recovery safe even when a process died mid-tool.
    """

    def __init__(self, state: AgentState) -> None:
        self.state = state
        self.reset()

    def reset(self) -> None:
        self._seen_event_ids: set[str] = set()
        self._inflight: dict[str, dict[str, Any]] = {}

    def apply(self, event: Mapping[str, Any] | Any) -> None:
        data = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_id = str(data.get("event_id") or "")
        if event_id and event_id in self._seen_event_ids:
            return
        if event_id:
            self._seen_event_ids.add(event_id)

        sequence = _int(data.get("sequence"), 0)
        self.state.last_applied_event_sequence = max(
            self.state.last_applied_event_sequence, sequence
        )
        if data.get("turn_id"):
            self.state.turn_id = str(data["turn_id"])
        payload = data.get("payload") or {}
        event_type = str(data.get("type") or "")

        if event_type == "turn_started":
            self._start_turn(str(payload.get("message") or ""))
        elif event_type == "step_started":
            self.state.step = _int(payload.get("step"), self.state.step)
            self.state.status = AgentStatus.BUILDING_CONTEXT
        elif event_type == "model_started":
            self.state.status = AgentStatus.CALLING_MODEL
        elif event_type == "assistant_response":
            self._assistant_response(payload)
        elif event_type == "tool_requested":
            pass
        elif event_type == "approval_requested":
            self._approval_requested(payload)
        elif event_type == "approval_result":
            self._approval_result(payload)
        elif event_type == "tool_started":
            self._tool_started(payload, sequence)
        elif event_type == "tool_result":
            self._tool_result(payload, sequence)
        elif event_type == "plan_updated":
            self.state.plan = copy.deepcopy(list(payload.get("plan") or []))
        elif event_type == "context_compacted":
            self.state.context_summary = str(payload.get("summary") or "")
            self.state.context_summary_meta = copy.deepcopy(
                dict(payload.get("summary_meta") or {})
            )
        elif event_type == "hook_context":
            content = str(payload.get("content") or "").strip()
            if content:
                self.state.messages.append(
                    {
                        "role": "system",
                        "content": f"HOOK CONTEXT ({payload.get('lifecycle') or 'lifecycle'}): {content}",
                    }
                )
        elif event_type == "repair_attempt":
            self.state.repair_attempts = max(
                self.state.repair_attempts, _int(payload.get("attempt"), 0)
            )
        elif event_type == "verification_recorded":
            self._verification_recorded(payload, sequence)
        elif event_type == "completion_checked":
            self.state.status = AgentStatus.VERIFYING
        elif event_type == "verification_required":
            self.state.messages.append(
                {
                    "role": "user",
                    "content": f"SYSTEM OBSERVATION: {payload.get('reason') or ''}",
                }
            )
        elif event_type == "checkpoint_restored":
            self.state.changed_files.clear()
            self.state.last_mutation_sequence = sequence
            self.state.last_successful_verification_sequence = 0
            self.state.verification_evidence.clear()
        elif event_type in {
            "run_budget_started",
            "run_budget_updated",
            "run_budget_exhausted",
        }:
            budget = payload.get("budget")
            if isinstance(budget, Mapping):
                self.state.run_budget = copy.deepcopy(dict(budget))
        elif event_type == "run_cancelled":
            self.state.cancel_requested = True
            self._mark_inflight_interrupted()
        elif event_type == "turn_finished":
            self._turn_finished(payload)

    def finalize_recovery(self) -> None:
        """Mark tool calls that had a start but no durable result as unknown."""
        self._mark_inflight_interrupted()

    def _start_turn(self, objective: str) -> None:
        self.state.status = AgentStatus.READY
        self.state.step = 0
        self.state.changed_files.clear()
        self.state.last_mutation_sequence = 0
        self.state.last_successful_verification_sequence = 0
        self.state.pending_approval = None
        self.state.recent_actions.clear()
        self.state.completion_rejections = 0
        self.state.cancel_requested = False
        self.state.tool_stats.clear()
        self.state.context_summary = ""
        self.state.context_summary_meta.clear()
        self.state.recalled_memories.clear()
        self.state.recalled_user_memories.clear()
        self.state.current_objective = objective
        self.state.repair_attempts = 0
        self.state.run_budget.clear()
        self.state.verification_evidence.clear()
        self.state.interrupted_tool_calls.clear()
        self.state.completed_tool_call_ids.clear()
        self._inflight.clear()
        self.state.messages.append({"role": "user", "content": objective})

    def _assistant_response(self, payload: Mapping[str, Any]) -> None:
        calls = payload.get("tool_calls") or []
        self.state.messages.append(
            {
                "role": "assistant",
                "content": payload.get("content") or "",
                **({"tool_calls": copy.deepcopy(calls)} if calls else {}),
            }
        )
        usage = payload.get("usage") or {}
        for key, value in usage.items():
            if isinstance(value, int):
                self.state.token_usage[key] = self.state.token_usage.get(key, 0) + value

    def _approval_requested(self, payload: Mapping[str, Any]) -> None:
        call = {
            "id": str(payload.get("id") or ""),
            "name": str(payload.get("name") or ""),
            "arguments": copy.deepcopy(payload.get("arguments") or {}),
        }
        pending = {
            "call": call,
            "remaining": copy.deepcopy(list(payload.get("remaining") or [])),
            "reason": str(payload.get("reason") or ""),
        }
        if _contains_redacted(call) or _contains_redacted(payload.get("remaining") or []):
            pending["redacted"] = True
        self.state.pending_approval = pending
        self.state.status = AgentStatus.WAITING_APPROVAL

    def _approval_result(self, payload: Mapping[str, Any]) -> None:
        call_id = str(payload.get("tool_call_id") or payload.get("id") or "")
        pending = self.state.pending_approval
        if pending is not None and (
            not call_id or str((pending.get("call") or {}).get("id")) == call_id
        ):
            self.state.pending_approval = None

    def _tool_started(self, payload: Mapping[str, Any], sequence: int) -> None:
        call_id = str(payload.get("id") or "")
        if not call_id or call_id in self._inflight:
            return
        call = {
            "id": call_id,
            "name": str(payload.get("name") or "unknown"),
            "arguments": copy.deepcopy(payload.get("arguments") or {}),
            "started_sequence": sequence,
        }
        self._inflight[call_id] = call
        self.state.status = AgentStatus.EXECUTING_TOOL

    def _tool_result(self, payload: Mapping[str, Any], sequence: int) -> None:
        call_id = str(payload.get("id") or "")
        if call_id and call_id in self.state.completed_tool_call_ids:
            return
        if call_id:
            self.state.completed_tool_call_ids.add(call_id)
            self._inflight.pop(call_id, None)
        result = payload.get("result") or {}
        name = str(payload.get("name") or "unknown")
        self.state.status = AgentStatus.OBSERVING
        self.state.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id or "recovered",
                "name": name,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )
        stats = self.state.tool_stats.setdefault(
            name, {"calls": 0, "successes": 0, "failures": 0, "duration_ms": 0}
        )
        stats["calls"] += 1
        ok = bool(result.get("ok"))
        stats["successes" if ok else "failures"] += 1
        duration = (result.get("metadata") or {}).get("duration_ms", 0)
        if isinstance(duration, int):
            stats["duration_ms"] += duration
        metadata = result.get("metadata") or {}
        mutated_files = [str(path) for path in metadata.get("mutated_files", [])]
        if ok and mutated_files:
            self.state.changed_files.update(mutated_files)
            self.state.last_mutation_sequence = sequence
        self.state.recent_actions.append(
            {
                "signature": json.dumps(
                    {"name": name, "arguments": payload.get("arguments") or {}},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "result_code": result.get("code"),
            }
        )
        self.state.recent_actions[:] = self.state.recent_actions[-20:]

    def _verification_recorded(self, payload: Mapping[str, Any], sequence: int) -> None:
        evidence = payload.get("evidence")
        if not isinstance(evidence, Mapping):
            return
        evidence_copy = copy.deepcopy(dict(evidence))
        self.state.verification_evidence.append(evidence_copy)
        if evidence_copy.get("accepted"):
            self.state.last_successful_verification_sequence = sequence

    def _turn_finished(self, payload: Mapping[str, Any]) -> None:
        self._mark_inflight_interrupted()
        try:
            self.state.status = AgentStatus(payload.get("status", self.state.status))
        except ValueError:
            pass
        token_usage = payload.get("token_usage")
        if isinstance(token_usage, Mapping):
            self.state.token_usage = {
                str(key): int(value) if isinstance(value, int) else value
                for key, value in token_usage.items()
            }
        tool_stats = payload.get("tool_stats")
        if isinstance(tool_stats, Mapping):
            self.state.tool_stats = copy.deepcopy(dict(tool_stats))

    def _mark_inflight_interrupted(self) -> None:
        for call in tuple(self._inflight.values()):
            call_id = str(call.get("id") or "")
            if any(item.get("id") == call_id for item in self.state.interrupted_tool_calls):
                continue
            self.state.interrupted_tool_calls.append(
                {
                    **copy.deepcopy(call),
                    "code": "INTERRUPTED_UNKNOWN",
                    "message": "Tool started but no durable tool_result was recorded",
                }
            )
            self.state.recovery_warnings.append(
                {
                    "code": "INTERRUPTED_UNKNOWN",
                    "tool_call_id": call_id,
                    "message": "副作用未确认，恢复时不会自动重放该工具调用",
                }
            )
        self._inflight.clear()


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _contains_redacted(value: Any) -> bool:
    if isinstance(value, str):
        return value == "[REDACTED]"
    if isinstance(value, Mapping):
        return any(_contains_redacted(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_redacted(item) for item in value)
    return False
