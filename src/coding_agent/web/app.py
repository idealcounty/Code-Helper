from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import os
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool

from ..agent_loop import AgentRunResult
from ..cancellation import CancellationToken
from ..algorithm.problem import parse_problem, suggest_boundary_cases
from ..algorithm.coordinator import (
    AlgorithmRunConfig,
    AlgorithmRunCoordinator,
    default_candidate_command,
)
from ..algorithm.reliability import get_report as get_algorithm_report, list_reports, render_markdown
from ..config import (
    AppConfig,
    default_settings_path,
    load_user_settings,
    save_user_settings,
)
from ..events import AgentEvent
from ..model import ModelClient, ToolCall
from ..permissions import ApprovalMode, PermissionResult
from ..repo_map import RepoMapBuilder
from ..replay import build_step_frames
from ..runtime import AgentRuntime, create_runtime, _skills_root
from ..session import AgentStatus
from ..skills import SkillLibrary
from ..trace_export import build_trace
from ..tools.base import ToolError


class SharedPasswordAuthMiddleware:
    """Small HTTP Basic boundary for trusted, low-traffic server deployments."""

    def __init__(self, app: Any, *, username: str, password: str) -> None:
        self.app = app
        self.username = username
        self.password = password

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope.get("type") == "websocket":
            await send(
                {
                    "type": "websocket.close",
                    "code": 4401,
                    "reason": "Authentication required",
                }
            )
            return
        response = Response(
            "Authentication required",
            status_code=401,
            media_type="text/plain",
            headers={"WWW-Authenticate": 'Basic realm="Code Helper", charset="UTF-8"'},
        )
        await response(scope, receive, send)

    def _authorized(self, scope: dict[str, Any]) -> bool:
        headers = dict(scope.get("headers") or [])
        value = headers.get(b"authorization", b"")
        if not value.lower().startswith(b"basic "):
            return False
        try:
            decoded = base64.b64decode(value[6:], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        username, separator, password = decoded.partition(":")
        return bool(separator) and secrets.compare_digest(
            username, self.username
        ) and secrets.compare_digest(password, self.password)


def _server_host() -> str:
    return os.getenv("CODE_HELPER_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _server_port() -> int:
    raw = os.getenv("CODE_HELPER_PORT", "8765").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("CODE_HELPER_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("CODE_HELPER_PORT must be between 1 and 65535")
    return port


def _require_workspace_scope(config: AppConfig, path: Path) -> None:
    allowed_root = config.server_workspace_root
    if allowed_root is None:
        return
    if not path.resolve().is_relative_to(allowed_root.resolve()):
        raise ValueError(
            f"Workspace must stay inside the server workspace root: {allowed_root}"
        )


def _budget_view(runtime: AgentRuntime) -> dict[str, Any]:
    view = dict(runtime.state.run_budget or runtime.run_budget.snapshot())
    view.setdefault("max_steps", runtime.state.max_steps)
    view["configured_max_output_tokens"] = runtime.config.max_output_tokens
    effective = getattr(
        runtime.runner.model_client, "effective_max_output_tokens", None
    )
    view["effective_max_output_tokens"] = (
        effective if isinstance(effective, int) and not isinstance(effective, bool) else None
    )
    return view


def _workflow_view(state: Any, *, include_details: bool = False) -> dict[str, Any]:
    """Expose the turn-scoped workflow state used by the UI and reports.

    The session contract deliberately keeps this object to the three stable
    projection fields. Consumers that render richer explanations (the
    intelligence and report views) can opt into plan-derived details without
    changing the backwards-compatible session shape.
    """

    name = getattr(state, "workflow_name", None)
    name = name if isinstance(name, str) else None
    stage = str(getattr(state, "workflow_stage", None) or "idle")
    loaded_skills = getattr(state, "loaded_skills", set()) or set()
    view = {
        "name": name,
        "stage": stage,
        "loaded_skills": sorted(str(item) for item in loaded_skills),
    }
    if not include_details:
        return view

    plan = getattr(state, "plan", [])
    plan = plan if isinstance(plan, list) else []
    view["acceptance"] = [
        str(item.get("acceptance"))
        for item in plan
        if isinstance(item, dict) and str(item.get("acceptance") or "").strip()
    ][:12]
    view["active_steps"] = [
        str(item.get("step"))
        for item in plan
        if isinstance(item, dict) and item.get("status") == "in_progress"
    ][:3]
    return view


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


def _token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 1.0 if left == right else 0.0
    return len(left & right) / len(left | right)


def _replay_metrics(session: Any) -> dict[str, Any]:
    events = session.runtime.event_store.load()
    compact = [item for event in events if (item := _compact_ui_history_event(event)) is not None]
    frames = build_step_frames(compact)
    tool_calls = sum(len(frame.get("tool_calls") or []) for frame in frames)
    verification = sum(len(frame.get("verification") or []) for frame in frames)
    errors = sum(len(frame.get("errors") or []) for frame in frames)
    tokens = sum(
        int((event.get("payload") or {}).get("estimated_tokens") or 0)
        for event in events
        if event.get("type") == "context_built"
    )
    return {
        "session_id": session.runtime.state.session_id,
        "reasoning": session.runtime.state.reasoning_mode,
        "steps": len([frame for frame in frames if int(frame.get("step") or 0) > 0]),
        "tool_calls": tool_calls,
        "verification_events": verification,
        "errors": errors,
        "estimated_context_tokens": tokens,
        "duration_ms": round(sum(float(frame.get("duration_ms") or 0) for frame in frames), 3),
    }


_OBSERVABILITY_SOURCE_LABELS = {
    "repo_map": ("项目结构", "帮助 Agent 找到与当前任务最相关的文件"),
    "Repo Map": ("项目结构", "帮助 Agent 找到与当前任务最相关的文件"),
    "history": ("最近对话", "保留你刚刚说过的目标和约束"),
    "recent_messages": ("最近对话", "保留你刚刚说过的目标和约束"),
    "summary": ("较早对话摘要", "把更早的内容压缩成可追踪的摘要"),
    "rules": ("项目规则", "遵循工作区中的约定和说明"),
    "core_system": ("安全规则", "保证 Agent 按照系统边界工作"),
    "tool_schemas": ("可用工具", "告诉 Agent 当前可以使用哪些能力"),
    "tools": ("可用工具", "告诉 Agent 当前可以使用哪些能力"),
    "project_memory": ("项目记忆", "复用已经确认过的项目事实"),
    "user_memory": ("你的偏好", "复用你主动启用的个人偏好"),
    "skill_catalog": ("工作技能", "按需加载的 Skills 目录"),
}


def _friendly_source(source: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source.get("id") or source.get("kind") or "unknown")
    label, description = _OBSERVABILITY_SOURCE_LABELS.get(
        source_id, (str(source.get("label") or source_id), "本轮任务使用的参考资料")
    )
    enabled = bool(source.get("enabled", True))
    chars = max(0, int(source.get("chars") or 0))
    return {
        "id": source_id,
        "label": label,
        "description": description,
        "status": "used" if enabled and chars else "available" if enabled else "disabled",
        "status_label": "已参考" if enabled and chars else "可参考" if enabled else "已关闭",
        "reason": str(source.get("reason") or ""),
        "chars": chars,
        "tokens": int(source.get("tokens") or round(chars / 4)),
        "locked": bool(source.get("locked", False)),
    }


def _observability_presentation(view: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build a user-facing explanation without changing raw observability data.

    The adapter deliberately uses only already redacted API data.  It is a
    presentation model: changing these labels or summaries must not change
    Agent execution, permissions, memory persistence, or event semantics.
    """
    if view == "replay":
        raw_steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        simple_steps: list[dict[str, Any]] = []
        for item in raw_steps:
            errors = item.get("errors") if isinstance(item.get("errors"), list) else []
            calls = item.get("tool_calls") if isinstance(item.get("tool_calls"), list) else []
            results = item.get("tool_results") if isinstance(item.get("tool_results"), list) else []
            files = sorted({
                str((call.get("arguments") or {}).get("path"))
                for call in calls
                if isinstance(call, dict) and (call.get("arguments") or {}).get("path")
            })
            if errors:
                status, status_label = "error", "需要关注"
                description = "这一步出现了可观测错误，建议先检查详情。"
            elif results or calls:
                status, status_label = "done", "已完成"
                description = f"Agent 使用了 {len(calls)} 个工具完成这一步。"
            else:
                status, status_label = "info", "已记录"
                description = "这一步完成了上下文准备或模型判断。"
            simple_steps.append({
                "step": int(item.get("step") or 0),
                "turn_id": str(item.get("turn_id") or ""),
                "status": status,
                "status_label": status_label,
                "title": f"第 {int(item.get('step') or 0)} 步",
                "description": description,
                "tool_count": len(calls),
                "files": files[:8],
                "duration_ms": float(item.get("duration_ms") or 0),
                "error_count": len(errors),
                "event_count": len(item.get("events") or []),
            })
        error_count = len(data.get("error_sequences") or [])
        if not simple_steps:
            summary = {"status": "empty", "tone": "neutral", "title": "还没有工作记录", "description": "完成一次任务后，这里会用时间线展示 Agent 做过什么。"}
        elif error_count:
            summary = {"status": "attention", "tone": "warning", "title": "任务中有一步需要关注", "description": f"已记录 {len(simple_steps)} 个步骤，其中 {error_count} 个可观测错误。"}
        else:
            summary = {"status": "ok", "tone": "success", "title": "运行回放", "description": f"已按时间顺序记录 {len(simple_steps)} 个步骤。"}
        return {"mode": "simple", "summary": summary, "steps": simple_steps, "error_count": error_count, "technical_available": bool(raw_steps or data.get("events"))}

    if view == "context":
        raw_sources = data.get("sources") if isinstance(data.get("sources"), list) else []
        sources = [_friendly_source(item) for item in raw_sources if isinstance(item, dict)]
        score = max(0, min(100, int(data.get("quality_score") or 0)))
        if score >= 80:
            budget_state, budget_label, tone = "healthy", "参考资料很充足", "success"
        elif score >= 55:
            budget_state, budget_label, tone = "organized", "参考资料已整理", "info"
        else:
            budget_state, budget_label, tone = "near_limit", "参考资料接近上限", "warning"
        used_chars = int(data.get("actual_context_chars") or data.get("total_chars") or 0)
        max_chars = int(data.get("max_chars") or 0)
        summary = {"status": budget_state, "tone": tone, "title": "上下文编译", "description": f"本轮整理了 {len([item for item in sources if item['status'] == 'used'])} 类资料，确保回答贴合当前项目。"}
        return {
            "mode": "simple",
            "summary": summary,
            "budget": {"state": budget_state, "label": budget_label, "score": score, "used_chars": used_chars, "max_chars": max_chars, "tokens": int(data.get("estimated_tokens") or 0)},
            "sources": sources,
            "quality_issues": [str(item.get("kind") or "") for item in data.get("quality_issues") or [] if isinstance(item, dict)],
            "technical_available": bool(raw_sources or data.get("repo_map")),
        }

    memories = data.get("memories") if isinstance(data.get("memories"), list) else []
    candidates = data.get("pending_candidates") if isinstance(data.get("pending_candidates"), list) else []
    recalls = data.get("recall_audit") if isinstance(data.get("recall_audit"), list) else []
    category_labels = {"fact": "项目事实", "decision": "已做决定", "preference": "你的偏好", "task": "待办事项", "constraint": "项目约束"}
    simple_candidates = [{
        "id": str(item.get("id") or ""),
        "content": str(item.get("content") or ""),
        "category": str(item.get("category") or "fact"),
        "category_label": category_labels.get(str(item.get("category") or "fact"), "待确认信息"),
        "reason": str(item.get("reason") or "这条信息可能对后续任务有帮助"),
        "source": str(item.get("turn_id") or "当前对话"),
        "keywords": [str(keyword) for keyword in item.get("keywords") or []][:8],
        "prompt": str(item.get("prompt") or "") or f"本轮识别到一条可能有用的信息，是否将“{str(item.get('content') or '')}”存入记忆区？",
        "occurrence_count": max(1, int(item.get("occurrence_count") or 1)),
        "work_type": str(item.get("work_type") or ""),
        "source_kind": str(item.get("source_kind") or "legacy"),
    } for item in candidates if isinstance(item, dict)]
    simple_memories = [{
        "id": str(item.get("id") or ""),
        "content": str(item.get("content") or ""),
        "category": str(item.get("category") or "fact"),
        "category_label": category_labels.get(str(item.get("category") or "fact"), "项目记忆"),
        "lifecycle": "已保存" if not item.get("archived") else "已归档",
        "verified": bool(item.get("verification_status") in {"verified", "confirmed"}),
        "pinned": bool(item.get("pinned")),
        "state": "cancelled" if item.get("archived") else "active",
        "action": "restore" if item.get("archived") else "archive",
        "action_label": "重新启用" if item.get("archived") else "取消记忆",
    } for item in memories if isinstance(item, dict)]
    recall_items = []
    for item in recalls[:20]:
        memory = item.get("memory") if isinstance(item, dict) else {}
        if not isinstance(memory, dict):
            continue
        recall_items.append({"content": str(memory.get("content") or ""), "reason": "与当前任务相关，因此被带入本轮", "score": item.get("score")})
    conflict_count = len(data.get("conflicts") or [])
    duplicate_count = len(data.get("duplicates") or []) + len(data.get("candidate_duplicates") or [])
    active_count = sum(item["state"] == "active" for item in simple_memories)
    cancelled_count = len(simple_memories) - active_count
    summary = {"status": "attention" if candidates or conflict_count else "ok", "tone": "warning" if candidates or conflict_count else "success", "title": "记忆治理", "description": f"当前启用 {active_count} 条记忆，已取消 {cancelled_count} 条，另有 {len(simple_candidates)} 条建议等待你决定。"}
    return {
        "mode": "simple",
        "summary": summary,
        "candidates": simple_candidates,
        "memories": simple_memories,
        "recalls": recall_items,
        "conflicts": [{"subject": str(item.get("subject") or "同一主题"), "count": len(item.get("memories") or [])} for item in data.get("conflicts") or [] if isinstance(item, dict)],
        "duplicate_count": duplicate_count,
        "technical_available": bool(memories or candidates or recalls or data.get("conflicts")),
    }


def _bookmark_path(workspace_root: Path) -> Path:
    return workspace_root / ".code-helper" / "replay-bookmarks.json"


def _load_replay_bookmarks(workspace_root: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(_bookmark_path(workspace_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)][:500] if isinstance(payload, list) else []


def _save_replay_bookmarks(workspace_root: Path, bookmarks: list[dict[str, Any]]) -> None:
    path = _bookmark_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(bookmarks[-500:], ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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


_UI_HISTORY_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "turn_started": ("message",),
    "run_cancel_requested": ("reason",),
    "run_cancelled": ("reason",),
    "run_budget_exhausted": ("code", "message"),
    "run_failed": ("code", "message"),
    "approval_policy_changed": ("policy",),
    "step_started": ("step",),
    "task_profile_selected": ("profile", "reason"),
    "skill_loaded": ("name",),
    "workflow_selected": ("name", "stage"),
    "workflow_stage_changed": ("from", "to", "reason"),
    "context_compacted": ("estimated_chars",),
    "model_started": (),
    "stuck_recovery": ("message",),
    "duplicate_write_satisfied": ("message",),
    "stuck_terminal": ("message",),
    "plan_updated": ("plan", "reason"),
    "approval_requested": ("name", "reason"),
    "verification_required": ("reason",),
    "repair_attempt": ("attempt", "max_attempts", "reason"),
    "algorithm_report_ready": ("report_id", "path", "summary", "evidence"),
    "algorithm_report_failed": ("code", "message"),
    "algorithm_run_progress": ("run_id", "stage", "progress", "message", "profile", "completed", "total", "cache", "model_requests"),
    "algorithm_run_completed": ("run_id", "report_id", "status", "summary", "cache", "model_requests", "path"),
    "algorithm_run_cancelled": ("run_id", "status", "code", "message", "profile", "model_requests"),
    "algorithm_run_failed": ("run_id", "report_id", "status", "code", "message", "summary", "cache", "model_requests", "path"),
    "checkpoint_created": ("path",),
    "checkpoint_tracking_failed": ("code", "message"),
    "checkpoint_restored": ("files", "forced"),
    "turn_finished": ("status", "message"),
}


def _compact_ui_history_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Keep only stable, bounded data required to restore the visible UI."""

    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    source = payload if isinstance(payload, dict) else {}
    if event_type == "assistant_response":
        compact_payload = {
            "content": str(source.get("content") or ""),
            "tool_calls": [
                {"name": str(call.get("name") or "")}
                for call in source.get("tool_calls") or []
                if isinstance(call, dict) and call.get("name")
            ],
        }
    elif event_type == "tool_started":
        arguments = source.get("arguments")
        argument_source = arguments if isinstance(arguments, dict) else {}
        compact_payload = {
            "name": str(source.get("name") or ""),
            "arguments": {
                key: argument_source[key]
                for key in ("path", "command", "query", "pattern")
                if key in argument_source
            },
        }
    elif event_type == "tool_result":
        result = source.get("result")
        result_source = result if isinstance(result, dict) else {}
        compact_payload = {
            "name": str(source.get("name") or ""),
            "result": {
                "ok": bool(result_source.get("ok")),
                "code": str(result_source.get("code") or ""),
                "message": str(result_source.get("message") or "")[:1000],
            },
        }
    elif event_type == "context_built":
        repo_map = source.get("repo_map")
        repo_source = repo_map if isinstance(repo_map, dict) else {}
        compact_payload = {
            "estimated_chars": source.get("estimated_chars", 0),
            "estimated_tokens": source.get("estimated_tokens", 0),
            "repo_map_selected_count": len(repo_source.get("selected") or []),
            "source_manifest": [
                {key: item.get(key) for key in ("kind", "chars", "enabled", "locked", "reason")}
                for item in (source.get("source_manifest") or [])[:20]
                if isinstance(item, dict)
            ],
            "snapshot": source.get("snapshot") if isinstance(source.get("snapshot"), dict) else {},
        }
    elif event_type == "verification_recorded":
        evidence = source.get("evidence")
        evidence_source = evidence if isinstance(evidence, dict) else {}
        compact_payload = {
            "evidence": {
                key: evidence_source.get(key)
                for key in ("accepted", "kind", "reason")
            }
        }
    elif event_type in _UI_HISTORY_PAYLOAD_KEYS:
        compact_payload = {
            key: source.get(key) for key in _UI_HISTORY_PAYLOAD_KEYS[event_type]
        }
    else:
        return None
    compact_event = {
        key: event.get(key)
        for key in ("event_id", "session_id", "turn_id", "sequence", "timestamp", "type")
        if key in event
    }
    compact_event["type"] = event_type
    compact_event["payload"] = compact_payload
    return compact_event


_UI_HISTORY_TYPE_MARKERS = tuple(
    f'"type": "{event_type}"'.encode("utf-8")
    for event_type in (
        *_UI_HISTORY_PAYLOAD_KEYS,
        "assistant_response",
        "tool_started",
        "tool_result",
        "context_built",
        "verification_recorded",
    )
)


@lru_cache(maxsize=128)
def _compact_ui_history_from_file(
    path_string: str, mtime_ns: int, size: int
) -> tuple[dict[str, Any], ...]:
    del mtime_ns, size  # These values intentionally invalidate the cache key.
    events: list[dict[str, Any]] = []
    try:
        with Path(path_string).open("rb") as handle:
            for line in handle:
                prefix = line[:256]
                if not any(marker in prefix for marker in _UI_HISTORY_TYPE_MARKERS):
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                compact = _compact_ui_history_event(parsed)
                if compact is not None:
                    events.append(compact)
    except OSError:
        return ()
    return tuple(events)


def _load_session_archive(root: Path) -> dict[str, str]:
    path = root / ".code-helper" / "session-archive.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sessions = payload.get("sessions", {}) if isinstance(payload, dict) else {}
    if not isinstance(sessions, dict):
        return {}
    return {
        str(session_id): str(archived_at)
        for session_id, archived_at in sessions.items()
        if session_id and archived_at
    }


def _save_session_archive(root: Path, sessions: dict[str, str]) -> None:
    path = root / ".code-helper" / "session-archive.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"sessions": sessions}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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


@lru_cache(maxsize=512)
def _session_summary_from_file(
    session_id: str,
    path_string: str,
    updated_at: str,
    mtime_ns: int,
    size: int,
) -> dict[str, Any]:
    del mtime_ns, size  # These values intentionally invalidate the cache key.
    path = Path(path_string)
    first_message = "新对话"
    preview = "尚未发送消息"
    status = "ready"
    try:
        with path.open("rb") as handle:
            for line in handle:
                prefix = line[:256]
                if not any(
                    marker in prefix
                    for marker in (
                        b'"type": "turn_started"',
                        b'"type": "assistant_response"',
                        b'"type": "turn_finished"',
                    )
                ):
                    continue
                event = json.loads(line)
                payload = event.get("payload") or {}
                event_type = event.get("type")
                if (
                    event_type == "turn_started"
                    and first_message == "新对话"
                    and payload.get("message")
                ):
                    message = str(payload["message"]).strip()
                    first_message = message[:42]
                    preview = message[:72]
                elif event_type == "assistant_response" and payload.get("content"):
                    preview = str(payload["content"]).strip()[:72]
                elif event_type == "turn_finished":
                    status = str(payload.get("status", "ready"))
    except (OSError, json.JSONDecodeError):
        pass
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
    mode: Literal["ask", "plan", "act"] | None = None
    session_id: str | None = None
    reasoning_profile: Literal["auto", "fast", "balanced", "deep"] | None = None
    task_profile: Literal["auto", "project", "algorithm"] | None = None
    approval_policy: Literal["ask", "auto", "full"] | None = None


class WorkspaceSessionRequest(BaseModel):
    workspace: str = Field(min_length=1)


class MessageRequest(BaseModel):
    content: str = Field(min_length=1)


class AlgorithmSpecRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class AlgorithmRunRequest(BaseModel):
    """Configuration for a direct, deterministic algorithm-lab run."""

    candidate_command: str = Field(default="", max_length=4_000)
    candidate_path: str = Field(default="", max_length=4_096)
    # ``source_path`` is retained as a descriptive alias used by the roadmap
    # and older clients; new UI code sends ``candidate_path``.
    source_path: str = Field(default="", max_length=4_096)
    oracle_command: str = Field(default="", max_length=4_000)
    problem_text: str = Field(default="", max_length=100_000)
    cases: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    profile: Literal["quick", "standard", "full"] = "standard"
    seed: int = Field(default=0, ge=0, le=2_147_483_647)
    timeout: float | None = Field(default=None, ge=0.1, le=30.0)
    shrink: bool | None = None
    benchmark: bool | None = None


class AlgorithmRetryRequest(BaseModel):
    profile: Literal["quick", "standard", "full"] | None = None


class ReplayForkRequest(BaseModel):
    mode: Literal["ask", "plan"] = "plan"


class ReplayCompareRequest(BaseModel):
    left_session_id: str = Field(min_length=1, max_length=128)
    right_session_id: str = Field(min_length=1, max_length=128)


class ReplayBookmarkRequest(BaseModel):
    turn_id: str = Field(min_length=1, max_length=128)
    step: int = Field(ge=0)
    label: str = Field(default="根因候选", max_length=200)


class ApprovalRequest(BaseModel):
    # ``tool_call_id`` is required for new clients.  Keep it optional at the
    # transport boundary so a cached older UI can still resolve the single
    # pending approval from the server-side session state instead of getting a
    # validation-only 422 response.
    tool_call_id: str | None = Field(default=None, min_length=1)
    approved: StrictBool
    scope: Literal["once", "session"] = "once"
    ttl_seconds: float = Field(default=3600.0, ge=60.0, le=86_400.0)


class ModeRequest(BaseModel):
    mode: Literal["ask", "plan", "act"]


class ReasoningRequest(BaseModel):
    profile: Literal["auto", "fast", "balanced", "deep"]


class ApprovalPolicyRequest(BaseModel):
    policy: Literal["ask", "auto", "full"]


class AppSettingsRequest(BaseModel):
    api_key: str | None = Field(default=None, max_length=512)
    clear_api_key: bool = False
    default_workspace: str = Field(default="", max_length=4096)
    default_mode: Literal["ask", "plan", "act"] = "act"
    default_reasoning_profile: Literal["auto", "fast", "balanced", "deep"] = "auto"
    default_task_profile: Literal["auto", "project", "algorithm"] = "auto"
    default_approval_policy: Literal["ask", "auto", "full"] = "ask"
    default_layout_mode: Literal["editor", "focus"] = "editor"
    enabled_skills: list[str] = Field(default_factory=list, max_length=100)


class RecoveryRequest(BaseModel):
    action: Literal["retry", "abandon"]
    tool_call_id: str = Field(min_length=1)
    confirm: bool = False


class MemoryCandidateRequest(BaseModel):
    action: Literal["confirm", "reject"]


class MemoryBulkResolveRequest(BaseModel):
    action: Literal["confirm", "reject"]
    candidate_ids: list[str] = Field(default_factory=list, max_length=100)
    confirm: bool = False


class MemoryGovernanceRequest(BaseModel):
    action: Literal["pin", "unpin", "archive", "restore", "reweight", "set_expiry", "clear_expiry"]
    importance: int | None = Field(default=None, ge=1, le=5)
    expires_at: str | None = Field(default=None, max_length=64)


class MemoryMergeRequest(BaseModel):
    memory_ids: list[str] = Field(min_length=2, max_length=100)
    category: Literal["fact", "decision", "preference", "task"]
    content: str = Field(min_length=1, max_length=2_000)
    confirm: bool = False


class ContextPreferenceRequest(BaseModel):
    source_id: Literal["repo_map", "project_memory", "user_memory", "skill_catalog"]
    enabled: bool


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
    algorithm_tasks: dict[str, asyncio.Task[dict[str, Any]]] = field(default_factory=dict)
    algorithm_cancellations: dict[str, CancellationToken] = field(default_factory=dict)
    algorithm_configs: dict[str, AlgorithmRunConfig] = field(default_factory=dict)
    algorithm_results: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        _require_workspace_scope(self.config, resolved_workspace)
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
    settings_path: Path | None = None,
    access_username: str | None = None,
    access_password: str | None = None,
) -> FastAPI:
    app = FastAPI(title="Code Helper", version="0.1.0")
    resolved_settings_path = (settings_path or default_settings_path()).expanduser()
    manager = WebSessionManager(
        config or AppConfig.from_env(resolved_settings_path),
        model_client_factory=model_client_factory,
    )
    resolved_access_username = (
        access_username
        if access_username is not None
        else os.getenv("CODE_HELPER_ACCESS_USERNAME", "codehelper")
    ).strip() or "codehelper"
    resolved_access_password = (
        access_password
        if access_password is not None
        else os.getenv("CODE_HELPER_ACCESS_PASSWORD", "") if config is None else ""
    )
    if resolved_access_password:
        app.add_middleware(
            SharedPasswordAuthMiddleware,
            username=resolved_access_username,
            password=resolved_access_password,
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

    def settings_view() -> dict[str, Any]:
        config_view = manager.config
        available = SkillLibrary(_skills_root()).list_summaries()
        enabled = (
            {item.name for item in available}
            if config_view.enabled_skills is None
            else set(config_view.enabled_skills)
        )
        key = config_view.api_key.strip()
        return {
            "api_key_configured": bool(key),
            "api_key_hint": f"••••{key[-4:]}" if key else "",
            "provider": config_view.provider,
            "model": config_view.model,
            "default_workspace": str(config_view.default_workspace or ""),
            "default_mode": config_view.default_mode,
            "default_reasoning_profile": config_view.default_reasoning_profile,
            "default_task_profile": config_view.default_task_profile,
            "default_approval_policy": config_view.default_approval_policy,
            "default_layout_mode": config_view.default_layout_mode,
            "skills": [
                {**item.to_dict(), "enabled": item.name in enabled}
                for item in available
            ],
        }

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return settings_view()

    @app.post("/api/settings")
    async def update_settings(request: AppSettingsRequest) -> dict[str, Any]:
        workspace_text = request.default_workspace.strip()
        workspace: Path | None = None
        if workspace_text:
            try:
                workspace = Path(workspace_text).expanduser().resolve(strict=True)
            except OSError as exc:
                raise HTTPException(status_code=400, detail=f"默认文件夹不可用：{exc}") from exc
            if not workspace.is_dir():
                raise HTTPException(status_code=400, detail="默认文件夹必须是一个目录")
            try:
                _require_workspace_scope(manager.config, workspace)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        available_names = {
            item.name for item in SkillLibrary(_skills_root()).list_summaries()
        }
        requested_skills = list(dict.fromkeys(request.enabled_skills))
        unknown = sorted(set(requested_skills) - available_names)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"未知 Skills：{', '.join(unknown)}",
            )

        api_key = manager.config.api_key
        supplied_key = (request.api_key or "").strip()
        if request.clear_api_key:
            api_key = ""
        elif supplied_key:
            api_key = supplied_key
        reasoning_effort = REASONING_PROFILES[request.default_reasoning_profile]
        manager.config = replace(
            manager.config,
            api_key=api_key,
            reasoning_effort=reasoning_effort,
            default_workspace=workspace,
            default_mode=request.default_mode,
            default_reasoning_profile=request.default_reasoning_profile,
            default_task_profile=request.default_task_profile,
            default_approval_policy=request.default_approval_policy,
            default_layout_mode=request.default_layout_mode,
            enabled_skills=tuple(requested_skills),
        )

        persisted = load_user_settings(resolved_settings_path)
        if request.clear_api_key:
            persisted["api_key"] = ""
        elif supplied_key:
            persisted["api_key"] = supplied_key
        persisted.update({
            "default_workspace": str(workspace or ""),
            "default_mode": request.default_mode,
            "default_reasoning_profile": request.default_reasoning_profile,
            "default_task_profile": request.default_task_profile,
            "default_approval_policy": request.default_approval_policy,
            "default_layout_mode": request.default_layout_mode,
            "enabled_skills": requested_skills,
        })
        try:
            save_user_settings(persisted, resolved_settings_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法保存设置：{exc}") from exc
        return settings_view()

    @app.get("/api/fs/browse")
    async def browse_directories(path: str = "") -> dict[str, Any]:
        allowed_root = manager.config.server_workspace_root
        if not path:
            if allowed_root is not None:
                try:
                    directory = allowed_root.expanduser().resolve(strict=True)
                    if not directory.is_dir():
                        raise ValueError("Server workspace root is not a directory")
                    entries = _directory_entries(directory)[:300]
                except (OSError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot browse server workspace root: {exc}",
                    ) from exc
                return {
                    "path": str(directory),
                    "parent": None,
                    "entries": [
                        {"name": child.name, "path": str(child), "kind": "directory"}
                        for child in entries
                    ],
                }
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
            _require_workspace_scope(manager.config, directory)
            entries = _directory_entries(directory)[:300]
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Cannot browse directory: {exc}"
            ) from exc
        parent = directory.parent if directory.parent != directory else None
        if allowed_root is not None and directory == allowed_root.resolve():
            parent = None
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
            _require_workspace_scope(manager.config, root)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not root.is_dir():
            raise HTTPException(status_code=400, detail="Workspace is not a directory")

        summaries: dict[str, dict[str, Any]] = {}
        archive = _load_session_archive(root)
        session_root = root / ".code-helper" / "sessions"
        if session_root.exists():
            for event_path in session_root.glob("*.jsonl"):
                stat = event_path.stat()
                updated = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
                summaries[event_path.stem] = _session_summary_from_file(
                    event_path.stem,
                    str(event_path),
                    updated,
                    stat.st_mtime_ns,
                    stat.st_size,
                )
        for session_id, session in manager.sessions.items():
            if session.runtime.workspace.root != root or session_id in summaries:
                continue
            summaries[session_id] = _session_summary(
                session_id, [], datetime.now(UTC).isoformat()
            )
        active_sessions = sorted(
            (
                summary
                for session_id, summary in summaries.items()
                if session_id not in archive
            ),
            key=lambda item: item["updated_at"],
            reverse=True,
        )[:50]
        archived_sessions = sorted(
            (
                {**summary, "archived_at": archive[session_id]}
                for session_id, summary in summaries.items()
                if session_id in archive
            ),
            key=lambda item: item["archived_at"],
            reverse=True,
        )[:50]
        return {
            "workspace": str(root),
            "sessions": active_sessions,
            "archived_sessions": archived_sessions,
        }

    def resolve_workspace_session(
        session_id: str, workspace: str
    ) -> tuple[Path, WebSession | None]:
        if (
            not session_id
            or len(session_id) > 128
            or not all(
                character.isalnum() or character in "-_" for character in session_id
            )
        ):
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            root = Path(workspace).expanduser().resolve(strict=True)
            _require_workspace_scope(manager.config, root)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not root.is_dir():
            raise HTTPException(status_code=400, detail="Workspace is not a directory")
        live_session = manager.sessions.get(session_id)
        if live_session is not None and live_session.runtime.workspace.root != root:
            raise HTTPException(status_code=404, detail="Session not found")
        event_path = root / ".code-helper" / "sessions" / f"{session_id}.jsonl"
        if live_session is None and not event_path.is_file():
            raise HTTPException(status_code=404, detail="Session not found")
        return root, live_session

    @app.post("/api/workspaces/sessions/{session_id}/archive")
    async def archive_workspace_session(
        session_id: str, request: WorkspaceSessionRequest
    ) -> dict[str, Any]:
        root, live_session = resolve_workspace_session(session_id, request.workspace)
        if live_session is not None and live_session.running:
            raise HTTPException(status_code=409, detail="运行中的对话不能归档")
        archive = _load_session_archive(root)
        archived_at = datetime.now(UTC).isoformat()
        archive[session_id] = archived_at
        try:
            _save_session_archive(root, archive)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法保存归档状态：{exc}") from exc
        return {"session_id": session_id, "archived": True, "archived_at": archived_at}

    @app.post("/api/workspaces/sessions/{session_id}/restore")
    async def restore_workspace_session(
        session_id: str, request: WorkspaceSessionRequest
    ) -> dict[str, Any]:
        root, _ = resolve_workspace_session(session_id, request.workspace)
        archive = _load_session_archive(root)
        if session_id not in archive:
            raise HTTPException(status_code=404, detail="Archived session not found")
        archive.pop(session_id)
        try:
            _save_session_archive(root, archive)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法保存归档状态：{exc}") from exc
        return {"session_id": session_id, "archived": False}

    @app.post("/api/sessions")
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        try:
            session = manager.create(
                request.workspace,
                request.mode or manager.config.default_mode,
                request.session_id,
                request.reasoning_profile or manager.config.default_reasoning_profile,
                request.task_profile or manager.config.default_task_profile,
                request.approval_policy or manager.config.default_approval_policy,
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
            "workflow": _workflow_view(state),
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
        loaded_skills: list[str] = sorted(str(item) for item in (state.loaded_skills or set()))
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
            "workflow": _workflow_view(state, include_details=True),
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
                "rules": [
                    rule.to_dict() for rule in runtime.verification_config.rules
                ],
                "active_commands": list(
                    runtime.verification_config.commands_for_state(runtime.state)
                ),
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

    @app.get("/api/sessions/{session_id}/trace")
    async def get_trace(session_id: str) -> dict[str, Any]:
        """Return a redacted Chrome Trace export of the session's spans."""
        session = manager.get(session_id)
        return build_trace(session.runtime.event_store.load())

    @app.get("/api/sessions/{session_id}/agent-lab/replay")
    @app.get("/api/sessions/{session_id}/replay")
    async def get_agent_replay(session_id: str) -> dict[str, Any]:
        """Return a bounded, redacted event timeline grouped by Agent step."""
        session = manager.get(session_id)
        compact_events = [
            compact
            for event in session.runtime.event_store.load()
            if (compact := _compact_ui_history_event(event)) is not None
        ]
        frames = build_step_frames(compact_events)
        # Include both terminal failures and the first failed tool result so
        # the replay UI can distinguish a root-cause event from later
        # cascading ``run_failed`` records.
        errors = [
            int(event.get("sequence") or 0)
            for event in compact_events
            if event.get("type") in {"run_failed", "run_budget_exhausted", "algorithm_report_failed"}
        ]
        errors.extend(
            int(error.get("sequence") or 0)
            for frame in frames
            for error in (frame.get("errors") or [])
            if error.get("type") == "tool_result_failed"
        )
        errors = sorted(set(errors))
        return {
            "steps": frames,
            "events": compact_events[-1200:],
            "error_sequences": errors[-50:],
            "bookmarks": [
                item for item in _load_replay_bookmarks(session.runtime.workspace.root)
                if item.get("session_id") == session_id
            ],
            "evidence": {"level": "event_log", "kind": "redacted_ui_history"},
        }

    @app.post("/api/replay/compare")
    async def compare_replays(request: ReplayCompareRequest) -> dict[str, Any]:
        left = manager.get(request.left_session_id)
        right = manager.get(request.right_session_id)
        if left.runtime.workspace.root.resolve() != right.runtime.workspace.root.resolve():
            raise HTTPException(status_code=409, detail="Replay comparison requires the same workspace")
        left_metrics = _replay_metrics(left)
        right_metrics = _replay_metrics(right)
        return {
            "left": left_metrics,
            "right": right_metrics,
            "delta": {
                key: right_metrics[key] - left_metrics[key]
                for key in ("steps", "tool_calls", "verification_events", "errors", "estimated_context_tokens", "duration_ms")
            },
            "evidence": {"level": "event_log", "kind": "aligned_session_metrics", "disclaimer": "仅比较可观测执行路径，不比较私有思维文本。"},
        }

    @app.post("/api/sessions/{session_id}/replay/bookmarks")
    async def add_replay_bookmark(
        session_id: str, request: ReplayBookmarkRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        bookmark = {
            "id": secrets.token_hex(12),
            "session_id": session_id,
            "turn_id": request.turn_id,
            "step": request.step,
            "label": request.label,
            "created_at": datetime.now(UTC).isoformat(),
        }
        bookmarks = _load_replay_bookmarks(session.runtime.workspace.root)
        bookmarks.append(bookmark)
        _save_replay_bookmarks(session.runtime.workspace.root, bookmarks)
        return {"bookmark": bookmark}

    @app.get("/api/sessions/{session_id}/replay/turns/{turn_id}/steps/{step}")
    async def get_replay_step(session_id: str, turn_id: str, step: int) -> dict[str, Any]:
        session = manager.get(session_id)
        compact_events = [
            compact
            for event in session.runtime.event_store.load()
            if (compact := _compact_ui_history_event(event)) is not None
        ]
        frame = next(
            (
                item
                for item in build_step_frames(compact_events)
                if item.get("turn_id") == turn_id and int(item.get("step") or 0) == step
            ),
            None,
        )
        if frame is None:
            raise HTTPException(status_code=404, detail="Replay step was not found")
        return {"step": frame, "evidence": {"level": "event_log", "kind": "redacted_step_frame"}}

    @app.post("/api/sessions/{session_id}/replay/turns/{turn_id}/steps/{step}/fork")
    async def fork_replay_step(
        session_id: str, turn_id: str, step: int, request: ReplayForkRequest
    ) -> dict[str, Any]:
        """Create a safe Context Fork without replaying commands or writes."""
        source = manager.get(session_id)
        events = source.runtime.event_store.load()
        target_sequence = 0
        frames = build_step_frames([
            compact
            for event in events
            if (compact := _compact_ui_history_event(event)) is not None
        ])
        frame = next(
            (
                item
                for item in frames
                if item.get("turn_id") == turn_id and int(item.get("step") or 0) == step
            ),
            None,
        )
        if frame is None:
            raise HTTPException(status_code=404, detail="Replay step was not found")
        target_sequence = int(frame.get("finished_sequence") or 0)
        try:
            fork = manager.create(
                str(source.runtime.workspace.root),
                request.mode,
                reasoning_profile=_profile_from_effort(source.runtime.state.reasoning_mode),
                task_profile=source.runtime.state.requested_task_profile,
                approval_policy="ask",
            )
            fork_events = [event for event in events if int(event.get("sequence") or 0) <= target_sequence]
            fork_messages: list[dict[str, Any]] = []
            for event in fork_events:
                payload = event.get("payload") or {}
                event_type = event.get("type")
                if event_type == "turn_started" and payload.get("message"):
                    fork_messages.append({"role": "user", "content": str(payload["message"])})
                elif event_type == "assistant_response" and payload.get("content"):
                    fork_messages.append({"role": "assistant", "content": str(payload["content"])})
            fork.runtime.state.messages = fork_messages
            fork.runtime.state.plan = list(source.runtime.state.plan)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "session_id": fork.runtime.state.session_id,
            "workspace": str(fork.runtime.workspace.root),
            "mode": request.mode,
            "source": {"session_id": session_id, "turn_id": turn_id, "step": step, "sequence": target_sequence},
            "safety": {"replayed_writes": False, "replayed_commands": False, "approval_policy": "ask"},
        }

    @app.get("/api/sessions/{session_id}/context-compiler")
    async def get_context_compiler(session_id: str) -> dict[str, Any]:
        """Expose the latest context build as an explainable, read-only view."""
        session = manager.get(session_id)
        events = session.runtime.event_store.load()
        latest = next(
            (event for event in reversed(events) if event.get("type") == "context_built"),
            None,
        )
        payload = (latest or {}).get("payload") or {}
        repo_map = payload.get("repo_map") or {}
        selected = repo_map.get("selected") or []
        rules = payload.get("rule_sources") or []
        sources: list[dict[str, Any]] = [
            {"id": "history", "label": "最近对话", "chars": sum(len(str(item.get("content") or "")) for item in session.runtime.state.messages), "enabled": True, "reason": "保留当前任务的最近消息"},
            {"id": "repo_map", "label": "Repo Map", "chars": int(repo_map.get("selected_chars") or 0), "enabled": not bool(repo_map.get("disabled_by_config") or repo_map.get("disabled_by_profile")), "reason": "按任务相关性和导入中心性排序"},
            {"id": "rules", "label": "项目规则", "chars": int(payload.get("rule_chars") or 0), "enabled": bool(rules), "reason": "工作区规则文件匹配当前路径"},
            {"id": "summary", "label": "历史摘要", "chars": len(str((payload.get("summary_meta") or {}).get("summary") or "")), "enabled": bool(session.runtime.state.context_summary), "reason": "上下文超预算时压缩旧消息"},
            {"id": "tools", "label": "Tools Schema", "chars": len(str(session.runtime.registry.schemas())), "enabled": True, "reason": "当前模式允许的工具描述"},
        ]
        manifest = payload.get("source_manifest")
        if isinstance(manifest, list) and manifest:
            sources = [
                {
                    "id": str(item.get("kind") or "unknown"),
                    "label": str(item.get("kind") or "unknown"),
                    "chars": max(0, int(item.get("chars") or 0)),
                    "tokens": round(max(0, int(item.get("chars") or 0)) / 4),
                    "enabled": bool(item.get("enabled", True)),
                    "locked": bool(item.get("locked", False)),
                    "reason": str(item.get("reason") or ""),
                }
                for item in manifest
                if isinstance(item, dict)
            ] or sources
        configured = {
            "repo_map": bool(session.runtime.context_manager.repo_map_enabled),
            "project_memory": bool(session.runtime.context_manager.project_memory_enabled),
            "user_memory": bool(session.runtime.context_manager.user_memory_enabled),
            "skill_catalog": bool(session.runtime.context_manager.skill_catalog_enabled),
        }
        for source in sources:
            source_id = str(source.get("id") or "")
            if source_id in configured:
                source["enabled"] = configured[source_id]
                source["configured_enabled"] = configured[source_id]
        existing_source_ids = {str(item.get("id") or "") for item in sources}
        for source_id, enabled in configured.items():
            if source_id not in existing_source_ids:
                sources.append({"id": source_id, "label": source_id, "chars": 0, "tokens": 0, "enabled": enabled, "locked": False, "reason": "最近一次构建没有注入该来源"})
        total_chars = sum(max(0, int(item["chars"])) for item in sources if item["enabled"])
        max_chars = max(1, int(session.runtime.context_manager.max_context_chars))
        original_history_chars = sum(
            len(str(message.get("content") or ""))
            for message in session.runtime.state.messages
            if isinstance(message, dict)
        )
        actual_context_chars = max(0, int(payload.get("estimated_chars") or 0))
        score = max(0, min(100, round(100 - max(0, total_chars - max_chars) / max_chars * 100)))
        normalized_messages = [" ".join(str(item.get("content") or "").casefold().split()) for item in session.runtime.state.messages if str(item.get("content") or "").strip()]
        duplicate_messages = len(normalized_messages) - len(set(normalized_messages))
        quality_issues: list[dict[str, Any]] = []
        if duplicate_messages:
            quality_issues.append({"kind": "duplicate_recent_message", "count": duplicate_messages, "penalty": min(15, duplicate_messages * 3)})
        if bool(repo_map.get("truncated")):
            quality_issues.append({"kind": "repo_map_truncated", "count": 1, "penalty": 5})
        score = max(0, score - sum(int(item["penalty"]) for item in quality_issues))
        enabled_count = sum(1 for item in sources if item.get("enabled", True))
        quality_breakdown = {
            "relevance": 30 if selected else 20,
            "freshness": 20 if latest else 8,
            "protocol_completeness": 20 if any(item.get("id") in {"tool_schemas", "tools"} for item in sources) else 8,
            "budget_balance": round(15 * score / 100),
            "traceability": min(15, 5 + enabled_count * 2),
        }
        return {
            "sources": sources,
            "total_chars": total_chars,
            "actual_context_chars": actual_context_chars,
            "original_history_chars": original_history_chars,
            "estimated_tokens": int(payload.get("estimated_tokens") or 0),
            "max_chars": max_chars,
            "quality_score": score,
            "quality_breakdown": quality_breakdown,
            "quality_issues": quality_issues,
            "repo_map": {"selected": selected, "truncated": bool(repo_map.get("truncated"))},
            "evidence": {"level": "observed", "kind": "context_built_event" if latest else "no_context_snapshot"},
        }

    @app.get("/api/sessions/{session_id}/context/builds")
    async def list_context_builds(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        builds: list[dict[str, Any]] = []
        for event in session.runtime.event_store.load():
            if event.get("type") != "context_built":
                continue
            payload = event.get("payload") or {}
            builds.append({
                "build_id": str(event.get("event_id") or event.get("sequence") or ""),
                "sequence": int(event.get("sequence") or 0),
                "turn_id": str(event.get("turn_id") or ""),
                "timestamp": event.get("timestamp"),
                "estimated_chars": int(payload.get("estimated_chars") or 0),
                "estimated_tokens": int(payload.get("estimated_tokens") or 0),
                "segments": payload.get("source_manifest") or [],
            })
        return {"builds": builds[-100:], "total": len(builds), "evidence": {"level": "event_log", "kind": "context_build_manifest"}}

    @app.get("/api/sessions/{session_id}/context/builds/{build_id}")
    async def get_context_build(session_id: str, build_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        for event in reversed(session.runtime.event_store.load()):
            if event.get("type") == "context_built" and str(event.get("event_id") or event.get("sequence") or "") == build_id:
                return {"build_id": build_id, "sequence": event.get("sequence"), "turn_id": event.get("turn_id"), "timestamp": event.get("timestamp"), "manifest": event.get("payload") or {}, "evidence": {"level": "event_log", "kind": "context_build_manifest"}}
        raise HTTPException(status_code=404, detail="Context build was not found")

    @app.get("/api/sessions/{session_id}/context/preferences")
    async def get_context_preferences(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        manager_state = session.runtime.context_manager
        return {
            "sources": {
                "repo_map": {
                    "enabled": bool(manager_state.repo_map_enabled),
                    "locked": False,
                    "reason": "可关闭以比较上下文选择差异；核心系统提示和最近消息不可关闭",
                },
                "project_memory": {"enabled": bool(manager_state.project_memory_enabled), "locked": False, "reason": "控制后续构建是否召回项目记忆"},
                "user_memory": {"enabled": bool(manager_state.user_memory_enabled), "locked": False, "reason": "控制后续构建是否注入已启用的用户记忆"},
                "skill_catalog": {"enabled": bool(manager_state.skill_catalog_enabled), "locked": False, "reason": "控制后续构建是否包含 Skills 目录摘要"},
                "core_system": {"enabled": True, "locked": True},
                "recent_messages": {"enabled": True, "locked": True},
                "tool_schemas": {"enabled": True, "locked": True},
            },
            "evidence": {"level": "runtime", "kind": "context_preferences"},
        }

    @app.put("/api/sessions/{session_id}/context/preferences")
    async def set_context_preference(
        session_id: str, request: ContextPreferenceRequest
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        manager_state = session.runtime.context_manager
        attribute = {
            "repo_map": "repo_map_enabled",
            "project_memory": "project_memory_enabled",
            "user_memory": "user_memory_enabled",
            "skill_catalog": "skill_catalog_enabled",
        }[request.source_id]
        setattr(manager_state, attribute, bool(request.enabled))
        return await get_context_preferences(session_id)

    @app.post("/api/sessions/{session_id}/context-compiler/what-if")
    @app.post("/api/sessions/{session_id}/context/shadow-build")
    async def context_compiler_what_if(session_id: str) -> dict[str, Any]:
        """Return a deterministic estimate of the last context with Repo Map removed."""
        current = await get_context_compiler(session_id)
        sources = list(current.get("sources") or [])
        without_map = [item for item in sources if item.get("id") not in {"repo_map", "Repo Map"}]
        current_chars = sum(int(item.get("chars") or 0) for item in sources if item.get("enabled", True))
        shadow_chars = sum(int(item.get("chars") or 0) for item in without_map if item.get("enabled", True))
        return {
            "current": {"total_chars": current_chars, "estimated_tokens": round(current_chars / 4)},
            "without_repo_map": {"total_chars": shadow_chars, "estimated_tokens": round(shadow_chars / 4)},
            "delta": {"chars": shadow_chars - current_chars, "tokens": round((shadow_chars - current_chars) / 4)},
            "sources": without_map,
            "evidence": {"level": "estimated", "kind": "deterministic_shadow_context", "disclaimer": "这是基于最近一次构建的 What-if 估算，不会发起模型请求。"},
        }

    @app.get("/api/sessions/{session_id}/memory-governance")
    @app.get("/api/sessions/{session_id}/memory/governance")
    async def get_memory_governance(session_id: str) -> dict[str, Any]:
        """Return memory candidates, duplicates and conflicts for human review."""
        runtime = manager.get(session_id).runtime
        memories = runtime.memory_store.list(limit=None)
        duplicate_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for memory in memories:
            key = (memory.category, " ".join(memory.content.casefold().split()))
            duplicate_groups.setdefault(key, []).append(memory.to_dict())
        duplicates = [group for group in duplicate_groups.values() if len(group) > 1]
        conflicts: list[dict[str, Any]] = []
        by_subject: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for memory in memories:
            if memory.subject:
                by_subject.setdefault((memory.category, memory.subject.casefold()), []).append(memory.to_dict())
        for (category, subject), group in by_subject.items():
            if len(group) > 1 and len({item["content"].casefold() for item in group}) > 1:
                conflicts.append({"category": category, "subject": subject, "memories": group})
        pending = runtime.summary_store.candidates(status="pending", limit=100)
        candidate_duplicates: list[list[dict[str, Any]]] = []
        for candidate in pending:
            words = set(str(candidate.get("content") or "").casefold().split())
            match = next(
                (
                    group for group in candidate_duplicates
                    if _token_similarity(words, set(str(group[0].get("content") or "").casefold().split())) >= 0.72
                    and str(group[0].get("category")) == str(candidate.get("category"))
                ),
                None,
            )
            if match is None:
                candidate_duplicates.append([candidate])
            else:
                match.append(candidate)
        candidate_duplicates = [group for group in candidate_duplicates if len(group) > 1]
        return {
            "memories": [item.to_dict() for item in memories[:200]],
            "pending_candidates": pending,
            "duplicates": duplicates,
            "candidate_duplicates": candidate_duplicates,
            "conflicts": conflicts,
            "recalled": runtime.state.recalled_memories,
            "stats": runtime.memory_store.stats(),
            "recall_audit": [
                {
                    **{key: value for key, value in item.items() if key != "memory"},
                    "memory": (item.get("memory") or {}).to_dict() if hasattr(item.get("memory"), "to_dict") else item.get("memory"),
                }
                for item in (runtime.state.recalled_memories or [])
                if isinstance(item, dict)
            ],
            "evidence": {"level": "observed", "kind": "append_only_memory_store", "disclaimer": "记忆变更仍需人工确认。"},
        }

    @app.get("/api/sessions/{session_id}/observability/{view}")
    async def get_observability_presentation(session_id: str, view: str) -> dict[str, Any]:
        """Return a plain-language presentation model for the research views.

        ``raw`` remains available through each original endpoint.  This
        additive route lets the UI default to approachable explanations while
        keeping every technical field available behind a professional toggle.
        """
        if view not in {"replay", "context", "memory"}:
            raise HTTPException(status_code=404, detail="Unknown observability view")
        if view == "replay":
            raw = await get_agent_replay(session_id)
        elif view == "context":
            raw = await get_context_compiler(session_id)
        else:
            raw = await get_memory_governance(session_id)
        return {
            "view": view,
            "presentation": _observability_presentation(view, raw),
            "raw": raw,
            "evidence": raw.get("evidence") or {"level": "observed"},
        }

    @app.post("/api/sessions/{session_id}/algorithm-lab/spec")
    @app.post("/api/sessions/{session_id}/algorithm-lab/spec/parse")
    async def parse_algorithm_spec(
        session_id: str, request: AlgorithmSpecRequest
    ) -> dict[str, Any]:
        """Parse a problem statement for the reliability workbench.

        This endpoint is deliberately conservative: the parser extracts
        headings and constraint-like lines, but never claims a proof or
        executes user supplied code.
        """
        manager.get(session_id)
        spec = parse_problem(request.text)
        confidence = 0.35
        if spec.constraints:
            confidence += 0.35
        if spec.input_description and spec.output_description:
            confidence += 0.2
        if spec.examples:
            confidence += 0.1
        return {
            "spec": spec.to_dict(),
            "suggested_cases": suggest_boundary_cases(spec),
            "evidence": {
                "level": "estimated",
                "kind": "deterministic_parser",
                "confidence": round(min(confidence, 1.0), 2),
                "disclaimer": "解析结果用于生成测试建议，不替代人工审阅。",
            },
        }

    @app.post("/api/sessions/{session_id}/algorithm-lab/cases")
    async def generate_algorithm_cases(
        session_id: str, request: AlgorithmSpecRequest
    ) -> dict[str, Any]:
        manager.get(session_id)
        spec = parse_problem(request.text)
        return {
            "spec": spec.to_dict(),
            "cases": suggest_boundary_cases(spec, limit=32),
            "evidence": {"level": "estimated", "kind": "deterministic_boundary_generator", "disclaimer": "边界输入是保守建议，完整题型仍需人工补充。"},
        }

    @app.post("/api/sessions/{session_id}/algorithm-lab/runs", status_code=202)
    async def start_algorithm_run(
        session_id: str, request: AlgorithmRunRequest
    ) -> dict[str, Any]:
        """Start a direct deterministic algorithm run.

        This endpoint intentionally does not call the model.  The explicit
        button action is the user's authorization to run the supplied
        candidate/Oracle commands inside the selected workspace.
        """

        session = manager.get(session_id)
        if session.running:
            raise HTTPException(status_code=409, detail="Cannot start an algorithm run while Agent is running")
        candidate_command = request.candidate_command.strip()
        candidate_path = (request.candidate_path.strip() or request.source_path.strip())
        if not candidate_command and candidate_path:
            candidate_command = default_candidate_command(candidate_path)
        if not candidate_command:
            raise HTTPException(status_code=400, detail="candidate_command or candidate_path is required")
        if not request.oracle_command.strip() and not request.cases:
            raise HTTPException(status_code=400, detail="Provide oracle_command with problem_text, or explicit expected-output cases")
        config = AlgorithmRunConfig(
            candidate_command=candidate_command,
            oracle_command=request.oracle_command.strip(),
            candidate_path=candidate_path,
            cases=tuple(dict(item) for item in request.cases if isinstance(item, dict)),
            problem_text=request.problem_text,
            profile=request.profile,
            seed=request.seed,
            timeout=request.timeout,
            shrink=request.shrink,
            benchmark=request.benchmark,
        )
        token = CancellationToken()
        async def on_algorithm_progress(payload: dict[str, Any]) -> None:
            current = session.algorithm_results.setdefault(run_id, {"run_id": run_id})
            current.update(payload)
            current["status"] = "running" if payload.get("stage") not in {"completed", "failed", "cancelled"} else str(payload.get("stage"))

        coordinator = AlgorithmRunCoordinator(
            workspace=session.runtime.workspace,
            event_bus=session.runtime.event_bus,
            session_id=session.runtime.state.session_id,
            cancellation=token,
            progress_callback=on_algorithm_progress,
        )
        run_id = coordinator.run_id
        session.algorithm_cancellations[run_id] = token
        session.algorithm_configs[run_id] = config
        session.algorithm_results[run_id] = {
            "run_id": run_id,
            "status": "queued",
            "profile": request.profile,
            "model_requests": 0,
        }

        async def execute() -> dict[str, Any]:
            session.algorithm_results[run_id] = {
                "run_id": run_id,
                "status": "running",
                "profile": request.profile,
                "model_requests": 0,
            }
            result = await coordinator.run(config)
            session.algorithm_results[run_id] = result
            return result

        task = asyncio.create_task(execute(), name=f"algorithm-run-{run_id}")
        session.algorithm_tasks[run_id] = task

        def finish(done: asyncio.Task[dict[str, Any]]) -> None:
            session.algorithm_tasks.pop(run_id, None)
            try:
                result = done.result()
            except asyncio.CancelledError:
                session.algorithm_results[run_id] = {
                    "run_id": run_id,
                    "status": "cancelled",
                    "profile": request.profile,
                    "code": "CANCELLED",
                    "message": "task cancelled",
                    "model_requests": 0,
                }
            except Exception as exc:  # coordinator normally serializes failures
                session.algorithm_results[run_id] = {
                    "run_id": run_id,
                    "status": "failed",
                    "profile": request.profile,
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "model_requests": 0,
                }
            else:
                session.algorithm_results[run_id] = result

        task.add_done_callback(finish)
        return {
            "accepted": True,
            "run_id": run_id,
            "status": "queued",
            "profile": request.profile,
            "model_requests": 0,
            "message": "算法实验已启动，后续阶段通过事件流实时更新",
        }

    @app.get("/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/status")
    async def get_algorithm_run_status(session_id: str, run_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        result = session.algorithm_results.get(run_id)
        if result is not None:
            return {"run": result, "running": run_id in session.algorithm_tasks}
        report = get_algorithm_report(session.runtime.workspace.root, run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Algorithm run was not found")
        failed = int((report.get("summary") or {}).get("failed") or 0)
        status = "failed" if failed else "completed"
        return {"run": {"run_id": run_id, "status": status, "report_id": run_id, "report": report, "model_requests": 0}, "running": False}

    @app.post("/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/cancel")
    async def cancel_algorithm_run(session_id: str, run_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        task = session.algorithm_tasks.get(run_id)
        token = session.algorithm_cancellations.get(run_id)
        if task is None or token is None:
            result = session.algorithm_results.get(run_id)
            return {"cancel_requested": False, "already_finished": True, "status": (result or {}).get("status", "unknown")}
        newly_requested = token.cancel("user_requested")
        return {"cancel_requested": True, "already_requested": not newly_requested, "run_id": run_id}

    @app.post("/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/retry", status_code=202)
    async def retry_algorithm_run(
        session_id: str, run_id: str, request: AlgorithmRetryRequest | None = None
    ) -> dict[str, Any]:
        session = manager.get(session_id)
        config = session.algorithm_configs.get(run_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Original algorithm run configuration is unavailable")
        replacement = request.profile if request and request.profile else config.profile
        retry_config = AlgorithmRunConfig(
            candidate_command=config.candidate_command,
            oracle_command=config.oracle_command,
            candidate_path=config.candidate_path,
            cases=config.cases,
            problem_text=config.problem_text,
            profile=replacement,
            seed=config.seed,
            timeout=config.timeout,
            shrink=config.shrink,
            benchmark=config.benchmark,
        )
        payload = AlgorithmRunRequest(
            candidate_command=retry_config.candidate_command,
            candidate_path=retry_config.candidate_path,
            oracle_command=retry_config.oracle_command,
            problem_text=retry_config.problem_text,
            cases=list(retry_config.cases),
            profile=retry_config.profile,
            seed=retry_config.seed,
            timeout=retry_config.timeout,
            shrink=retry_config.shrink,
            benchmark=retry_config.benchmark,
        )
        return await start_algorithm_run(session_id, payload)

    @app.get("/api/sessions/{session_id}/algorithm-lab/runs")
    async def get_algorithm_runs(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        reports = list_reports(session.runtime.workspace.root)
        active = [
            item for item in session.algorithm_results.values()
            if item.get("status") in {"queued", "running"}
        ]
        return {
            "runs": reports,
            "total": len(reports),
            "active_runs": active,
            "evidence": {"level": "deterministic", "kind": "persisted_judge_reports"},
        }

    @app.get("/api/sessions/{session_id}/algorithm-lab/runs/{run_id}")
    async def get_algorithm_run(session_id: str, run_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        # Keep the documented detail route useful while a run is still active.
        # Older clients expect ``report`` for completed runs; active clients
        # can consume the same route without having to know the ``/status``
        # compatibility suffix.
        active = session.algorithm_results.get(run_id)
        if active is not None:
            return {"run": active, "running": run_id in session.algorithm_tasks}
        report = get_algorithm_report(session.runtime.workspace.root, run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Algorithm report was not found")
        return {"report": report}

    @app.get("/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/markdown")
    @app.get("/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/report.md")
    async def export_algorithm_run_markdown(session_id: str, run_id: str) -> Response:
        session = manager.get(session_id)
        report = get_algorithm_report(session.runtime.workspace.root, run_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Algorithm report was not found")
        return Response(render_markdown(report), media_type="text/markdown")

    @app.post("/api/sessions/{session_id}/memory/candidates/bulk-resolve")
    async def bulk_resolve_memory_candidates(
        session_id: str, request: MemoryBulkResolveRequest
    ) -> dict[str, Any]:
        if not request.confirm:
            raise HTTPException(status_code=400, detail="Bulk memory changes require explicit confirmation")
        runtime = manager.get(session_id).runtime
        resolved: list[dict[str, Any]] = []
        skipped: list[str] = []
        for candidate_id in dict.fromkeys(request.candidate_ids):
            result = (
                runtime.summary_store.confirm(candidate_id, runtime.memory_store)
                if request.action == "confirm"
                else runtime.summary_store.reject(candidate_id)
            )
            if result is None:
                skipped.append(candidate_id)
            else:
                resolved.append(result)
        return {"resolved": resolved, "skipped": skipped, "action": request.action}

    @app.get("/api/sessions/{session_id}/memory/recall-audit")
    async def get_memory_recall_audit(session_id: str) -> dict[str, Any]:
        runtime = manager.get(session_id).runtime
        return {
            "recalled": runtime.state.recalled_memories,
            "turn_id": runtime.state.turn_id,
            "evidence": {"level": "runtime", "kind": "memory_recall_scores"},
        }

    @app.post("/api/sessions/{session_id}/memory/cluster/{cluster_id}/merge")
    async def merge_memory_cluster(
        session_id: str, cluster_id: str, request: MemoryMergeRequest
    ) -> dict[str, Any]:
        del cluster_id
        if not request.confirm:
            raise HTTPException(status_code=400, detail="Memory merge requires explicit confirmation")
        runtime = manager.get(session_id).runtime
        memories = [runtime.memory_store.get(memory_id) for memory_id in dict.fromkeys(request.memory_ids)]
        if len(memories) < 2 or any(memory is None for memory in memories):
            raise HTTPException(status_code=404, detail="At least two valid memories are required")
        merged = runtime.memory_store.remember(
            category=request.category,
            content=request.content,
            keywords=list(dict.fromkeys(keyword for memory in memories if memory for keyword in memory.keywords))[:12],
            importance=max(memory.importance for memory in memories if memory),
            source_session_id=session_id,
            source_turn_id=runtime.state.turn_id,
        )
        archived: list[dict[str, Any]] = []
        for memory in memories:
            if memory and memory.id != merged.id:
                updated = runtime.memory_store.update_metadata(memory.id, archived=True, duplicate_of=merged.id)
                if updated:
                    archived.append(updated.to_dict())
        return {"merged": merged.to_dict(), "archived": archived, "evidence": {"level": "user_confirmed", "kind": "append_only_memory_merge"}}

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

    @app.patch("/api/sessions/{session_id}/memory/{memory_id}")
    async def update_memory_governance(
        session_id: str, memory_id: str, request: MemoryGovernanceRequest
    ) -> dict[str, Any]:
        runtime = manager.get(session_id).runtime
        changes: dict[str, Any] = {
            "pin": {"pinned": True},
            "unpin": {"pinned": False},
            "archive": {"archived": True},
            "restore": {"archived": False},
            "reweight": {"importance": request.importance},
            "set_expiry": {"expires_at": request.expires_at},
            "clear_expiry": {"expires_at": None},
        }[request.action]
        if request.action == "reweight" and request.importance is None:
            raise HTTPException(status_code=400, detail="importance is required for reweight")
        if request.action == "set_expiry" and not request.expires_at:
            raise HTTPException(status_code=400, detail="expires_at is required for set_expiry")
        try:
            memory = runtime.memory_store.update_metadata(memory_id, **changes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if memory is None:
            raise HTTPException(status_code=404, detail="Memory was not found")
        return {"memory": memory.to_dict(), "action": request.action}

    @app.post("/api/sessions/{session_id}/memory/{memory_id}/revalidate")
    async def revalidate_memory(session_id: str, memory_id: str) -> dict[str, Any]:
        runtime = manager.get(session_id).runtime
        memory = runtime.memory_store.get(memory_id)
        if memory is None:
            raise HTTPException(status_code=404, detail="Memory was not found")
        evidence = runtime.memory_store._repository_evidence(memory)
        symbol_status = "not_applicable"
        if memory.symbols:
            repo_map = RepoMapBuilder(runtime.workspace).build(query=" ".join(memory.symbols), focus_paths=memory.file_paths, max_files=80, max_chars=20_000)
            available = {
                str(symbol).casefold()
                for item in repo_map.get("files", [])
                for symbol in (item.get("symbols") or [])
            }
            found = sum(symbol.casefold() in available for symbol in memory.symbols)
            symbol_status = "verified" if found == len(memory.symbols) else "partial" if found else "missing"
        observed = [value for value in (evidence, symbol_status) if value != "not_applicable"]
        status = "not_applicable" if not observed else "missing" if all(value == "missing" for value in observed) else "verified" if all(value == "verified" for value in observed) else "partial"
        updated = runtime.memory_store.update_metadata(
            memory_id,
            last_verified_at=datetime.now(UTC).isoformat(),
            verification_status=status,
        )
        return {"memory": updated.to_dict() if updated else memory.to_dict(), "verification_status": status, "checks": {"files": evidence, "symbols": symbol_status}, "evidence": {"level": "deterministic", "kind": "workspace_revalidation"}}

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
            terminal_statuses = {
                AgentStatus.COMPLETED,
                AgentStatus.PARTIAL,
                AgentStatus.FAILED,
                AgentStatus.CANCELLED,
            }
            if session.runtime.state.status in terminal_statuses and session.task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(session.task)
            if session.running:
                raise HTTPException(status_code=409, detail="Cannot change mode while running")
        state = session.runtime.state
        state.mode = request.mode
        await session.runtime.event_bus.publish(
            AgentEvent(
                type="mode_changed",
                session_id=state.session_id,
                turn_id=state.turn_id,
                payload={"mode": request.mode},
            )
        )
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
        pending = session.runtime.state.pending_approval or {}
        pending_call = pending.get("call") or {}
        tool_call_id = str(request.tool_call_id or pending_call.get("id") or "").strip()
        if not tool_call_id:
            raise HTTPException(status_code=409, detail="Approval is no longer pending")
        grant_details: dict[str, Any] | None = None
        if request.approved and request.scope == "session":
            state = session.runtime.state
            pending = state.pending_approval or {}
            call = pending.get("call") or {}
            if str(call.get("id") or "") != tool_call_id:
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
            session.approval_broker.resolve(tool_call_id, request.approved)
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

    @app.get("/api/sessions/{session_id}/algorithm-lab/runs/{run_id}/events")
    async def get_algorithm_run_events(session_id: str, run_id: str) -> dict[str, Any]:
        """Return the redacted, durable event slice for one deterministic Run."""

        session = manager.get(session_id)
        events = [
            event
            for event in session.runtime.event_store.load()
            if str(event.get("payload", {}).get("run_id") or "") == run_id
        ]
        if not events and run_id not in session.algorithm_results:
            raise HTTPException(status_code=404, detail="Algorithm run was not found")
        return {"run_id": run_id, "events": events, "total": len(events)}

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
            "workflow": _workflow_view(state, include_details=True),
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
        checkpoint_manager = session.runtime.checkpoint_manager
        try:
            preview = checkpoint_manager.preview_restore(state.turn_id)
        except ToolError:
            preview = []
        return {
            "turn_id": state.turn_id,
            "files": checkpoint_manager.list_files(state.turn_id),
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
            event_path = session.runtime.event_store.path
            if event_path.exists():
                event_stat = event_path.stat()
                ui_history = list(
                    _compact_ui_history_from_file(
                        str(event_path), event_stat.st_mtime_ns, event_stat.st_size
                    )
                )
            else:
                ui_history = []
            last_sequence = session.runtime.event_bus.sequence
            await websocket.send_json(
                {
                    "type": "history_start",
                    "payload": {
                        "event_count": len(ui_history),
                        "running": session.running,
                        "status": session.runtime.state.status.value,
                        "pending_approval": session.runtime.state.pending_approval,
                        # Include the same derived details as the session
                        # intelligence view.  The browser replays compact
                        # history first and then applies this metadata; if it
                        # only receives the base projection here, acceptance
                        # criteria and active steps would be cleared at the
                        # end of a refresh.
                        "workflow": _workflow_view(
                            session.runtime.state, include_details=True
                        ),
                    },
                }
            )
            for offset in range(0, len(ui_history), 200):
                await websocket.send_json(
                    {
                        "type": "history_chunk",
                        "payload": {"events": ui_history[offset : offset + 200]},
                    }
                )
            await websocket.send_json({"type": "history_end", "payload": {}})
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
    app.state.shared_password_enabled = bool(resolved_access_password)
    return app


app = create_app()


def main() -> None:
    host = _server_host()
    port = _server_port()
    if host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv(
        "CODE_HELPER_ACCESS_PASSWORD", ""
    ):
        raise SystemExit(
            "Refusing a non-local listener without CODE_HELPER_ACCESS_PASSWORD"
        )
    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
