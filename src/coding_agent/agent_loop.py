from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from .checkpoints import CheckpointManager
from .budget import BudgetExceeded, RunBudget
from .cancellation import CancellationToken, RunCancelled
from .context import ContextManager
from .events import AgentEvent, EventBus
from .model import ModelClient, ModelError, ModelResponse, ToolCall
from .permissions import PermissionDecision, PermissionPolicy, PermissionResult
from .profiles import get_profile, resolve_profile
from .session import AgentState, AgentStatus
from .stuck_detector import StuckDetector
from .tool_executor import ToolExecutor
from .tools.base import ToolError, ToolResult, ToolRisk
from .tools.registry import ToolRegistry
from .tools.workspace import Workspace
from .verifier import CompletionStatus, Verifier
from .verification_evidence import build_verification_evidence


ApprovalHandler = Callable[[ToolCall, PermissionResult], Awaitable[bool]]
TurnSummarizer = Callable[[AgentState, AgentStatus, str], dict[str, Any]]
CANCEL_RESULT_GRACE_SECONDS = 12
OPERATION_CANCEL_GRACE_SECONDS = 1.0
CONTROL_PROGRESS_INTERVAL_SECONDS = 10.0
OUTPUT_DELTA_INTERVAL_SECONDS = 0.05
OUTPUT_DELTA_MAX_BYTES = 16_384


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
        project_verification_commands: Collection[str] | None = None,
        workspace: Workspace | None = None,
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
        self.project_verification_commands = tuple(project_verification_commands or ())
        self.workspace = workspace
        self._stuck_recovery_attempts = 0
        self._stuck_signature: tuple[str, str, str] | None = None

    async def run_turn(
        self, state: AgentState, user_message: str | None = None
    ) -> AgentRunResult:
        if user_message is not None:
            if state.status not in {
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
            profile = resolve_profile(state.requested_task_profile, user_message)
            await self._emit(
                state,
                "task_profile_selected",
                {
                    "requested": state.requested_task_profile,
                    "profile": profile.name,
                    "reason": "explicit" if state.requested_task_profile != "auto" else "deterministic_classifier",
                },
            )

        if user_message is not None or not self.run_budget.active:
            if user_message is not None:
                self._stuck_recovery_attempts = 0
                self._stuck_signature = None
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

    async def retry_interrupted(
        self, state: AgentState, *, tool_call_id: str
    ) -> AgentRunResult:
        """Manually retry one interrupted call after an explicit user action.

        Recovery never replays calls automatically.  The web UI invokes this
        method only after the user selected a specific call, so the normal
        permission checks are applied again by the execution path.
        """
        interrupted = next(
            (
                item
                for item in state.interrupted_tool_calls
                if str(item.get("id") or "") == tool_call_id
            ),
            None,
        )
        if interrupted is None:
            raise RuntimeError("Interrupted tool call not found")
        call = ToolCall(
            id=str(interrupted.get("id") or ""),
            name=str(interrupted.get("name") or ""),
            arguments=dict(interrupted.get("arguments") or {}),
        )
        spec = self.registry.get(call.name)
        spec.validate(call.arguments)
        permission = self.permission_policy.evaluate(
            mode=state.mode, spec=spec, arguments=call.arguments
        )
        if permission.decision is PermissionDecision.DENY:
            raise ToolError("PERMISSION_DENIED", permission.reason)
        if _contains_redacted(call.arguments):
            raise RuntimeError(
                "Persisted tool arguments were redacted; resubmit the operation"
            )
        await self._emit(
            state,
            "recovery_retry_requested",
            {"tool_call_id": call.id, "name": call.name},
        )
        if not self.run_budget.active:
            await self._start_run_controls(state)
        return await self._run_safely(
            state, self._retry_interrupted_call(state, call)
        )

    async def abandon_interrupted(
        self, state: AgentState, *, tool_call_id: str
    ) -> AgentRunResult:
        """Acknowledge an interrupted call without executing it again."""
        interrupted = next(
            (
                item
                for item in state.interrupted_tool_calls
                if str(item.get("id") or "") == tool_call_id
            ),
            None,
        )
        if interrupted is None:
            raise RuntimeError("Interrupted tool call not found")
        await self._emit(
            state,
            "recovery_abandoned",
            {
                "tool_call_id": tool_call_id,
                "name": str(interrupted.get("name") or "unknown"),
            },
        )
        return AgentRunResult(
            AgentStatus.PARTIAL,
            "Interrupted tool call was abandoned; no operation was replayed",
            state,
        )

    async def _retry_interrupted_call(
        self, state: AgentState, call: ToolCall
    ) -> AgentRunResult:
        await self._execute_and_observe(state, call)
        return await self._run_loop(state)

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
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            await self._emit(
                state,
                "run_failed",
                {"code": "UNEXPECTED_AGENT_ERROR", "message": message},
            )
            return await self._finish(
                state,
                AgentStatus.FAILED,
                f"UNEXPECTED_AGENT_ERROR: {message}",
            )

    async def _run_loop(self, state: AgentState) -> AgentRunResult:
        while True:
            if state.cancel_requested and not self.cancellation.requested:
                self.cancellation.cancel("state_cancel_requested")
            self.cancellation.raise_if_cancelled()
            self.run_budget.check_time()
            self.run_budget.check_step(state.step + 1)

            schemas = self._allowed_tool_schemas(state.mode, state.task_profile)
            context_span_started = perf_counter()
            context_span_id = await self._start_span(
                state, "context_build", {"step": state.step + 1}
            )
            try:
                context = self.context_manager.build(state, schemas)
            finally:
                await self._finish_span(
                    state, context_span_id, "context_build", context_span_started
                )
            self.run_budget.check_time()
            next_step = state.step + 1
            await self._emit(state, "step_started", {"step": next_step})
            await self._emit(
                state,
                "context_built",
                {
                    "estimated_chars": context.estimated_chars,
                    "task_profile": state.task_profile,
                    "rule_candidates": context.rule_candidates,
                    "rule_chars": context.rule_chars,
                    "rule_truncated": context.rule_truncated,
                    "rule_sources": context.rule_sources,
                    "rule_conflicts": context.rule_conflicts,
                    "repo_map": context.repo_map,
                    "summary_meta": context.context_summary_meta,
                },
            )
            if context.truncated:
                await self._emit(state, "context_compacted", {
                    "estimated_chars": context.estimated_chars,
                    "summary": state.context_summary,
                    "summary_meta": state.context_summary_meta,
                })

            await self._emit(state, "model_started", {"step": state.step})
            model_span_started = perf_counter()
            model_span_id = await self._start_span(
                state, "model_request", {"step": state.step}
            )
            try:
                try:
                    async def on_model_wait(elapsed_seconds: float) -> None:
                        await self._emit(
                            state,
                            "model_progress",
                            {
                                "step": state.step,
                                "elapsed_seconds": round(elapsed_seconds, 1),
                                "request_timeout_seconds": getattr(
                                    self.model_client, "timeout", None
                                ),
                            },
                        )

                    stream_method = getattr(self.model_client, "complete_stream", None)
                    if callable(stream_method):
                        async def on_delta(delta: str) -> None:
                            await self._emit(state, "assistant_delta", {"content": delta})
                        response = await self._await_controlled(
                            stream_method(messages=context.messages, tools=context.allowed_tools, reasoning_effort=state.reasoning_mode, on_delta=on_delta),
                            progress_callback=on_model_wait,
                        )
                    else:
                        response = await self._await_controlled(
                            self.model_client.complete(messages=context.messages, tools=context.allowed_tools, reasoning_effort=state.reasoning_mode),
                            progress_callback=on_model_wait,
                        )
                finally:
                    await self._finish_span(
                        state, model_span_id, "model_request", model_span_started
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
            self._attach_private_model_state(state, response)
            self.run_budget.check_tokens()

            if response.tool_calls:
                outcome = await self._handle_tool_calls(state, response.tool_calls)
                if outcome is not None:
                    return outcome
                if self.stuck_detector.is_stuck(state.recent_actions):
                    stuck_signature = self._stuck_fingerprint(state.recent_actions)
                    if stuck_signature != self._stuck_signature:
                        self._stuck_signature = stuck_signature
                        self._stuck_recovery_attempts = 0
                    recovery_hint = self.stuck_detector.recovery_hint(
                        state.recent_actions
                    )
                    if recovery_hint and self._stuck_recovery_attempts < 1:
                        self._stuck_recovery_attempts += 1
                        await self._emit(
                            state,
                            "stuck_recovery",
                            {
                                "attempt": self._stuck_recovery_attempts,
                                "message": recovery_hint,
                            },
                        )
                        state.messages.append(
                            {"role": "system", "content": recovery_hint}
                        )
                        continue
                    if self.stuck_detector.is_successful_write_loop(
                        state.recent_actions
                    ):
                        latest = state.recent_actions[-1]
                        await self._emit(
                            state,
                            "stuck_terminal",
                            {
                                "status": AgentStatus.PARTIAL,
                                "message": (
                                    "Repeated writes were stopped; "
                                    "the latest file changes were preserved."
                                ),
                                "action": latest.get("signature"),
                            },
                        )
                        return await self._finish(
                            state,
                            AgentStatus.PARTIAL,
                            "Repeated writes were stopped; the latest file changes were preserved. Verify the result before continuing.",
                        )
                    return await self._finish(
                        state,
                        AgentStatus.FAILED,
                        "AGENT_STUCK: repeated identical tool action and result",
                    )
                else:
                    # A different action breaks the current repetition streak;
                    # a later, independent loop gets its own bounded recovery.
                    self._stuck_signature = None
                    self._stuck_recovery_attempts = 0
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

    @staticmethod
    def _attach_private_model_state(
        state: AgentState, response: ModelResponse
    ) -> None:
        """Keep provider protocol state in memory without persisting model reasoning."""
        if not response.reasoning_content or not response.tool_calls:
            return
        for message in reversed(state.messages):
            if message.get("role") == "assistant":
                message.clear()
                message.update(response.to_assistant_message())
                return

    async def _handle_tool_calls(
        self, state: AgentState, calls: list[ToolCall]
    ) -> AgentRunResult | None:
        parallel_specs = self._parallel_read_specs(state, calls)
        if parallel_specs is not None:
            await self._handle_parallel_reads(state, parallel_specs)
            return None
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

                approval_span_started = perf_counter()
                approval_span_id = await self._start_span(
                    state, "approval_wait", {"tool_call_id": call.id}
                )
                try:
                    approved = await self._await_controlled(
                        self.approval_handler(call, permission)
                    )
                finally:
                    await self._finish_span(
                        state,
                        approval_span_id,
                        "approval_wait",
                        approval_span_started,
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

            if self._is_duplicate_successful_write(state, call):
                await self._record_tool_result(
                    state,
                    call,
                    ToolResult.failure(
                        "DUPLICATE_TOOL_CALL",
                        "An identical write already succeeded. Inspect the current file and continue without repeating it.",
                    ),
                )
                if self._duplicate_write_is_satisfied(call):
                    await self._emit(
                        state,
                        "duplicate_write_satisfied",
                        {
                            "id": call.id,
                            "name": call.name,
                            "path": call.arguments.get("path"),
                            "message": "The requested write is already present; duplicate execution was blocked.",
                        },
                    )
                    return await self._finish(
                        state,
                        AgentStatus.PARTIAL,
                        "The requested write is already applied; the duplicate operation was blocked. Verification is still required.",
                    )
                continue

            await self._execute_and_observe(state, call)
        return None

    @staticmethod
    def _stuck_fingerprint(
        recent_actions: list[dict[str, Any]],
    ) -> tuple[str, str, str] | None:
        if not recent_actions:
            return None
        latest = recent_actions[-1]
        return (
            str(latest.get("signature") or ""),
            str(latest.get("result_code") or ""),
            str(latest.get("result_fingerprint") or ""),
        )

    @staticmethod
    def _is_duplicate_successful_write(
        state: AgentState, call: ToolCall
    ) -> bool:
        if call.name not in {"apply_patch", "write_file"}:
            return False
        for action in reversed(state.recent_actions):
            if str(action.get("result_code") or "") != "OK":
                continue
            try:
                signature = json.loads(str(action.get("signature") or "{}"))
            except (TypeError, ValueError):
                continue
            if signature.get("name") == call.name and signature.get("arguments") == call.arguments:
                return True
        return False

    def _duplicate_write_is_satisfied(self, call: ToolCall) -> bool:
        """Check whether a blocked duplicate write already produced its target state."""
        workspace = self.workspace
        if workspace is None and self.checkpoint_manager is not None:
            workspace = self.checkpoint_manager.workspace
        if workspace is None:
            return False
        raw_path = call.arguments.get("path")
        if not isinstance(raw_path, str):
            return False
        try:
            path = workspace.resolve(raw_path, must_exist=True)
            current = path.read_text(encoding="utf-8")
        except (ToolError, OSError, UnicodeDecodeError):
            return False
        if call.name == "write_file":
            content = call.arguments.get("content")
            return isinstance(content, str) and current == content
        if call.name == "apply_patch":
            old_text = call.arguments.get("old_text")
            new_text = call.arguments.get("new_text")
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                return False
            # A successful replacement normally removes the unique old span.
            # Require the new span and no remaining old span to avoid treating
            # an unrelated external edit as proof that the patch was applied.
            return new_text in current and old_text not in current
        return False

    def _parallel_read_specs(
        self, state: AgentState, calls: list[ToolCall]
    ) -> list[tuple[ToolCall, Any]] | None:
        """Return an eligible read batch, or None to keep strict sequencing."""
        if len(calls) < 2:
            return None
        hooks = self.tool_executor.hooks
        if hooks.pre or hooks.post:
            return None
        paths: set[str] = set()
        prepared: list[tuple[ToolCall, Any]] = []
        for call in calls:
            try:
                spec = self.registry.get(call.name)
                spec.validate(call.arguments)
            except ToolError:
                return None
            if spec.risk is not ToolRisk.READ:
                return None
            path = call.arguments.get("path")
            if isinstance(path, str):
                normalized = path.replace("\\", "/").strip().lower()
                if normalized in paths:
                    return None
                paths.add(normalized)
            permission = self.permission_policy.evaluate(
                mode=state.mode, spec=spec, arguments=call.arguments
            )
            if permission.decision is not PermissionDecision.ALLOW:
                return None
            prepared.append((call, spec))
        return prepared

    async def _handle_parallel_reads(
        self, state: AgentState, prepared: list[tuple[ToolCall, Any]]
    ) -> None:
        """Execute independent reads concurrently, recording results in call order."""
        for call, spec in prepared:
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
                    "execution": "parallel_read",
                },
            )
        started_sequences: dict[str, int] = {}
        for call, _ in prepared:
            started = await self._emit(
                state,
                "tool_started",
                {"id": call.id, "name": call.name, "arguments": call.arguments},
            )
            started_sequences[call.id] = started.sequence

        async def execute_one(call: ToolCall) -> ToolResult:
            return await self._await_controlled(
                self.tool_executor.execute(call.name, call.arguments),
                allow_cancel_result=True,
            )

        tasks = [asyncio.create_task(execute_one(call)) for call, _ in prepared]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for (call, _), result in zip(prepared, results):
            await self._record_tool_result(
                state, call, result, started_sequence=started_sequences.get(call.id, 0)
            )

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
        output_index = 0
        output_pending: dict[str, list[str]] = {"stdout": [], "stderr": []}
        output_pending_bytes = 0
        output_last_flush = asyncio.get_running_loop().time()
        output_lock = asyncio.Lock()

        async def flush_output(*, force: bool = False) -> None:
            nonlocal output_index, output_pending_bytes, output_last_flush
            async with output_lock:
                now = asyncio.get_running_loop().time()
                if not force and (
                    now - output_last_flush < OUTPUT_DELTA_INTERVAL_SECONDS
                    and output_pending_bytes < OUTPUT_DELTA_MAX_BYTES
                ):
                    return
                batches = {
                    stream: "".join(chunks)
                    for stream, chunks in output_pending.items()
                    if chunks
                }
                output_pending["stdout"].clear()
                output_pending["stderr"].clear()
                output_pending_bytes = 0
                output_last_flush = now
                for stream, batch in batches.items():
                    output_index += 1
                    await self._emit(
                        state,
                        "tool_output_delta",
                        {
                            "id": call.id,
                            "name": call.name,
                            "stream": stream,
                            "content": batch,
                            "index": output_index,
                            "coalesced": True,
                        },
                    )

        async def on_output(stream: str, content: str) -> None:
            nonlocal output_pending_bytes
            async with output_lock:
                output_pending.setdefault(stream, []).append(content)
                output_pending_bytes += len(content.encode("utf-8", errors="replace"))
                should_flush = output_pending_bytes >= OUTPUT_DELTA_MAX_BYTES
            if should_flush:
                await flush_output()

        try:
            result = await self._await_controlled(
                self.tool_executor.execute(
                    call.name,
                    call.arguments,
                    output_callback=on_output if call.name == "run_command" else None,
                ),
                allow_cancel_result=True,
            )
        finally:
            if call.name == "run_command":
                await flush_output(force=True)
        await self._record_tool_result(
            state, call, result, started_sequence=started_event.sequence
        )

    async def _await_controlled(
        self,
        operation: Awaitable[Any],
        *,
        allow_cancel_result: bool = False,
        progress_callback: Callable[[float], Awaitable[None]] | None = None,
    ) -> Any:
        operation_task = asyncio.ensure_future(operation)
        cancel_task = asyncio.create_task(self.cancellation.wait())
        started = asyncio.get_running_loop().time()
        try:
            while True:
                remaining = self.run_budget.remaining_seconds
                wait_timeout = remaining
                if progress_callback is not None:
                    wait_timeout = CONTROL_PROGRESS_INTERVAL_SECONDS
                    if remaining is not None:
                        wait_timeout = min(wait_timeout, remaining)
                done, _ = await asyncio.wait(
                    {operation_task, cancel_task},
                    timeout=wait_timeout,
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
                    await _cancel_operation_task(operation_task)
                    raise RunCancelled(self.cancellation.reason)
                if operation_task in done:
                    return await operation_task
                if remaining is not None and remaining <= (wait_timeout or 0):
                    await _cancel_operation_task(operation_task)
                    self.run_budget.check_time()
                    raise BudgetExceeded(
                        "TIME_BUDGET_EXHAUSTED",
                        "Run wall-time budget expired while an operation was active",
                    )
                if progress_callback is not None:
                    await progress_callback(
                        asyncio.get_running_loop().time() - started
                    )
        except asyncio.CancelledError:
            await _cancel_operation_task(operation_task)
            raise
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
        if call.name in {"run_command", "judge_algorithm"} and result.metadata.get("purpose") == "verify":
            verification_command = str(
                call.arguments.get("command")
                or (result.data or {}).get("command")
                or ""
            )
            verification_result = result.to_dict()
            if call.name == "judge_algorithm":
                # Judge results are verification evidence even though they do
                # not expose a shell exit code. Keep the command label
                # explicit so the evidence classifier can trust this tool.
                verification_command = f"judge_algorithm {verification_command}".strip()
                verification_result.setdefault("data", {})["exit_code"] = 0 if result.ok else 1
            evidence = build_verification_evidence(
                command=verification_command,
                purpose=str(result.metadata.get("purpose") or call.arguments.get("purpose") or "other"),
                result=verification_result,
                objective=state.current_objective,
                changed_files=state.changed_files,
                started_sequence=started_sequence,
                finished_sequence=event.sequence,
                project_commands=self.project_verification_commands,
                dependency_graph=self._verification_dependency_graph(),
            )
            evidence_payload = evidence.to_dict()
            await self._emit(
                state, "verification_recorded", {"evidence": evidence_payload}
            )
            await self._run_lifecycle_hooks(
                state, "OnVerification", evidence_payload,
                self.tool_executor.hooks.on_verification(evidence_payload),
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

    def _verification_dependency_graph(self) -> dict[str, list[str]] | None:
        """Build a best-effort import graph for targeted-test coverage checks."""
        workspace = self.workspace or getattr(self.context_manager, "workspace", None)
        if workspace is None:
            return None
        try:
            from .repo_map import RepoMapBuilder

            data = RepoMapBuilder(workspace).build(max_files=80)
        except Exception:
            # Verification evidence must remain conservative and must never
            # fail a tool result because indexing was unavailable.
            return None
        return {
            str(item.get("path") or ""): [str(value) for value in item.get("dependencies", [])]
            for item in data.get("files", [])
            if item.get("path")
        }

    def _allowed_tool_schemas(self, mode: str, task_profile: str = "project") -> list[dict[str, Any]]:
        profile_tools = get_profile(task_profile).allowed_tools
        if mode == "act":
            names = set(self.registry.names())
            if profile_tools is not None:
                names &= profile_tools
            return self.registry.schemas(names)
        read_names = {
            name
            for name in self.registry.names()
            if self.registry.get(name).risk is ToolRisk.READ
        }
        if profile_tools is not None:
            read_names &= profile_tools
        return self.registry.schemas(read_names)


    async def _finish(
        self, state: AgentState, status: AgentStatus, message: str
    ) -> AgentRunResult:
        finish_payload = {
            "status": status,
            "message": message,
            "changed_files": sorted(state.changed_files),
            "verification_fresh": state.verification_is_fresh,
            "verification_evidence": state.verification_evidence,
        }
        await self._run_lifecycle_hooks(
            state,
            "OnTaskEnd",
            finish_payload,
            self.tool_executor.hooks.on_task_end(finish_payload),
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
        return AgentRunResult(status, message, state)

    async def _run_lifecycle_hooks(
        self,
        state: AgentState,
        lifecycle: str,
        payload: dict[str, Any],
        decisions_awaitable: Awaitable[list[Any]],
    ) -> None:
        """Run hooks and persist their decisions without granting policy authority."""
        hook_started = perf_counter()
        hook_span_id = await self._start_span(
            state, "hook_pipeline", {"lifecycle": lifecycle}
        )
        try:
            try:
                decisions = await decisions_awaitable
            except Exception as exc:  # HookManager normally normalizes this; keep loop safe.
                decisions = [{
                    "allow": False,
                    "code": "HOOK_FAILED",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "hook": "HookManager",
                }]
        finally:
            await self._finish_span(state, hook_span_id, "hook_pipeline", hook_started)
        for decision in decisions:
            if hasattr(decision, "allow"):
                data = {
                    "allow": bool(decision.allow),
                    "code": str(decision.code),
                    "reason": str(decision.reason),
                    "hook": str(decision.hook),
                    "additional_context": str(decision.additional_context),
                }
            else:
                data = dict(decision)
            await self._emit(
                state,
                "hook_executed",
                {"lifecycle": lifecycle, **data},
            )
            context = str(data.get("additional_context") or "").strip()
            if context:
                await self._emit(
                    state,
                    "hook_context",
                    {"lifecycle": lifecycle, "hook": data.get("hook", ""), "content": context[:2_000]},
                )

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

    async def _start_span(
        self, state: AgentState, kind: str, payload: dict[str, Any] | None = None
    ) -> str:
        span_id = uuid4().hex
        event = await self._emit(
            state,
            "span_started",
            {"span_id": span_id, "kind": kind, **(payload or {})},
        )
        return span_id

    async def _finish_span(
        self, state: AgentState, span_id: str, kind: str, started: float
    ) -> None:
        await self._emit(
            state,
            "span_finished",
            {
                "span_id": span_id,
                "kind": kind,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
            },
        )


def _serialize_call(call: ToolCall) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def _contains_redacted(value: Any) -> bool:
    if isinstance(value, str):
        return value == "[REDACTED]"
    if isinstance(value, dict):
        return any(_contains_redacted(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_redacted(item) for item in value)
    return False


async def _cancel_operation_task(task: asyncio.Future[Any]) -> None:
    """Cancel an operation without letting cancellation-resistant I/O freeze the run."""
    if task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.shield(task), timeout=OPERATION_CANCEL_GRACE_SECONDS
        )
    except TimeoutError:
        task.add_done_callback(_consume_background_task)
    except asyncio.CancelledError:
        pass
    except Exception:
        # The caller is already terminating the operation; its original
        # failure must not replace the cancellation/budget outcome.
        pass


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass
