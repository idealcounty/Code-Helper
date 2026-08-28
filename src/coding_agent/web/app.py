from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agent_loop import AgentRunResult
from ..config import AppConfig
from ..events import AgentEvent
from ..model import ModelClient, ToolCall
from ..permissions import PermissionResult
from ..runtime import AgentRuntime, create_runtime
from ..tools.base import ToolError


class CreateSessionRequest(BaseModel):
    workspace: str = Field(min_length=1)
    mode: Literal["ask", "plan", "act"] = "act"


class MessageRequest(BaseModel):
    content: str = Field(min_length=1)


class ApprovalRequest(BaseModel):
    tool_call_id: str
    approved: bool


class ModeRequest(BaseModel):
    mode: Literal["ask", "plan", "act"]


class ApprovalBroker:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._early_decisions: dict[str, bool] = {}

    async def request(self, call: ToolCall, _: PermissionResult) -> bool:
        if call.id in self._pending:
            raise RuntimeError(f"Duplicate pending approval: {call.id}")
        if call.id in self._early_decisions:
            return self._early_decisions.pop(call.id)
        future = asyncio.get_running_loop().create_future()
        self._pending[call.id] = future
        try:
            return await future
        finally:
            self._pending.pop(call.id, None)

    def resolve(self, tool_call_id: str, approved: bool) -> None:
        future = self._pending.get(tool_call_id)
        if future is None:
            self._early_decisions[tool_call_id] = approved
            return
        if future.done():
            raise KeyError(tool_call_id)
        future.set_result(approved)

    def reject_all(self) -> None:
        self._early_decisions.clear()
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_result(False)


@dataclass(slots=True)
class WebSession:
    runtime: AgentRuntime
    approval_broker: ApprovalBroker
    task: asyncio.Task[AgentRunResult] | None = None
    last_error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass(slots=True)
class WebSessionManager:
    config: AppConfig
    model_client_factory: Callable[[], ModelClient] | None = None
    sessions: dict[str, WebSession] = field(default_factory=dict)

    def create(self, workspace: str, mode: str) -> WebSession:
        if not self.config.api_key:
            raise ValueError(
                "API key is not configured; set DEEPSEEK_API_KEY or "
                "CODE_HELPER_API_KEY"
            )
        broker = ApprovalBroker()
        runtime = create_runtime(
            config=self.config,
            workspace_path=Path(workspace),
            mode=mode,
            model_client=(
                self.model_client_factory() if self.model_client_factory else None
            ),
            approval_handler=broker.request,
        )
        session = WebSession(runtime=runtime, approval_broker=broker)
        self.sessions[runtime.state.session_id] = session
        return session

    def get(self, session_id: str) -> WebSession:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc


def create_app(
    config: AppConfig | None = None,
    model_client_factory: Callable[[], ModelClient] | None = None,
) -> FastAPI:
    app = FastAPI(title="Code Helper", version="0.1.0")
    manager = WebSessionManager(
        config or AppConfig.from_env(), model_client_factory=model_client_factory
    )
    static_root = Path(__file__).with_name("static")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "api_key_configured": bool(manager.config.api_key),
            "provider": manager.config.provider,
            "model": manager.config.model,
            "thinking_mode": manager.config.thinking_mode,
            "reasoning_effort": manager.config.reasoning_effort,
        }

    @app.post("/api/sessions")
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        try:
            session = manager.create(request.workspace, request.mode)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime = session.runtime
        return {
            "session_id": runtime.state.session_id,
            "workspace": str(runtime.workspace.root),
            "mode": runtime.state.mode,
        }

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        state = session.runtime.state
        return {
            "session_id": state.session_id,
            "turn_id": state.turn_id,
            "status": state.status,
            "mode": state.mode,
            "step": state.step,
            "running": session.running,
            "changed_files": sorted(state.changed_files),
            "plan": state.plan,
            "token_usage": state.token_usage,
            "tool_stats": state.tool_stats,
            "last_error": session.last_error,
        }

    @app.post("/api/sessions/{session_id}/messages", status_code=202)
    async def send_message(
        session_id: str, request: MessageRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        if session.running:
            raise HTTPException(status_code=409, detail="Agent is already running")

        async def execute() -> AgentRunResult:
            try:
                return await session.runtime.runner.run_turn(
                    session.runtime.state, request.content
                )
            except Exception as exc:
                session.last_error = f"{type(exc).__name__}: {exc}"
                raise

        session.task = asyncio.create_task(execute())

        def consume_task_result(task: asyncio.Task[AgentRunResult]) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                session.last_error = f"{type(exc).__name__}: {exc}"

        session.task.add_done_callback(consume_task_result)
        return {
            "accepted": True,
            "session_id": session_id,
            "turn_id": session.runtime.state.turn_id,
        }

    @app.post("/api/sessions/{session_id}/mode")
    async def set_mode(session_id: str, request: ModeRequest) -> dict[str, Any]:
        session = manager.get(session_id)
        if session.running:
            raise HTTPException(status_code=409, detail="Cannot change mode while running")
        session.runtime.state.mode = request.mode
        return {"mode": request.mode}

    @app.post("/api/sessions/{session_id}/approval")
    async def resolve_approval(
        session_id: str, request: ApprovalRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        try:
            session.approval_broker.resolve(request.tool_call_id, request.approved)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="Approval is no longer pending") from exc
        return {"resolved": True, "approved": request.approved}

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        session.runtime.state.cancel_requested = True
        session.approval_broker.reject_all()
        return {"cancel_requested": True}

    @app.get("/api/sessions/{session_id}/events")
    async def get_events(session_id: str) -> list[dict[str, Any]]:
        return manager.get(session_id).runtime.event_store.load()

    @app.get("/api/sessions/{session_id}/files")
    async def list_workspace_files(
        session_id: str, path: str = "."
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        workspace = session.runtime.workspace
        try:
            directory = workspace.resolve(path, must_exist=True)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not directory.is_dir():
            raise HTTPException(status_code=400, detail="Path is not a directory")

        entries: list[dict[str, Any]] = []
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.casefold()),
            )
            for child in children:
                if workspace.is_ignored(child) or workspace.is_sensitive(child):
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": workspace.relative(child),
                        "kind": "directory" if child.is_dir() else "file",
                    }
                )
                if len(entries) >= 500:
                    break
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"Cannot list directory: {exc}"
            ) from exc
        return {
            "path": workspace.relative(directory),
            "entries": entries,
            "truncated": len(entries) >= 500,
        }

    @app.get("/api/sessions/{session_id}/diff")
    async def get_diff(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        process = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--no-color",
            cwd=session.runtime.workspace.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise HTTPException(status_code=504, detail="git diff timed out")
        return {
            "ok": process.returncode == 0,
            "diff": stdout.decode(errors="replace")[:100_000],
            "error": stderr.decode(errors="replace")[:2_000],
        }

    @app.get("/api/sessions/{session_id}/checkpoint")
    async def get_checkpoint(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        state = session.runtime.state
        return {
            "turn_id": state.turn_id,
            "files": session.runtime.checkpoint_manager.list_files(state.turn_id),
        }

    @app.post("/api/sessions/{session_id}/restore")
    async def restore_checkpoint(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        if session.running:
            raise HTTPException(status_code=409, detail="Cannot restore while Agent is running")
        state = session.runtime.state
        try:
            restored = session.runtime.checkpoint_manager.restore(state.turn_id)
        except ToolError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        state.changed_files.clear()
        state.last_mutation_sequence = 0
        state.last_successful_verification_sequence = 0
        await session.runtime.event_bus.publish(
            AgentEvent(
                type="checkpoint_restored",
                session_id=state.session_id,
                turn_id=state.turn_id,
                payload={"files": restored},
            )
        )
        return {"restored": restored}

    @app.websocket("/ws/sessions/{session_id}")
    async def session_events(websocket: WebSocket, session_id: str) -> None:
        session = manager.sessions.get(session_id)
        if session is None:
            await websocket.close(code=4404, reason="Session not found")
            return
        await websocket.accept()
        queue, unsubscribe = session.runtime.event_bus.create_queue()
        try:
            history = session.runtime.event_store.load()
            last_sequence = 0
            for event in history:
                last_sequence = max(last_sequence, int(event.get("sequence", 0)))
                await websocket.send_json(event)
            while True:
                event = await queue.get()
                if event.sequence > last_sequence:
                    await websocket.send_json(event.to_dict())
                    last_sequence = event.sequence
        except WebSocketDisconnect:
            pass
        finally:
            unsubscribe()

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_root / "index.html")

    app.mount("/static", StaticFiles(directory=static_root), name="static")
    app.state.session_manager = manager
    return app


app = create_app()


def main() -> None:
    uvicorn.run(
        "coding_agent.web.app:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()
