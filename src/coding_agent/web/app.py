from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agent_loop import AgentRunResult
from ..config import AppConfig
from ..events import AgentEvent
from ..model import ModelClient, ToolCall
from ..permissions import ApprovalMode, PermissionResult
from ..repo_map import RepoMapBuilder
from ..runtime import AgentRuntime, create_runtime
from ..session import AgentStatus
from ..tools.base import ToolError


def _budget_view(runtime: AgentRuntime) -> dict[str, Any]:
    if runtime.state.run_budget:
        return runtime.state.run_budget
    return {
        **runtime.run_budget.snapshot(),
        "max_steps": runtime.state.max_steps,
    }


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _percentile(samples: list[float], quantile: float) -> float:
    """Return a deterministic linear-interpolated percentile in milliseconds."""
    if not samples:
        return 0.0
    ordered = sorted(float(value) for value in samples)
    position = (len(ordered) - 1) * min(max(quantile, 0.0), 1.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _drive_roots() -> list[Path]:
    if os.name == "nt":
        return [
            Path(f"{letter}:\\")
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{letter}:\\").exists()
        ]
    return [Path("/")]


def _directory_entries(directory: Path) -> list[Path]:
    ignored = {"$recycle.bin", "system volume information"}
    return sorted(
        (
            child
            for child in directory.iterdir()
            if child.name.casefold() not in ignored and child.is_dir()
        ),
        key=lambda child: child.name.casefold(),
    )


def _load_event_file(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return events


def _storage_usage(root: Path, pattern: str) -> dict[str, int]:
    """Return bounded, read-only storage usage for the intelligence panel."""
    files = [path for path in root.glob(pattern) if path.is_file()] if root.exists() else []
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return {"files": len(files), "bytes": total}


def _session_summary(
    session_id: str, events: list[dict[str, Any]], updated_at: str
) -> dict[str, Any]:
    first_message = "新对话"
    preview = "尚未发送消息"
    status = "ready"
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") == "turn_started" and payload.get("message"):
            message = str(payload["message"]).strip()
            first_message = message[:42]
            preview = message[:72]
            break
    for event in reversed(events):
        payload = event.get("payload") or {}
        if event.get("type") == "assistant_response" and payload.get("content"):
            preview = str(payload["content"]).strip()[:72]
            break
    for event in reversed(events):
        if event.get("type") == "turn_finished":
            status = str((event.get("payload") or {}).get("status", "ready"))
            break
    return {
        "session_id": session_id,
        "title": first_message,
        "preview": preview,
        "status": status,
        "updated_at": updated_at,
    }


def _language_from_path(path: Path) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescriptreact",
        ".jsx": "javascriptreact",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".json": "json",
        ".md": "markdown",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".sh": "shell",
        ".ps1": "powershell",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".rs": "rust",
        ".go": "go",
    }.get(path.suffix.casefold(), "plaintext")


REASONING_PROFILES = {
    "auto": None,
    "fast": "low",
    "balanced": "medium",
    "deep": "high",
}
WEB_CANCEL_FORCE_SECONDS = 2.0


def _reasoning_profile(value: str | None) -> tuple[str, str | None]:
    normalized = (value or "auto").strip().lower()
    if normalized not in REASONING_PROFILES:
        raise ValueError(f"Unknown reasoning profile: {value}")
    return normalized, REASONING_PROFILES[normalized]


def _profile_from_effort(value: str | None) -> str:
    return {None: "auto", "low": "fast", "medium": "balanced", "high": "deep"}.get(
        value, value or "auto"
    )


class CreateSessionRequest(BaseModel):
    workspace: str = Field(min_length=1)
    mode: Literal["ask", "plan", "act"] = "act"
    session_id: str | None = None
    reasoning_profile: Literal["auto", "fast", "balanced", "deep"] = "auto"
    task_profile: Literal["auto", "project", "algorithm"] = "auto"
    approval_policy: Literal["ask", "auto", "full"] = "ask"


class MessageRequest(BaseModel):
    content: str = Field(min_length=1)


class ApprovalRequest(BaseModel):
    tool_call_id: str
    approved: bool
    scope: Literal["once", "session"] = "once"
    ttl_seconds: float = Field(default=3600.0, ge=60.0, le=86_400.0)


class ModeRequest(BaseModel):
    mode: Literal["ask", "plan", "act"]


class ReasoningRequest(BaseModel):
    profile: Literal["auto", "fast", "balanced", "deep"]


class ApprovalPolicyRequest(BaseModel):
    policy: Literal["ask", "auto", "full"]


class RecoveryRequest(BaseModel):
    action: Literal["retry", "abandon"]
    tool_call_id: str = Field(min_length=1)
    confirm: bool = False


class MemoryCandidateRequest(BaseModel):
    action: Literal["confirm", "reject"]


class UserMemorySettingRequest(BaseModel):
    enabled: bool


class RestoreRequest(BaseModel):
    paths: list[str] | None = None
    force: bool = False
    confirmed_hashes: dict[str, str | None] | None = None


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
    cancel_watchdog: asyncio.Task[None] | None = None
    last_error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass(slots=True)
class WebSessionManager:
    config: AppConfig
    model_client_factory: Callable[[], ModelClient] | None = None
    sessions: dict[str, WebSession] = field(default_factory=dict)

    def create(
        self,
        workspace: str,
        mode: str,
        session_id: str | None = None,
        reasoning_profile: str = "auto",
        task_profile: str = "auto",
        approval_policy: str = "ask",
    ) -> WebSession:
        if not self.config.api_key:
            raise ValueError(
                "API key is not configured; set DEEPSEEK_API_KEY or "
                "CODE_HELPER_API_KEY"
            )
        resolved_workspace = Path(workspace).expanduser().resolve(strict=True)
        if not resolved_workspace.is_dir():
            raise ValueError("Workspace path is not a directory")
        if session_id and session_id in self.sessions:
            existing = self.sessions[session_id]
            if existing.runtime.workspace.root != resolved_workspace:
                raise ValueError("Session belongs to a different workspace")
            return existing
        broker = ApprovalBroker()
        runtime = create_runtime(
            config=self.config,
            workspace_path=resolved_workspace,
            mode=mode,
            task_profile=task_profile,
            session_id=session_id,
            model_client=(
                self.model_client_factory() if self.model_client_factory else None
            ),
            approval_handler=broker.request,
        )
        _, runtime.state.reasoning_mode = _reasoning_profile(reasoning_profile)
        runtime.runner.permission_policy.set_approval_mode(approval_policy)
        if session_id:
            events = runtime.event_store.load()
            runtime.state.restore_from_events(
                events,
                recovery_diagnostics=runtime.event_store.last_load_diagnostics,
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

    @app.get("/api/fs/browse")
    async def browse_directories(path: str = "") -> dict[str, Any]:
        if not path:
            return {
                "path": "",
                "parent": None,
                "entries": [
                    {"name": str(root), "path": str(root), "kind": "drive"}
                    for root in _drive_roots()
                ],
            }
        try:
            directory = Path(path).expanduser().resolve(strict=True)
            if not directory.is_dir():
                raise ValueError("Path is not a directory")
            entries = _directory_entries(directory)[:300]
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Cannot browse directory: {exc}"
            ) from exc
        parent = directory.parent if directory.parent != directory else None
        return {
            "path": str(directory),
            "parent": str(parent) if parent else None,
            "entries": [
                {"name": child.name, "path": str(child), "kind": "directory"}
                for child in entries
            ],
        }

    @app.get("/api/workspaces/sessions")
    async def list_workspace_sessions(workspace: str) -> dict[str, Any]:
        try:
            root = Path(workspace).expanduser().resolve(strict=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not root.is_dir():
            raise HTTPException(status_code=400, detail="Workspace is not a directory")

        summaries: dict[str, dict[str, Any]] = {}
        session_root = root / ".code-helper" / "sessions"
        if session_root.exists():
            for event_path in session_root.glob("*.jsonl"):
                stat = event_path.stat()
                updated = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
                summaries[event_path.stem] = _session_summary(
                    event_path.stem, _load_event_file(event_path), updated
                )
        for session_id, session in manager.sessions.items():
            if session.runtime.workspace.root != root or session_id in summaries:
                continue
            summaries[session_id] = _session_summary(
                session_id, [], datetime.now(UTC).isoformat()
            )
        sessions = sorted(
            summaries.values(), key=lambda item: item["updated_at"], reverse=True
        )[:50]
        return {"workspace": str(root), "sessions": sessions}

    @app.post("/api/sessions")
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        try:
            session = manager.create(
                request.workspace,
                request.mode,
                request.session_id,
                request.reasoning_profile,
                request.task_profile,
                request.approval_policy,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime = session.runtime
        return {
            "session_id": runtime.state.session_id,
            "workspace": str(runtime.workspace.root),
            "mode": runtime.state.mode,
            "reasoning_profile": _profile_from_effort(runtime.state.reasoning_mode),
            "task_profile": runtime.state.requested_task_profile,
            "approval_policy": runtime.runner.permission_policy.approval_mode,
        }

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        state = session.runtime.state
        return {
            "session_id": state.session_id,
            "workspace": str(session.runtime.workspace.root),
            "turn_id": state.turn_id,
            "status": state.status,
            "mode": state.mode,
            "reasoning_profile": _profile_from_effort(state.reasoning_mode),
            "task_profile": state.task_profile,
            "approval_policy": session.runtime.runner.permission_policy.approval_mode,
            "step": state.step,
            "running": session.running,
            "changed_files": sorted(state.changed_files),
            "plan": state.plan,
            "token_usage": state.token_usage,
            "tool_stats": state.tool_stats,
            "budget": _budget_view(session.runtime),
            "verification_evidence": state.verification_evidence,
            "pending_approval": state.pending_approval,
            "interrupted_tool_calls": state.interrupted_tool_calls,
            "recovery_warnings": state.recovery_warnings,
            "last_error": session.last_error,
        }

    @app.get("/api/sessions/{session_id}/intelligence")
    async def get_intelligence(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        runtime = session.runtime
        state = runtime.state
        events = runtime.event_store.load()
        loaded_skills: list[str] = []
        compactions = 0
        output_references: list[str] = []
        repo_map_calls = 0
        last_context_built: dict[str, Any] = {}
        output_deltas = 0
        # Keep lifecycle Hook spans separate by hook name.  Other spans retain
        # the historical kind-only aggregation shape for API compatibility.
        span_totals: dict[tuple[str, str, str], dict[str, Any]] = {}
        active_span_ids: set[str] = set()
        cancel_requests = 0
        pending_cancel_times: dict[str, datetime] = {}
        cancel_latencies: list[float] = []
        for event in events:
            payload = event.get("payload") or {}
            event_type = event.get("type")
            turn_id = str(event.get("turn_id") or "")
            if event_type == "run_cancel_requested":
                cancel_requests += 1
                requested_at = _event_timestamp(event)
                if requested_at is not None and turn_id:
                    pending_cancel_times[turn_id] = requested_at
            elif event_type == "run_cancelled" and turn_id:
                started_at = pending_cancel_times.pop(turn_id, None)
                finished_at = _event_timestamp(event)
                if started_at is not None and finished_at is not None:
                    latency = (finished_at - started_at).total_seconds() * 1000
                    if latency >= 0:
                        cancel_latencies.append(round(latency, 3))
            if event.get("type") == "context_compacted":
                compactions += 1
            if event.get("type") == "context_built":
                last_context_built = dict(payload)
            if event.get("type") == "tool_output_delta":
                output_deltas += 1
            if event.get("type") == "span_started":
                span_id = payload.get("span_id") or event.get("event_id")
                if span_id:
                    active_span_ids.add(str(span_id))
            if event.get("type") == "span_finished":
                span_id = payload.get("span_id")
                if span_id:
                    active_span_ids.discard(str(span_id))
                kind = str(payload.get("kind") or "unknown")
                lifecycle = str(payload.get("lifecycle") or "")
                hook = str(payload.get("hook") or "")
                duration = payload.get("duration_ms")
                if isinstance(duration, (int, float)) and duration >= 0:
                    stats = span_totals.setdefault(
                        (kind, lifecycle, hook),
                        {
                            "count": 0,
                            "total_duration_ms": 0.0,
                            "max_duration_ms": 0.0,
                            "samples_ms": [],
                        },
                    )
                    stats["count"] = int(stats["count"]) + 1
                    stats["total_duration_ms"] = float(stats["total_duration_ms"]) + float(duration)
                    stats["max_duration_ms"] = max(float(stats["max_duration_ms"]), float(duration))
                    stats["samples_ms"].append(float(duration))
            if event.get("type") != "tool_result":
                continue
            result = payload.get("result") or {}
            data = result.get("data") or {}
            if payload.get("name") == "load_skill" and result.get("ok"):
                name = ((data.get("skill") or {}).get("name"))
                if name and name not in loaded_skills:
                    loaded_skills.append(str(name))
            if payload.get("name") == "get_repo_map":
                repo_map_calls += 1
            reference = data.get("result_reference")
            if reference:
                output_references.append(str(reference))

        context_manager = runtime.context_manager
        estimated_chars = sum(
            len(str(message.get("content", ""))) for message in state.messages
        )
        repo_map = RepoMapBuilder(runtime.workspace).build(max_files=8)
        skills = [item.to_dict() for item in runtime.skill_library.list_summaries()]
        event_usage = _storage_usage(runtime.event_store.root, "*.jsonl")
        result_store = runtime.tool_executor.result_store
        result_usage = (
            _storage_usage(result_store, "tool-result-*.json")
            if result_store
            else {"files": 0, "bytes": 0}
        )
        tool_totals = {
            "calls": sum(item.get("calls", 0) for item in state.tool_stats.values()),
            "successes": sum(
                item.get("successes", 0) for item in state.tool_stats.values()
            ),
            "failures": sum(
                item.get("failures", 0) for item in state.tool_stats.values()
            ),
            "duration_ms": sum(
                item.get("duration_ms", 0) for item in state.tool_stats.values()
            ),
        }
        span_observability = []
        for (kind, lifecycle, hook), stats in sorted(span_totals.items()):
            item = {
                "kind": kind,
                "count": int(stats["count"]),
                "total_duration_ms": round(float(stats["total_duration_ms"]), 3),
                "average_duration_ms": round(
                    float(stats["total_duration_ms"]) / max(int(stats["count"]), 1), 3
                ),
                "max_duration_ms": round(float(stats["max_duration_ms"]), 3),
                "p50_duration_ms": round(_percentile(stats["samples_ms"], 0.50), 3),
                "p95_duration_ms": round(_percentile(stats["samples_ms"], 0.95), 3),
            }
            if lifecycle:
                item["lifecycle"] = lifecycle
            if hook:
                item["hook"] = hook
            span_observability.append(item)
        return {
            "reasoning_profile": _profile_from_effort(state.reasoning_mode),
            "context": {
                "estimated_chars": estimated_chars,
                "max_chars": context_manager.max_context_chars,
                "messages": len(state.messages),
                "compactions": compactions,
                "summary": state.context_summary,
                "summary_meta": state.context_summary_meta,
                "last_build": last_context_built,
            },
            "repo_map": {
                "calls": repo_map_calls,
                "totals": repo_map["totals"],
                "top_files": repo_map["files"],
                "truncated": repo_map["truncated"],
            },
            "skills": {"available": skills, "loaded": loaded_skills},
            "memory": {
                **runtime.memory_store.stats(),
                "recalled": state.recalled_memories,
                "summaries": runtime.summary_store.stats(),
            },
            "user_memory": {
                **runtime.user_memory.stats(),
                "recalled": state.recalled_user_memories,
            },
            "cache": {
                "file_summaries": len(runtime.workspace.summary_cache),
                "observed_files": len(runtime.workspace.observations),
            },
            "outputs": {
                "references": output_references[-8:],
                "stored_count": len(output_references),
            },
            "storage": {
                "events": {
                    **event_usage,
                    "max_bytes": runtime.event_store.max_storage_bytes,
                    "max_files": runtime.event_store.max_session_files,
                    "last_prune": list(runtime.event_store.last_prune_diagnostics),
                },
                "tool_results": {
                    **result_usage,
                    "max_bytes": runtime.tool_executor.result_store_max_bytes,
                    "max_files": runtime.tool_executor.result_store_max_files,
                },
            },
            "hooks": {
                "pipeline_enabled": True,
                "pre": len(runtime.tool_executor.hooks.pre),
                "post": len(runtime.tool_executor.hooks.post),
                "verification": len(runtime.tool_executor.hooks.verification),
                "task_end": len(runtime.tool_executor.hooks.task_end),
                "external": len(runtime.tool_executor.hooks.external),
                "diagnostics": list(runtime.hook_config.diagnostics),
            },
            "permissions": {
                "grants": runtime.runner.permission_policy.grants_snapshot(),
                "approval_policy": runtime.runner.permission_policy.approval_mode,
            },
            "verification_config": {
                "commands": list(runtime.verification_config.commands),
                "diagnostics": list(runtime.verification_config.diagnostics),
            },
            "observability": {
                "tool_output_deltas": output_deltas,
                "spans": span_observability,
                "active_spans": len(active_span_ids),
                "cancellation": {
                    "requests": cancel_requests,
                    "completed": len(cancel_latencies),
                    "samples_ms": cancel_latencies[-20:],
                    "average_ms": round(sum(cancel_latencies) / len(cancel_latencies), 3)
                    if cancel_latencies
                    else 0.0,
                    "max_ms": max(cancel_latencies, default=0.0),
                    "p50_ms": round(_percentile(cancel_latencies, 0.50), 3),
                    "p95_ms": round(_percentile(cancel_latencies, 0.95), 3),
                },
            },
            "token_usage": state.token_usage,
            "tool_stats": state.tool_stats,
            "tool_totals": tool_totals,
            "step": state.step,
            "budget": _budget_view(runtime),
            "verification": {
                "fresh": state.verification_is_fresh,
                "successful_sequence": state.last_successful_verification_sequence,
                "evidence": state.verification_evidence,
            },
            "interrupted_tool_calls": state.interrupted_tool_calls,
            "recovery_warnings": state.recovery_warnings,
        }

    @app.post("/api/sessions/{session_id}/memory/candidates/{candidate_id}")
    async def resolve_memory_candidate(
        session_id: str, candidate_id: str, request: MemoryCandidateRequest
    ) -> dict[str, Any]:
        runtime = manager.get(session_id).runtime
        candidate = (
            runtime.summary_store.confirm(candidate_id, runtime.memory_store)
            if request.action == "confirm"
            else runtime.summary_store.reject(candidate_id)
        )
        if candidate is None:
            raise HTTPException(status_code=404, detail="Memory candidate was not found or was already resolved")
        return {"candidate": candidate}

    @app.post("/api/sessions/{session_id}/user-memory/enabled")
    async def set_user_memory_enabled(
        session_id: str, request: UserMemorySettingRequest
    ) -> dict[str, Any]:
        service = manager.get(session_id).runtime.user_memory
        return {"enabled": service.set_enabled(request.enabled)}

    @app.get("/api/sessions/{session_id}/user-memory/export")
    async def export_user_memory(session_id: str) -> dict[str, Any]:
        return manager.get(session_id).runtime.user_memory.export()

    @app.delete("/api/sessions/{session_id}/user-memory")
    async def clear_user_memory(session_id: str) -> dict[str, Any]:
        service = manager.get(session_id).runtime.user_memory
        if not service.enabled:
            raise HTTPException(status_code=409, detail="User memory is disabled")
        return {"cleared": service.clear()}

    @app.get("/api/sessions/{session_id}/file")
    async def read_workspace_file(session_id: str, path: str) -> dict[str, Any]:
        session = manager.get(session_id)
        workspace = session.runtime.workspace
        try:
            target = workspace.resolve(path, must_exist=True)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.is_file():
            raise HTTPException(status_code=400, detail="Path is not a file")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"Cannot read file: {exc}"
            ) from exc
        if b"\x00" in raw[:4096]:
            raise HTTPException(status_code=415, detail="二进制文件暂不支持文本预览")
        content = raw.decode("utf-8", errors="replace")
        limit = 300_000
        truncated = len(content) > limit
        if truncated:
            content = content[:limit]
        return {
            "name": target.name,
            "path": workspace.relative(target),
            "content": content,
            "size": len(raw),
            "line_count": len(content.splitlines()),
            "language": _language_from_path(target),
            "truncated": truncated,
        }

    @app.post("/api/sessions/{session_id}/messages", status_code=202)
    async def send_message(
        session_id: str, request: MessageRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        if session.running:
            terminal_statuses = {
                AgentStatus.COMPLETED,
                AgentStatus.PARTIAL,
                AgentStatus.FAILED,
                AgentStatus.CANCELLED,
            }
            if session.runtime.state.status in terminal_statuses and session.task:
                # turn_finished is delivered just before the task coroutine
                # returns. Bridge that tiny boundary instead of rejecting an
                # immediate follow-up message.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(session.task)
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

        session.last_error = None
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

    @app.post("/api/sessions/{session_id}/reasoning")
    async def set_reasoning(
        session_id: str, request: ReasoningRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        if session.running:
            raise HTTPException(
                status_code=409, detail="Cannot change reasoning while running"
            )
        profile, effort = _reasoning_profile(request.profile)
        session.runtime.state.reasoning_mode = effort
        return {"profile": profile, "reasoning_effort": effort}

    @app.post("/api/sessions/{session_id}/approval-policy")
    async def set_approval_policy(
        session_id: str, request: ApprovalPolicyRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        policy = session.runtime.runner.permission_policy
        previous = policy.approval_mode
        current = policy.set_approval_mode(request.policy)
        pending = session.runtime.state.pending_approval or {}
        pending_call = pending.get("call") or {}
        pending_call_id = str(pending_call.get("id") or "")
        approved_pending = False
        if current is not ApprovalMode.ASK and pending_call_id:
            try:
                session.approval_broker.resolve(pending_call_id, True)
                approved_pending = True
            except KeyError:
                pass
        await session.runtime.event_bus.publish(
            AgentEvent(
                type="approval_policy_changed",
                session_id=session.runtime.state.session_id,
                turn_id=session.runtime.state.turn_id,
                payload={
                    "previous": previous,
                    "policy": current,
                    "approved_pending": approved_pending,
                },
            )
        )
        return {
            "approval_policy": current,
            "previous": previous,
            "approved_pending": approved_pending,
        }

    @app.post("/api/sessions/{session_id}/approval")
    async def resolve_approval(
        session_id: str, request: ApprovalRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        grant_details: dict[str, Any] | None = None
        if request.approved and request.scope == "session":
            state = session.runtime.state
            pending = state.pending_approval or {}
            call = pending.get("call") or {}
            if str(call.get("id") or "") != request.tool_call_id:
                raise HTTPException(status_code=409, detail="Approval is no longer pending")
            if pending.get("redacted"):
                raise HTTPException(
                    status_code=409,
                    detail="Persisted approval data was redacted; resubmit the operation before granting a session scope",
                )
            try:
                spec = session.runtime.registry.get(str(call.get("name") or ""))
                arguments = dict(call.get("arguments") or {})
                spec.validate(arguments)
            except (KeyError, TypeError, ToolError) as exc:
                raise HTTPException(status_code=409, detail=f"Cannot create session grant: {exc}") from exc
            permission = session.runtime.runner.permission_policy.evaluate(
                mode=state.mode, spec=spec, arguments=arguments
            )
            if permission.decision.value == "deny":
                raise HTTPException(status_code=403, detail=permission.reason)
            path_prefix = arguments.get("path") if isinstance(arguments.get("path"), str) else None
            command_prefix = arguments.get("command") if spec.risk.value == "command" else None
            grant = session.runtime.runner.permission_policy.grant(
                permission.capabilities,
                path_prefix=path_prefix,
                command_prefix=command_prefix if isinstance(command_prefix, str) else None,
                ttl_seconds=request.ttl_seconds,
            )
            grant_details = {
                "grant_id": grant.grant_id,
                "capabilities": sorted(grant.capabilities),
                "path_prefix": grant.path_prefix,
                "command_prefix": grant.command_prefix,
                "expires_at": grant.expires_at,
                "tool_name": spec.name,
            }
            await session.runtime.event_bus.publish(
                AgentEvent(
                    type="permission_granted",
                    session_id=state.session_id,
                    turn_id=state.turn_id,
                    payload={
                        "grant_id": grant.grant_id,
                        "capabilities": sorted(grant.capabilities),
                        "path_prefix": grant.path_prefix,
                        "command_prefix": grant.command_prefix,
                        "expires_at": grant.expires_at,
                        "tool_name": spec.name,
                    },
                )
            )
        try:
            session.approval_broker.resolve(request.tool_call_id, request.approved)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="Approval is no longer pending") from exc
        return {
            "resolved": True,
            "approved": request.approved,
            "scope": request.scope,
            **({"grant": grant_details} if grant_details else {}),
        }

    @app.post("/api/sessions/{session_id}/recovery", status_code=202)
    async def recover_interrupted(
        session_id: str, request: RecoveryRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        if session.running:
            raise HTTPException(
                status_code=409, detail="Cannot recover while the agent is running"
            )
        state = session.runtime.state
        interrupted = next(
            (
                item
                for item in state.interrupted_tool_calls
                if str(item.get("id") or "") == request.tool_call_id
            ),
            None,
        )
        if interrupted is None:
            raise HTTPException(status_code=404, detail="Interrupted tool call not found")
        try:
            spec = session.runtime.registry.get(str(interrupted.get("name") or ""))
            spec.validate(dict(interrupted.get("arguments") or {}))
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc

        if request.action == "retry":
            if spec.risk.value != "read" and not request.confirm:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Retrying a write or command may repeat an unknown side effect; "
                        "confirm explicitly"
                    ),
                )
            permission = session.runtime.runner.permission_policy.evaluate(
                mode=state.mode,
                spec=spec,
                arguments=dict(interrupted.get("arguments") or {}),
            )
            if permission.decision.value == "deny":
                raise HTTPException(status_code=403, detail=permission.reason)

            async def execute_retry() -> AgentRunResult:
                try:
                    return await session.runtime.runner.retry_interrupted(
                        state, tool_call_id=request.tool_call_id
                    )
                except Exception as exc:
                    session.last_error = f"{type(exc).__name__}: {exc}"
                    raise

            session.last_error = None
            session.task = asyncio.create_task(execute_retry())

            def consume_retry_result(task: asyncio.Task[AgentRunResult]) -> None:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    session.last_error = f"{type(exc).__name__}: {exc}"

            session.task.add_done_callback(consume_retry_result)
            return {
                "accepted": True,
                "action": request.action,
                "tool_call_id": request.tool_call_id,
            }

        await session.runtime.runner.abandon_interrupted(
            state, tool_call_id=request.tool_call_id
        )
        return {
            "accepted": True,
            "action": request.action,
            "tool_call_id": request.tool_call_id,
        }

    @app.get("/api/sessions/{session_id}/permissions")
    async def list_permissions(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        policy = session.runtime.runner.permission_policy
        return {
            "grants": policy.grants_snapshot(),
            "approval_policy": policy.approval_mode,
        }

    @app.delete("/api/sessions/{session_id}/permissions/{grant_id}")
    async def revoke_permission(session_id: str, grant_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        revoked = session.runtime.runner.permission_policy.revoke(grant_id)
        if not revoked:
            raise HTTPException(status_code=404, detail="Permission grant not found")
        await session.runtime.event_bus.publish(
            AgentEvent(
                type="permission_revoked",
                session_id=session.runtime.state.session_id,
                turn_id=session.runtime.state.turn_id,
                payload={"grant_id": grant_id},
            )
        )
        return {"revoked": True, "grant_id": grant_id}

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        if not session.running:
            return {
                "cancel_requested": False,
                "already_finished": True,
                "running": False,
                "force_after_seconds": WEB_CANCEL_FORCE_SECONDS,
            }
        session.runtime.state.cancel_requested = True
        newly_requested = session.runtime.cancellation.cancel("user_requested")
        session.approval_broker.reject_all()
        if newly_requested:
            await session.runtime.event_bus.publish(
                AgentEvent(
                    type="run_cancel_requested",
                    session_id=session.runtime.state.session_id,
                    turn_id=session.runtime.state.turn_id,
                    payload={"reason": "user_requested"},
                )
            )
        active_task = session.task
        if active_task is not None and not active_task.done() and (
            session.cancel_watchdog is None or session.cancel_watchdog.done()
        ):
            async def enforce_cancel() -> None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(active_task), timeout=WEB_CANCEL_FORCE_SECONDS
                    )
                except TimeoutError:
                    if not active_task.done():
                        active_task.cancel()
                except asyncio.CancelledError:
                    pass

            session.cancel_watchdog = asyncio.create_task(enforce_cancel())
        return {
            "cancel_requested": True,
            "already_requested": not newly_requested,
            "force_after_seconds": WEB_CANCEL_FORCE_SECONDS,
        }

    @app.get("/api/sessions/{session_id}/events")
    async def get_events(session_id: str) -> list[dict[str, Any]]:
        return manager.get(session_id).runtime.event_store.load()

    @app.get("/api/sessions/{session_id}/report")
    async def get_report(session_id: str) -> dict[str, Any]:
        """Return an evidence-focused completion report for CLI/Web consumers."""
        runtime = manager.get(session_id).runtime
        state = runtime.state
        return {
            "session_id": state.session_id,
            "turn_id": state.turn_id,
            "status": state.status,
            "changed_files": sorted(state.changed_files),
            "plan": state.plan,
            "verification": {
                "fresh": state.verification_is_fresh,
                "successful_sequence": state.last_successful_verification_sequence,
                "evidence": state.verification_evidence,
            },
            "repair_attempts": {"used": state.repair_attempts, "max": state.max_repair_attempts},
            "token_usage": state.token_usage,
            "tool_stats": state.tool_stats,
            "context_summary": state.context_summary,
            "budget": _budget_view(runtime),
            "pending_approval": state.pending_approval,
            "interrupted_tool_calls": state.interrupted_tool_calls,
            "recovery_warnings": state.recovery_warnings,
        }

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
        manager = session.runtime.checkpoint_manager
        try:
            preview = manager.preview_restore(state.turn_id)
        except ToolError:
            preview = []
        return {
            "turn_id": state.turn_id,
            "files": manager.list_files(state.turn_id),
            "preview": preview,
        }

    @app.post("/api/sessions/{session_id}/restore")
    async def restore_checkpoint(
        session_id: str, request: RestoreRequest | None = None
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        if session.running:
            raise HTTPException(status_code=409, detail="Cannot restore while Agent is running")
        state = session.runtime.state
        restore_request = request or RestoreRequest()
        checkpoint_manager = session.runtime.checkpoint_manager
        try:
            preview = checkpoint_manager.preview_restore(
                state.turn_id, paths=restore_request.paths
            )
            restored = checkpoint_manager.restore(
                state.turn_id,
                paths=restore_request.paths,
                force=restore_request.force,
                confirmed_hashes=restore_request.confirmed_hashes,
            )
        except ToolError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": exc.message, **exc.data},
            ) from exc
        state.changed_files.clear()
        state.last_successful_verification_sequence = 0
        event = await session.runtime.event_bus.publish(
            AgentEvent(
                type="checkpoint_restored",
                session_id=state.session_id,
                turn_id=state.turn_id,
                payload={
                    "files": restored,
                    "forced": restore_request.force,
                    "preview": preview,
                },
            )
        )
        state.last_mutation_sequence = event.sequence
        return {"restored": restored, "forced": restore_request.force, "preview": preview}

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
        return FileResponse(
            static_root / "index.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/assets/app.bundle.js", include_in_schema=False)
    async def frontend_bundle() -> Response:
        """Serve renderer and application atomically to prevent cache skew."""
        renderer = (static_root / "rendering.js").read_text(encoding="utf-8")
        application = (static_root / "app.js").read_text(encoding="utf-8")
        return Response(
            f"{renderer}\n;{application}",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

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
