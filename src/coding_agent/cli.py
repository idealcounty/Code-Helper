from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .agent_loop import AgentRunner
from .config import AppConfig
from .context import ContextManager
from .events import AgentEvent, EventBus, EventStore
from .model import OpenAICompatibleModelClient, ToolCall
from .permissions import PermissionPolicy, PermissionResult
from .session import AgentState
from .tool_executor import ToolExecutor
from .tools import ToolRegistry, Workspace, register_filesystem_tools, register_shell_tools


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Code Helper local coding agent")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Project directory the agent may access",
    )
    parser.add_argument(
        "--mode", choices=["ask", "plan", "act"], default="act"
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    config = AppConfig.from_env()
    if not config.api_key:
        print("Missing CODE_HELPER_API_KEY. Configure it as an environment variable.")
        return 2

    workspace = Workspace(args.workspace)
    state = AgentState.create(
        max_steps=config.max_steps,
        mode=args.mode,
        reasoning_mode=config.reasoning_effort,
    )
    event_store = EventStore(
        workspace.root / ".code-helper" / "sessions", state.session_id
    )
    event_bus = EventBus(event_store)
    event_bus.subscribe(_print_event)

    registry = ToolRegistry()
    register_filesystem_tools(registry, workspace)
    register_shell_tools(
        registry, workspace, default_timeout=config.command_timeout
    )

    model_client = OpenAICompatibleModelClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        timeout=config.request_timeout,
    )
    runner = AgentRunner(
        model_client=model_client,
        context_manager=ContextManager(),
        registry=registry,
        tool_executor=ToolExecutor(registry),
        permission_policy=PermissionPolicy(),
        event_bus=event_bus,
        approval_handler=_ask_for_approval,
    )

    print(f"Code Helper | workspace: {workspace.root} | mode: {state.mode}")
    print("Commands: /exit, /mode ask|plan|act")
    while True:
        try:
            user_input = (await asyncio.to_thread(input, "\nYou> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input == "/exit":
            break
        if user_input.startswith("/mode "):
            requested = user_input.removeprefix("/mode ").strip()
            if requested not in {"ask", "plan", "act"}:
                print("Mode must be ask, plan, or act")
                continue
            state.mode = requested
            print(f"Mode changed to {requested}")
            continue

        result = await runner.run_turn(state, user_input)
        print(f"\n[{result.status}] {result.message}")
    return 0


async def _ask_for_approval(
    call: ToolCall, permission: PermissionResult
) -> bool:
    print(f"\nApproval required: {call.name}")
    print(json.dumps(call.arguments, ensure_ascii=False, indent=2))
    print(f"Reason: {permission.reason}")
    answer = (await asyncio.to_thread(input, "Allow once? [y/N] ")).strip().lower()
    return answer in {"y", "yes"}


def _print_event(event: AgentEvent) -> None:
    if event.type == "assistant_response":
        content = event.payload.get("content")
        if content:
            print(f"\nAgent> {content}")
    elif event.type == "tool_started":
        print(f"\n→ {event.payload['name']}")
    elif event.type == "tool_result":
        result = event.payload["result"]
        marker = "✓" if result["ok"] else "✗"
        print(f"{marker} {event.payload['name']}: {result['message']}")
    elif event.type == "verification_required":
        print(f"! {event.payload['reason']}")


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))

