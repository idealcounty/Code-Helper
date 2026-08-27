from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .agent_loop import AgentRunner
from .config import AppConfig
from .events import AgentEvent
from .model import ToolCall
from .permissions import PermissionResult
from .runtime import create_runtime


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
        print(
            "Missing API key. Set DEEPSEEK_API_KEY (DeepSeek) or "
            "CODE_HELPER_API_KEY."
        )
        return 2

    runtime = create_runtime(
        config=config,
        workspace_path=args.workspace,
        mode=args.mode,
        approval_handler=_ask_for_approval,
        event_listener=_print_event,
    )
    workspace = runtime.workspace
    state = runtime.state
    runner = runtime.runner

    print(
        f"Code Helper | {config.provider}/{config.model} | "
        f"workspace: {workspace.root} | mode: {state.mode}"
    )
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
