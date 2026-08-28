from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .memory import MemoryStore
from .session import AgentState, AgentStatus


@dataclass(slots=True)
class MemoryCandidate:
    id: str
    category: str
    content: str
    keywords: list[str]
    importance: int
    reason: str
    status: str = "pending"
    memory_id: str = ""


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    turn_id: str
    created_at: str
    objective: str
    outcome: str
    status: str
    completed_items: list[str]
    changed_files: list[str]
    verification: dict[str, Any]
    decisions: list[str]
    pending_items: list[str]
    candidates: list[MemoryCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionSummaryStore:
    """Durable, evidence-derived turn summaries and memory candidates."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(
        self,
        state: AgentState,
        status: AgentStatus,
        outcome: str,
        memory_store: MemoryStore,
    ) -> SessionSummary:
        completed = [
            str(item.get("step", ""))
            for item in state.plan
            if item.get("status") == "completed" and item.get("step")
        ]
        pending = [
            str(item.get("step", ""))
            for item in state.plan
            if item.get("status") != "completed" and item.get("step")
        ]
        if status in {AgentStatus.PARTIAL, AgentStatus.FAILED} and not pending:
            pending.append(outcome[:500])
        decisions = [
            item.content
            for item in memory_store.list(category="decision", limit=100)
            if item.source_turn_id == state.turn_id
        ]
        summary = SessionSummary(
            session_id=state.session_id,
            turn_id=state.turn_id,
            created_at=datetime.now(UTC).isoformat(),
            objective=state.current_objective[:2_000],
            outcome=outcome[:2_000],
            status=str(status),
            completed_items=completed,
            changed_files=sorted(state.changed_files),
            verification={
                "fresh": state.verification_is_fresh,
                "successful_sequence": state.last_successful_verification_sequence,
            },
            decisions=decisions,
            pending_items=pending,
            candidates=_candidate_memories(state.current_objective, pending),
        )
        self._write(summary)
        return summary

    def get(self, session_id: str, turn_id: str) -> SessionSummary | None:
        path = self._path(session_id, turn_id)
        if not path.exists():
            return None
        try:
            return _summary_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def list(self, *, session_id: str | None = None, limit: int = 50) -> list[SessionSummary]:
        if not self.root.exists():
            return []
        summaries: list[SessionSummary] = []
        pattern = f"{session_id}__*.json" if session_id else "*.json"
        for path in self.root.glob(pattern):
            try:
                summary = _summary_from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            summaries.append(summary)
        summaries.sort(key=lambda item: item.created_at, reverse=True)
        return summaries[: max(0, min(limit, 500))]

    def candidates(self, *, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for summary in self.list(limit=500):
            for candidate in summary.candidates:
                if status and candidate.status != status:
                    continue
                found.append(
                    {
                        **asdict(candidate),
                        "session_id": summary.session_id,
                        "turn_id": summary.turn_id,
                        "created_at": summary.created_at,
                    }
                )
                if len(found) >= limit:
                    return found
        return found

    def confirm(self, candidate_id: str, memory_store: MemoryStore) -> dict[str, Any] | None:
        return self._resolve_candidate(candidate_id, "confirmed", memory_store)

    def reject(self, candidate_id: str) -> dict[str, Any] | None:
        return self._resolve_candidate(candidate_id, "rejected", None)

    def stats(self) -> dict[str, Any]:
        summaries = self.list(limit=500)
        pending = sum(
            candidate.status == "pending"
            for summary in summaries
            for candidate in summary.candidates
        )
        return {
            "count": len(summaries),
            "pending_candidates": pending,
            "candidates": self.candidates(status="pending", limit=20),
            "recent": [summary.to_dict() for summary in summaries[:3]],
        }

    def _resolve_candidate(
        self,
        candidate_id: str,
        status: str,
        memory_store: MemoryStore | None,
    ) -> dict[str, Any] | None:
        for summary in self.list(limit=500):
            for candidate in summary.candidates:
                if candidate.id != candidate_id or candidate.status != "pending":
                    continue
                if memory_store is not None:
                    memory = memory_store.remember(
                        category=candidate.category,
                        content=candidate.content,
                        keywords=candidate.keywords,
                        importance=candidate.importance,
                        source_session_id=summary.session_id,
                        source_turn_id=summary.turn_id,
                    )
                    candidate.memory_id = memory.id
                candidate.status = status
                self._write(summary)
                return {
                    **asdict(candidate),
                    "session_id": summary.session_id,
                    "turn_id": summary.turn_id,
                }
        return None

    def _path(self, session_id: str, turn_id: str) -> Path:
        safe_session = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
        safe_turn = re.sub(r"[^a-zA-Z0-9_-]", "_", turn_id)
        return self.root / f"{safe_session}__{safe_turn}.json"

    def _write(self, summary: SessionSummary) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(summary.session_id, summary.turn_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


def _candidate_memories(objective: str, pending: list[str]) -> list[MemoryCandidate]:
    objective = objective.strip()
    candidates: list[MemoryCandidate] = []
    lowered = objective.casefold()
    if objective and any(marker in lowered for marker in ("我希望", "我偏好", "以后", "prefer", "always")):
        candidates.append(
            MemoryCandidate(
                id=uuid4().hex,
                category="preference",
                content=objective[:800],
                keywords=_summary_keywords(objective),
                importance=4,
                reason="The user expressed a potentially durable project preference.",
            )
        )
    elif objective and any(marker in lowered for marker in ("决定", "采用", "选择", "choose", "use ")):
        candidates.append(
            MemoryCandidate(
                id=uuid4().hex,
                category="decision",
                content=objective[:800],
                keywords=_summary_keywords(objective),
                importance=4,
                reason="The task contains a possible project decision that requires confirmation.",
            )
        )
    for item in pending[:3]:
        candidates.append(
            MemoryCandidate(
                id=uuid4().hex,
                category="task",
                content=item[:800],
                keywords=_summary_keywords(item),
                importance=3,
                reason="The turn ended with an incomplete plan item.",
            )
        )
    return candidates


def _summary_keywords(value: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", value.casefold())
    return list(dict.fromkeys(words))[:8]


def _summary_from_dict(data: dict[str, Any]) -> SessionSummary:
    return SessionSummary(
        session_id=str(data["session_id"]),
        turn_id=str(data["turn_id"]),
        created_at=str(data["created_at"]),
        objective=str(data.get("objective", "")),
        outcome=str(data.get("outcome", "")),
        status=str(data.get("status", "")),
        completed_items=[str(item) for item in data.get("completed_items", [])],
        changed_files=[str(item) for item in data.get("changed_files", [])],
        verification=dict(data.get("verification") or {}),
        decisions=[str(item) for item in data.get("decisions", [])],
        pending_items=[str(item) for item in data.get("pending_items", [])],
        candidates=[MemoryCandidate(**candidate) for candidate in data.get("candidates", [])],
    )
