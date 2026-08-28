from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent_loop import AgentRunner, ApprovalHandler
from .checkpoints import CheckpointManager
from .config import AppConfig
from .context import ContextManager
from .events import EventBus, EventListener, EventStore
from .model import ModelClient, OpenAICompatibleModelClient
from .permissions import PermissionPolicy
from .session import AgentState
from .skills import SkillLibrary
from .tool_executor import ToolExecutor
from .tools import (
    ToolRegistry,
    Workspace,
    register_filesystem_tools,
    register_git_tools,
    register_repo_map_tool,
    register_shell_tools,
    register_plan_tools,
    register_skill_tools,
)


@dataclass(slots=True)
class AgentRuntime:
    config: AppConfig
    workspace: Workspace
    state: AgentState
    event_store: EventStore
    event_bus: EventBus
    registry: ToolRegistry
    checkpoint_manager: CheckpointManager
    runner: AgentRunner


def create_runtime(
    *,
    config: AppConfig,
    workspace_path: Path,
    mode: str = "act",
    session_id: str | None = None,
    model_client: ModelClient | None = None,
    approval_handler: ApprovalHandler | None = None,
    event_listener: EventListener | None = None,
) -> AgentRuntime:
    workspace = Workspace(workspace_path)
    state = AgentState.create(
        max_steps=config.max_steps,
        mode=mode,
        reasoning_mode=config.reasoning_effort,
        session_id=session_id,
    )
    event_store = EventStore(
        workspace.root / ".code-helper" / "sessions", state.session_id
    )
    event_bus = EventBus(event_store)
    if event_listener is not None:
        event_bus.subscribe(event_listener)

    registry = ToolRegistry()
    skill_library = SkillLibrary(Path(__file__).resolve().parents[2] / "skills")
    register_filesystem_tools(registry, workspace)
    register_repo_map_tool(registry, workspace)
    register_git_tools(registry, workspace)
    register_shell_tools(
        registry, workspace, default_timeout=config.command_timeout
    )
    register_plan_tools(registry, state)
    register_skill_tools(registry, skill_library)
    client = model_client or OpenAICompatibleModelClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        timeout=config.request_timeout,
        provider=config.provider,
        thinking_mode=config.thinking_mode,
    )
    checkpoint_manager = CheckpointManager(workspace)
    runner = AgentRunner(
        model_client=client,
        context_manager=ContextManager(workspace=workspace, skill_library=skill_library),
        registry=registry,
        tool_executor=ToolExecutor(registry, result_store=workspace.root / ".code-helper" / "tool-results"),
        permission_policy=PermissionPolicy(),
        event_bus=event_bus,
        approval_handler=approval_handler,
        checkpoint_manager=checkpoint_manager,
    )
    return AgentRuntime(
        config=config,
        workspace=workspace,
        state=state,
        event_store=event_store,
        event_bus=event_bus,
        registry=registry,
        checkpoint_manager=checkpoint_manager,
        runner=runner,
    )
