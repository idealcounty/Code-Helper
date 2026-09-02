from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from .memory import MemoryStore
from .session import AgentState, AgentStatus


_SUMMARY_LOCKS: dict[Path, RLock] = {}
_SUMMARY_LOCKS_GUARD = Lock()


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
    prompt: str = ""
    occurrence_count: int = 1
    work_type: str = ""
    source_kind: str = "legacy"


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
        with _SUMMARY_LOCKS_GUARD:
            self._lock = _SUMMARY_LOCKS.setdefault(root.resolve(), RLock())

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
        previous_summaries = self.list(limit=100)
        candidates = _candidate_memories(
            state.current_objective,
            pending,
            previous_summaries=previous_summaries,
            memory_store=memory_store,
        )
        self._retire_replaced_candidates(previous_summaries, candidates)
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
            candidates=candidates,
        )
        self._write(summary)
        return summary

    def _retire_replaced_candidates(
        self,
        summaries: list[SessionSummary],
        replacements: list[MemoryCandidate],
    ) -> None:
        """Keep only the newest pending suggestion for the same memory text."""
        replacement_keys = {
            (item.category, _normalized_candidate_content(item.content))
            for item in replacements
            if item.source_kind == "conversation_keywords"
        }
        if not replacement_keys:
            return
        for summary in summaries:
            changed = False
            for candidate in summary.candidates:
                key = (candidate.category, _normalized_candidate_content(candidate.content))
                if candidate.status == "pending" and key in replacement_keys:
                    candidate.status = "rejected"
                    changed = True
            if changed:
                self._write(summary)

    def get(self, session_id: str, turn_id: str) -> SessionSummary | None:
        path = self._path(session_id, turn_id)
        with self._lock:
            if not path.exists():
                return None
            try:
                return _summary_from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                return None

    def list(self, *, session_id: str | None = None, limit: int = 50) -> list[SessionSummary]:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(summary.session_id, summary.turn_id)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)


def _candidate_memories(
    objective: str,
    pending: list[str],
    *,
    previous_summaries: list[SessionSummary] | None = None,
    memory_store: MemoryStore | None = None,
) -> list[MemoryCandidate]:
    objective = objective.strip()
    candidates: list[MemoryCandidate] = []
    lowered = objective.casefold()
    keywords = _summary_keywords(objective)
    work_type = _classify_work_type(objective)
    occurrence_count = 1 + sum(
        _classify_work_type(summary.objective) == work_type
        for summary in (previous_summaries or [])
        if work_type
    )
    existing = {
        (item.category, _normalized_candidate_content(item.content))
        for item in memory_store.list(limit=None)
    } if memory_store is not None else set()

    if objective and any(_contains_marker(lowered, marker) for marker in ("我希望", "我偏好", "以后", "prefer", "always")):
        candidate = MemoryCandidate(
            id=uuid4().hex,
            category="preference",
            content=objective[:800],
            keywords=keywords,
            importance=4,
            reason=_suggestion_reason(occurrence_count),
            prompt=_suggestion_prompt(objective[:800], keywords, work_type, occurrence_count),
            occurrence_count=occurrence_count,
            work_type=work_type,
            source_kind="conversation_keywords",
        )
        if (candidate.category, _normalized_candidate_content(candidate.content)) not in existing:
            candidates.append(candidate)
    elif objective and any(_contains_marker(lowered, marker) for marker in ("决定", "采用", "选择", "choose", "use")):
        candidate = MemoryCandidate(
            id=uuid4().hex,
            category="decision",
            content=objective[:800],
            keywords=keywords,
            importance=4,
            reason=_suggestion_reason(occurrence_count),
            prompt=_suggestion_prompt(objective[:800], keywords, work_type, occurrence_count),
            occurrence_count=occurrence_count,
            work_type=work_type,
            source_kind="conversation_keywords",
        )
        if (candidate.category, _normalized_candidate_content(candidate.content)) not in existing:
            candidates.append(candidate)
    elif objective and keywords and work_type:
        content = _profile_memory_content(work_type, keywords)
        candidate = MemoryCandidate(
            id=uuid4().hex,
            category="preference",
            content=content,
            keywords=keywords,
            importance=3 if occurrence_count == 1 else 4,
            reason=_suggestion_reason(occurrence_count),
            prompt=_suggestion_prompt(content, keywords, work_type, occurrence_count),
            occurrence_count=occurrence_count,
            work_type=work_type,
            source_kind="conversation_keywords",
        )
        if (candidate.category, _normalized_candidate_content(candidate.content)) not in existing:
            candidates.append(candidate)
    for item in pending[:3]:
        candidates.append(
            MemoryCandidate(
                id=uuid4().hex,
                category="task",
                content=item[:800],
                keywords=_summary_keywords(item),
                importance=3,
                reason="本轮结束时仍有未完成事项，确认后可在后续对话中继续提醒。",
                prompt=f"本轮还有“{item[:120]}”尚未完成，是否将它存入记忆区？",
                work_type="待办事项",
                source_kind="pending_task",
            )
        )
    return candidates


def _summary_keywords(value: str) -> list[str]:
    lowered = value.casefold()
    canonical_patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("C++", ("c++", "cpp")),
        ("Python", ("python",)),
        ("Java", ("java",)),
        ("JavaScript", ("javascript", "js")),
        ("TypeScript", ("typescript",)),
        ("React", ("react",)),
        ("FastAPI", ("fastapi",)),
        ("DeepSeek", ("deepseek",)),
        ("Docker", ("docker",)),
        ("Git", ("git", "github")),
        ("SQL", ("sql",)),
        ("API", ("api",)),
        ("UI", ("ui",)),
        ("算法", ("算法", "algorithm", "leetcode", "codeforces")),
        ("复杂度", ("复杂度", "complexity")),
        ("对拍", ("对拍", "oracle")),
        ("测试", ("测试", "pytest", "单元测试", "压力测试")),
        ("前端", ("前端",)),
        ("后端", ("后端",)),
        ("网页", ("网页", "网站")),
        ("文档", ("文档", "markdown")),
        ("Agent", ("agent",)),
        ("记忆", ("记忆",)),
        ("上下文", ("上下文",)),
    )
    found: list[str] = []
    covered_aliases: set[str] = set()
    for label, aliases in canonical_patterns:
        matched = False
        for alias in aliases:
            if re.fullmatch(r"[a-z0-9_+#.-]+", alias):
                present = re.search(rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])", lowered) is not None
            else:
                present = alias in lowered
            if present:
                matched = True
                covered_aliases.add(alias)
        if matched and label not in found:
            found.append(label)
    for quoted in re.findall(r"[「“\"]([^」”\"]{2,24})[」”\"]", value):
        cleaned = quoted.strip()
        if cleaned and cleaned not in found:
            found.append(cleaned)
    stopwords = {"please", "using", "with", "this", "that", "from", "into", "about", "continue", "finish", "use", "prefer", "always"}
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_+#.-]{2,30}", value):
        normalized = word.casefold()
        if normalized in stopwords or normalized in covered_aliases:
            continue
        if all(normalized != item.casefold() for item in found):
            found.append(word)
    return found[:8]


def _classify_work_type(value: str) -> str:
    lowered = value.casefold()
    patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("算法问题求解", ("算法", "algorithm", "复杂度", "complexity", "leetcode", "codeforces", "对拍", "oracle")),
        ("测试与质量验证", ("测试", "pytest", "单元测试", "压力测试", "验证", "coverage")),
        ("前端与网页开发", ("前端", "网页", "网站", "ui", "css", "html", "javascript", "react")),
        ("文档与报告整理", ("文档", "markdown", "报告", "总结")),
        ("项目功能开发", ("项目", "功能", "feature", "bug", "fix", "修复", "仓库", "repository", "代码", "code")),
    )
    scored = [
        (sum(_contains_marker(lowered, marker) for marker in markers), -index, label)
        for index, (label, markers) in enumerate(patterns)
    ]
    score, _, label = max(scored)
    return label if score else ""


def _profile_memory_content(work_type: str, keywords: list[str]) -> str:
    primary_tool = next(
        (item for item in keywords if item in {"C++", "Python", "Java", "JavaScript", "TypeScript", "React", "FastAPI", "DeepSeek", "Docker", "Git", "SQL"}),
        "",
    )
    topical = [item for item in keywords if item != primary_tool][:3]
    if primary_tool and topical:
        return f"用户经常使用 {primary_tool} 完成{work_type}，并关注{'、'.join(topical)}。"
    if primary_tool:
        return f"用户经常使用 {primary_tool} 完成{work_type}。"
    return f"用户经常完成{work_type}，常用关键词：{'、'.join(topical or keywords[:3])}。"


def _suggestion_prompt(
    content: str,
    keywords: list[str],
    work_type: str,
    occurrence_count: int,
) -> str:
    keyword_text = "」「".join(keywords[:3]) or "当前任务"
    work = work_type or "当前类型的工作"
    if occurrence_count >= 2:
        lead = f"我们检测到你在最近 {occurrence_count} 次对话中经常使用「{keyword_text}」描述任务，并完成{work}。"
    else:
        lead = f"本轮对话中识别到「{keyword_text}」，主要完成{work}。"
    return f"{lead}是否将“{content}”存入记忆区？"


def _suggestion_reason(occurrence_count: int) -> str:
    if occurrence_count >= 2:
        return f"同类任务已在最近 {occurrence_count} 次对话中出现，只有确认后才会保存。"
    return "这是从本轮对话提取的关键词建议，只有确认后才会保存。"


def _normalized_candidate_content(value: str) -> str:
    return " ".join(value.casefold().split())


def _contains_marker(lowered: str, marker: str) -> bool:
    if re.fullmatch(r"[a-z0-9_+#.-]+", marker):
        return re.search(rf"(?<![a-z0-9_]){re.escape(marker)}(?![a-z0-9_])", lowered) is not None
    return marker in lowered


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
