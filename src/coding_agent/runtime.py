from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .agent_loop import AgentRunner, ApprovalHandler
from .budget import RunBudget
from .cancellation import CancellationToken
from .checkpoints import CheckpointManager
from .hooks import HookDecision, HookManager
from .config import AppConfig
from .context import ContextManager
from .events import EventBus, EventListener, EventStore
from .model import ModelClient, OpenAICompatibleModelClient
from .memory import MemoryStore
from .memory_summary import SessionSummaryStore
from .user_memory import UserMemoryService
from .verification_config import VerificationConfig
from .permissions import PermissionPolicy
from .redaction import Redactor
from .session import AgentState
from .skills import SkillLibrary
from .tool_executor import ToolExecutor
from .tools import (
    ToolRegistry,
    Workspace,
    register_filesystem_tools,
    register_algorithm_tools,
    register_git_tools,
    register_repo_map_tool,
    register_shell_tools,
    register_plan_tools,
    register_skill_tools,
    register_memory_tools,
    register_user_memory_tools,
)


@dataclass(slots=True)
class AgentRuntime:
    config: AppConfig
    workspace: Workspace
    state: AgentState
    event_store: EventStore
    event_bus: EventBus
    registry: ToolRegistry
    skill_library: SkillLibrary
    context_manager: ContextManager
    tool_executor: ToolExecutor
    checkpoint_manager: CheckpointManager
    memory_store: MemoryStore
    summary_store: SessionSummaryStore
    user_memory: UserMemoryService
    verification_config: VerificationConfig
    cancellation: CancellationToken
    run_budget: RunBudget
    runner: AgentRunner


async def _verification_context_hook(evidence: dict[str, object]) -> HookDecision | None:
    """Keep rejected verification visible to the next model step."""
    if bool(evidence.get("accepted")):
        return None
    reason = str(evidence.get("reason") or "verification did not satisfy the completion contract")
    return HookDecision(
        additional_context=(
            "Verification hook: the latest verification was rejected. "
            f"Inspect the failure and repair before claiming completion. Reason: {reason}"
        )
    )


async def _task_end_evidence_hook(summary: dict[str, object]) -> HookDecision | None:
    """Annotate suspicious completed turns without changing their status."""
    if str(summary.get("status")) == "completed" and not bool(summary.get("verification_fresh")):
        return HookDecision(
            additional_context="Task-end hook: completion was reported without fresh verification evidence."
        )
    return None


def create_runtime(
    *,
    config: AppConfig,
    workspace_path: Path,
    mode: str = "act",
    task_profile: str = "auto",
    session_id: str | None = None,
    model_client: ModelClient | None = None,
    approval_handler: ApprovalHandler | None = None,
    event_listener: EventListener | None = None,
) -> AgentRuntime:
    workspace = Workspace(workspace_path)
    state = AgentState.create(
        max_steps=config.max_steps,
        mode=mode,
        task_profile=task_profile,
        reasoning_mode=config.reasoning_effort,
        session_id=session_id,
    )
    redactor = Redactor([config.api_key])
    event_store = EventStore(
        workspace.root / ".code-helper" / "sessions",
        state.session_id,
        redactor=redactor,
    )
    event_bus = EventBus(event_store)
    if event_listener is not None:
        event_bus.subscribe(event_listener)

    registry = ToolRegistry()
    skill_library = SkillLibrary(Path(__file__).resolve().parents[2] / "skills")
    memory_store = MemoryStore(
        workspace.root / ".code-helper" / "memory",
        workspace_root=workspace.root,
    )
    summary_store = SessionSummaryStore(
        workspace.root / ".code-helper" / "memory" / "summaries"
    )
    user_memory_root = config.user_memory_dir or _default_user_memory_root()
    if user_memory_root.resolve().is_relative_to(workspace.root):
        raise ValueError("User memory directory must be outside the project workspace")
    user_memory = UserMemoryService(
        user_memory_root,
        initially_enabled=config.user_memory_enabled,
    )
    verification_config = VerificationConfig.load(workspace.root)
    cancellation = CancellationToken()
    run_budget = RunBudget(
        max_seconds=config.run_timeout,
        token_limit=config.token_budget,
        max_steps=config.max_steps,
    )
    register_filesystem_tools(registry, workspace)
    register_algorithm_tools(registry, workspace, cancellation=cancellation)
    register_repo_map_tool(registry, workspace)
    register_git_tools(registry, workspace)
    register_shell_tools(
        registry,
        workspace,
        default_timeout=config.command_timeout,
        cancellation=cancellation,
    )
    register_plan_tools(registry, state)
    register_skill_tools(registry, skill_library)
    register_memory_tools(registry, memory_store, state, summary_store)
    register_user_memory_tools(registry, user_memory, state)
    client = model_client or OpenAICompatibleModelClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        timeout=config.request_timeout,
        provider=config.provider,
        thinking_mode=config.thinking_mode,
    )
    checkpoint_manager = CheckpointManager(workspace)
    context_manager = ContextManager(
        workspace=workspace,
        skill_library=skill_library,
        memory_store=memory_store,
        user_memory=user_memory,
        project_verification_commands=verification_config.commands,
    )
    hooks = HookManager(
        verification=[_verification_context_hook],
        task_end=[_task_end_evidence_hook],
    )
    tool_executor = ToolExecutor(
        registry,
        hooks=hooks,
        result_store=workspace.root / ".code-helper" / "tool-results",
        redactor=redactor,
    )
    runner = AgentRunner(
        model_client=client,
        context_manager=context_manager,
        registry=registry,
        tool_executor=tool_executor,
        permission_policy=PermissionPolicy(workspace_root=workspace.root),
        event_bus=event_bus,
        approval_handler=approval_handler,
        checkpoint_manager=checkpoint_manager,
        turn_summarizer=lambda current_state, status, outcome: summary_store.create(
            current_state, status, outcome, memory_store
        ).to_dict(),
        cancellation=cancellation,
        run_budget=run_budget,
        project_verification_commands=verification_config.commands,
    )
    return AgentRuntime(
        config=config,
        workspace=workspace,
        state=state,
        event_store=event_store,
        event_bus=event_bus,
        registry=registry,
        skill_library=skill_library,
        context_manager=context_manager,
        tool_executor=tool_executor,
        checkpoint_manager=checkpoint_manager,
        memory_store=memory_store,
        summary_store=summary_store,
        user_memory=user_memory,
        verification_config=verification_config,
        cancellation=cancellation,
        run_budget=run_budget,
        runner=runner,
    )


def _default_user_memory_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "CodeHelper" / "user-memory"
    return Path.home() / ".code-helper" / "user-memory"
