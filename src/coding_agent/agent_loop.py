from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .checkpoints import CheckpointManager
from .budget import BudgetExceeded, RunBudget
from .cancellation import CancellationToken, RunCancelled
from .context import ContextManager
from .events import AgentEvent, EventBus
from .model import ModelClient, ModelError, ModelResponse, ToolCall
from .permissions import PermissionDecision, PermissionPolicy, PermissionResult
from .session import AgentState, AgentStatus
from .stuck_detector import StuckDetector
from .tool_executor import ToolExecutor
from .tools.base import ToolError, ToolResult, ToolRisk
from .tools.registry import ToolRegistry
from .verifier import CompletionStatus, Verifier
from .verification_evidence import build_verification_evidence


ApprovalHandler = Callable[[ToolCall, PermissionResult], Awaitable[bool]]
TurnSummarizer = Callable[[AgentState, AgentStatus, str], dict[str, Any]]
CANCEL_RESULT_GRACE_SECONDS = 12


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: AgentStatus
    message: str
    state: AgentState


class AgentRunner:
    def __init__(
        self,
        *,
        model_client: ModelClient,
        context_manager: ContextManager,
        registry: ToolRegistry,
        tool_executor: ToolExecutor,
        permission_policy: PermissionPolicy,
        event_bus: EventBus,
        verifier: Verifier | None = None,
        stuck_detector: StuckDetector | None = None,
        approval_handler: ApprovalHandler | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        turn_summarizer: TurnSummarizer | None = None,
        cancellation: CancellationToken | None = None,
        run_budget: RunBudget | None = None,
    ) -> None:
        self.model_client = model_client
        self.context_manager = context_manager
        self.registry = registry
        self.tool_executor = tool_executor
        self.permission_policy = permission_policy
        self.event_bus = event_bus
        self.verifier = verifier or Verifier()
        self.stuck_detector = stuck_detector or StuckDetector()
        self.approval_handler = approval_handler
        self.checkpoint_manager = checkpoint_manager
        self.turn_summarizer = turn_summarizer
        self.cancellation = cancellation or CancellationToken()
        self.run_budget = run_budget or RunBudget()

    async def run_turn(
        self, state: AgentState, user_message: str | None = None
    ) -> AgentRunResult:
        if user_message is not None:
            if state.step or state.status not in {
                AgentStatus.READY,
                AgentStatus.COMPLETED,
                AgentStatus.PARTIAL,
                AgentStatus.FAILED,
                AgentStatus.CANCELLED,
            }:
                raise RuntimeError("Cannot start a new turn while another turn is active")
            if state.status is not AgentStatus.READY:
                state.begin_new_turn()
            await self._emit(state, "turn_started", {"message": user_message})

        if user_message is not None or not self.run_budget.active:
            await self._start_run_controls(state)

        return await self._run_safely(state)

    async def resume_approval(
        self, state: AgentState, *, approved: bool
    ) -> AgentRunResult:
        pending = state.pending_approval
        if state.status is not AgentStatus.WAITING_APPROVAL or pending is None:
            raise RuntimeError("There is no pending approval")
        if not self.run_budget.active:
            await self._start_run_controls(state)

        return await self._run_safely(
            state, self._resume_approval(state, pending, approved=approved)
        )

    async def _resume_approval(
        self,
        state: AgentState,
        pending: dict[str, Any],
        *,
        approved: bool,
    ) -> AgentRunResult:

        if pending.get("redacted"):
            call_id = str((pending.get("call") or {}).get("id") or "unknown")
            await self._emit(
                state,
                "recovery_reapproval_required",
                {
                    "tool_call_id": call_id,
                    "reason": "Approval arguments were redacted during persistence; resubmit the operation",
                },
            )
            return AgentRunResult(
                AgentStatus.WAITING_APPROVAL,
                "RECOVERY_REAPPROVAL_REQUIRED: resubmit the operation because persisted approval data was redacted",
                state,
            )

        call = ToolCall(
            id=pending["call"]["id"],
            name=pending["call"]["name"],
            arguments=pending["call"]["arguments"],
        )
        await self._emit(
            state,
            "approval_result",
            {"tool_call_id": call.id, "approved": approved},
        )
        if approved:
            await self._execute_and_observe(state, call)
        else:
            await self._record_tool_result(
                state,
                call,
                ToolResult.failure(
                    "USER_REJECTED",
                    "The user rejected this operation; try a safer alternative",
                ),
            )

        remaining = [
            ToolCall(item["id"], item["name"], item["arguments"])
            for item in pending.get("remaining", [])
        ]
        if remaining:
            outcome = await self._handle_tool_calls(state, remaining)
            if outcome is not None:
                return outcome
        return await self._run_loop(state)

    async def _start_run_controls(self, state: AgentState) -> None:
        self.cancellation.reset()
        state.cancel_requested = False
        self.run_budget.start(max_steps=state.max_steps)
        await self._emit(
            state,
            "run_budget_started",
            {"budget": state.run_budget},
        )

    async def _run_safely(
        self,
        state: AgentState,
        operation: Awaitable[AgentRunResult] | None = None,
    ) -> AgentRunResult:
        try:
            return await (operation if operation is not None else self._run_loop(state))
        except RunCancelled as exc:
            await self._emit(
                state,
                "run_cancelled",
                {"reason": exc.reason, "budget": self._update_budget_state(state)},
            )
            return await self._finish(
                state,
                AgentStatus.CANCELLED,
                f"RUN_CANCELLED: {exc.reason}",
            )
        except BudgetExceeded as exc:
            await self._emit(
                state,
                "run_budget_exhausted",
                {
                    "code": exc.code,
                    "message": exc.message,
                    "budget": self._update_budget_state(state),
                },
            )
            return await self._finish(
                state,
                AgentStatus.PARTIAL,
                f"{exc.code}: {exc.message}",
            )
        except asyncio.CancelledError:
            self.cancellation.cancel("task_cancelled")
            current = asyncio.current_task()
            while current is not None and current.cancelling():
                current.uncancel()
            await self._emit(
                state,
                "run_cancelled",
                {"reason": "task_cancelled", "budget": self._update_budget_state(state)},
            )
            return await self._finish(
                state,
                AgentStatus.CANCELLED,
                "RUN_CANCELLED: task_cancelled",
            )

    async def _run_loop(self, state: AgentState) -> AgentRunResult:
        while True:
            if state.cancel_requested and not self.cancellation.requested:
                self.cancellation.cancel("state_cancel_requested")
            self.cancellation.raise_if_cancelled()
            self.run_budget.check_time()
            self.run_budget.check_step(state.step + 1)

            schemas = self._allowed_tool_schemas(state.mode)
            context = self.context_manager.build(state, schemas)
            self.run_budget.check_time()
            next_step = state.step + 1
            await self._emit(state, "step_started", {"step": next_step})
            if context.truncated:
                await self._emit(state, "context_compacted", {
                    "estimated_chars": context.estimated_chars,
                    "summary": state.context_summary,
                })

            await self._emit(state, "model_started", {"step": state.step})
            try:
                stream_method = getattr(self.model_client, "complete_stream", None)
                if callable(stream_method):
                    async def on_delta(delta: str) -> None:
                        await self._emit(state, "assistant_delta", {"content": delta})
                    response = await self._await_controlled(
                        stream_method(messages=context.messages, tools=context.allowed_tools, reasoning_effort=state.reasoning_mode, on_delta=on_delta)
                    )
                else:
                    response = await self._await_controlled(
                        self.model_client.complete(messages=context.messages, tools=context.allowed_tools, reasoning_effort=state.reasoning_mode)
                    )
            except ModelError as exc:
                return await self._finish(state, AgentStatus.FAILED, str(exc))

            self.run_budget.record_usage(response.usage)
            await self._emit(
                state,
                "run_budget_updated",
                {"budget": self._update_budget_state(state)},
            )
            await self._emit(
                state,
                "assistant_response",
                {
                    "content": response.content,
                    "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                    "usage": response.usage,
                },
            )
            self.run_budget.check_tokens()

            if response.tool_calls:
                outcome = await self._handle_tool_calls(state, response.tool_calls)
                if outcome is not None:
                    return outcome
                if self.stuck_detector.is_stuck(state.recent_actions):
                    return await self._finish(
                        state,
                        AgentStatus.FAILED,
                        "AGENT_STUCK: repeated identical tool action and result",
                    )
                continue

            decision = self.verifier.evaluate(state, response)
            await self._emit(
                state,
                "completion_checked",
                {"status": decision.status, "reason": decision.reason},
            )
            if decision.status is CompletionStatus.COMPLETED:
                return await self._finish(
                    state, AgentStatus.COMPLETED, response.content or decision.reason
                )
            if decision.status is CompletionStatus.PARTIAL:
                return await self._finish(state, AgentStatus.PARTIAL, decision.reason)

            await self._emit(
                state, "verification_required", {"reason": decision.reason}
            )

    async def _handle_tool_calls(
        self, state: AgentState, calls: list[ToolCall]
    ) -> AgentRunResult | None:
        for index, call in enumerate(calls):
            self.cancellation.raise_if_cancelled()
            self.run_budget.check_time()
            try:
                spec = self.registry.get(call.name)
                spec.validate(call.arguments)
            except ToolError as exc:
                await self._record_tool_result(
                    state, call, ToolResult.failure(exc.code, exc.message, data=exc.data)
                )
                continue

            permission = self.permission_policy.evaluate(
                mode=state.mode, spec=spec, arguments=call.arguments
            )
            await self._emit(
                state,
                "tool_requested",
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "risk": spec.risk,
                    "permission": permission.decision,
                    "reason": permission.reason,
                    "capabilities": permission.capabilities,
                },
            )

            if permission.decision is PermissionDecision.DENY:
                await self._record_tool_result(
                    state,
                    call,
                    ToolResult.failure("PERMISSION_DENIED", permission.reason),
                )
                continue

            if permission.decision is PermissionDecision.ASK:
                await self._emit(
                    state,
                    "approval_requested",
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                        "reason": permission.reason,
                        "remaining": [
                            _serialize_call(item) for item in calls[index + 1 :]
                        ],
                    },
                )
                if self.approval_handler is None:
                    return AgentRunResult(
                        AgentStatus.WAITING_APPROVAL,
                        f"Approval required for {call.name}",
                        state,
                    )

                approved = await self._await_controlled(
                    self.approval_handler(call, permission)
                )
                await self._emit(
                    state,
                    "approval_result",
                    {"tool_call_id": call.id, "approved": approved},
                )
                if not approved:
                    await self._record_tool_result(
                        state,
                        call,
                        ToolResult.failure(
                            "USER_REJECTED",
                            "The user rejected this operation; try a safer alternative",
                        ),
                    )
                    continue

            await self._execute_and_observe(state, call)
        return None

    async def _execute_and_observe(self, state: AgentState, call: ToolCall) -> None:
        spec = self.registry.get(call.name)
        if spec.risk is ToolRisk.WRITE and self.checkpoint_manager is not None:
            path = call.arguments.get("path")
            if isinstance(path, str):
                try:
                    capture = self.checkpoint_manager.capture(state.turn_id, path)
                except ToolError as exc:
                    await self._record_tool_result(
                        state,
                        call,
                        ToolResult.failure(exc.code, exc.message, data=exc.data),
                    )
                    return
                if capture.created:
                    await self._emit(
                        state,
                        "checkpoint_created",
                        {
                            "turn_id": state.turn_id,
                            "path": capture.path,
                            "existed": capture.existed,
                        },
                    )
        started_event = await self._emit(
            state,
            "tool_started",
            {"id": call.id, "name": call.name, "arguments": call.arguments},
        )
        result = await self._await_controlled(
            self.tool_executor.execute(call.name, call.arguments),
            allow_cancel_result=True,
        )
        await self._record_tool_result(
            state, call, result, started_sequence=started_event.sequence
        )

    async def _await_controlled(
        self,
        operation: Awaitable[Any],
        *,
        allow_cancel_result: bool = False,
    ) -> Any:
        operation_task = asyncio.ensure_future(operation)
        cancel_task = asyncio.create_task(self.cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {operation_task, cancel_task},
                timeout=self.run_budget.remaining_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self.cancellation.requested:
                if allow_cancel_result:
                    try:
                        return await asyncio.wait_for(
                            asyncio.shield(operation_task),
                            timeout=CANCEL_RESULT_GRACE_SECONDS,
                        )
                    except TimeoutError:
                        pass
                operation_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await operation_task
                raise RunCancelled(self.cancellation.reason)
            if operation_task in done:
                return await operation_task
            operation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation_task
            self.run_budget.check_time()
            raise BudgetExceeded(
                "TIME_BUDGET_EXHAUSTED",
                "Run wall-time budget expired while an operation was active",
            )
        finally:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

    def _update_budget_state(self, state: AgentState) -> dict[str, Any]:
        state.run_budget = self.run_budget.snapshot()
        return state.run_budget

    async def _record_tool_result(
        self,
        state: AgentState,
        call: ToolCall,
        result: ToolResult,
        *,
        started_sequence: int = 0,
    ) -> None:
        event = await self._emit(
            state,
            "tool_result",
            {
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "result": result.to_dict(),
            },
        )

        mutated_files = result.metadata.get("mutated_files", []) if result.ok else []
        if mutated_files:
            if self.checkpoint_manager is not None:
                for path in map(str, mutated_files):
                    try:
                        self.checkpoint_manager.record_mutation(
                            state.turn_id,
                            path,
                            sequence=event.sequence,
                            tool=call.name,
                            expected_sha256=(
                                str(result.data["sha256"])
                                if result.data.get("sha256")
                                else None
                            ),
                        )
                    except ToolError as exc:
                        await self._emit(
                            state,
                            "checkpoint_tracking_failed",
                            {
                                "path": path,
                                "code": exc.code,
                                "message": exc.message,
                            },
                        )
        if call.name == "run_command" and result.metadata.get("purpose") == "verify":
            evidence = build_verification_evidence(
                command=str(call.arguments.get("command") or ""),
                purpose=str(result.metadata.get("purpose") or call.arguments.get("purpose") or "other"),
                result=result.to_dict(),
                objective=state.current_objective,
                changed_files=state.changed_files,
                started_sequence=started_sequence,
                finished_sequence=event.sequence,
            )
            evidence_payload = evidence.to_dict()
            await self._emit(
                state, "verification_recorded", {"evidence": evidence_payload}
            )
            if not evidence.accepted:
                await self._emit(state, "repair_attempt", {
                    "attempt": state.repair_attempts + 1,
                    "max_attempts": state.max_repair_attempts,
                    "reason": evidence.reason,
                })
        if result.ok and result.metadata.get("plan_updated"):
            await self._emit(state, "plan_updated", {
                "plan": state.plan, "reason": result.data.get("reason", "")
            })

    def _allowed_tool_schemas(self, mode: str) -> list[dict[str, Any]]:
        if mode == "act":
            return self.registry.schemas()
        read_names = {
            name
            for name in self.registry.names()
            if self.registry.get(name).risk is ToolRisk.READ
        }
        return self.registry.schemas(read_names)

    async def _finish(
        self, state: AgentState, status: AgentStatus, message: str
    ) -> AgentRunResult:
        await self._emit(
            state,
            "turn_finished",
            {
                "status": status,
                "message": message,
                "changed_files": sorted(state.changed_files),
                "token_usage": state.token_usage,
                "tool_stats": state.tool_stats,
                "budget": self._update_budget_state(state),
                "evidence": {
                    "changed_files": sorted(state.changed_files),
                    "plan": state.plan,
                    "verification_fresh": state.verification_is_fresh,
                    "successful_verification_sequence": state.last_successful_verification_sequence,
                    "verification_evidence": state.verification_evidence,
                },
            },
        )
        if self.turn_summarizer is not None:
            try:
                summary = self.turn_summarizer(state, status, message)
                await self._emit(state, "session_summarized", {"summary": summary})
            except Exception as exc:  # Summary persistence must never fail the completed turn.
                await self._emit(
                    state,
                    "session_summary_failed",
                    {"error": str(exc), "raw_events_preserved": True},
                )
        return AgentRunResult(status, message, state)

    async def _emit(
        self, state: AgentState, event_type: str, payload: dict[str, Any]
    ) -> AgentEvent:
        event = await self.event_bus.publish(
            AgentEvent(
                type=event_type,
                session_id=state.session_id,
                turn_id=state.turn_id,
                payload=payload,
            )
        )
        state.apply_event(event.to_dict())
        return event
def _serialize_call(call: ToolCall) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}
