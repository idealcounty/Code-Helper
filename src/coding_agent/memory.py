from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable
from uuid import uuid4


MEMORY_CATEGORIES = {"fact", "decision", "preference", "task"}
_ASCII_TERM = re.compile(r"[a-z0-9_./-]+", re.IGNORECASE)
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]+")
_STORE_LOCKS: dict[Path, RLock] = {}
_STORE_LOCKS_GUARD = Lock()


@dataclass(frozen=True, slots=True)
class ProjectMemory:
    id: str
    category: str
    content: str
    keywords: list[str]
    importance: int
    source_session_id: str
    source_turn_id: str
    created_at: str
    updated_at: str
    subject: str = ""
    file_paths: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    scope: str = "project"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryStore:
    """Append-only long-term memory store with an enforced project/user scope."""

    def __init__(
        self,
        root: Path,
        *,
        workspace_root: Path | None = None,
        embedding_provider: Callable[[str], list[float]] | None = None,
        scope: str = "project",
    ) -> None:
        if scope not in {"project", "user"}:
            raise ValueError("Memory store scope must be project or user")
        self.root = root
        self.path = root / "memories.jsonl"
        self.workspace_root = workspace_root
        self.embedding_provider = embedding_provider
        self.scope = scope
        resolved = self.path.resolve()
        with _STORE_LOCKS_GUARD:
            self._lock = _STORE_LOCKS.setdefault(resolved, RLock())

    def remember(
        self,
        *,
        category: str,
        content: str,
        keywords: list[str] | None = None,
        importance: int = 3,
        source_session_id: str = "",
        source_turn_id: str = "",
        memory_id: str | None = None,
        subject: str = "",
        file_paths: list[str] | None = None,
        symbols: list[str] | None = None,
        scope: str | None = None,
    ) -> ProjectMemory:
        category = category.strip().lower()
        content = content.strip()
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"Unknown memory category: {category}")
        if not content or len(content) > 2_000:
            raise ValueError("Memory content must contain 1 to 2000 characters")
        if not isinstance(importance, int) or isinstance(importance, bool) or not 1 <= importance <= 5:
            raise ValueError("Memory importance must be an integer from 1 to 5")
        normalized_keywords = _normalize_keywords(keywords or [])
        normalized_paths = _normalize_metadata(file_paths or [], "file path")
        normalized_symbols = _normalize_metadata(symbols or [], "symbol")
        subject = subject.strip()[:200]
        scope = scope or self.scope
        if scope != self.scope:
            raise ValueError(f"Cannot write {scope} memory into a {self.scope} memory store")
        with self._lock:
            now = datetime.now(UTC).isoformat()
            active, _, _ = self._load()
            existing = active.get(memory_id) if memory_id else None
            if memory_id and existing is None:
                raise ValueError(f"Unknown project memory: {memory_id}")
            if existing is None:
                existing = next(
                    (
                        item
                        for item in active.values()
                        if item.category == category
                        and item.content.casefold() == content.casefold()
                    ),
                    None,
                )
                if existing:
                    memory_id = existing.id
            memory = ProjectMemory(
                id=memory_id or uuid4().hex,
                category=category,
                content=content,
                keywords=normalized_keywords,
                importance=importance,
                source_session_id=source_session_id,
                source_turn_id=source_turn_id,
                created_at=existing.created_at if existing else now,
                updated_at=now,
                subject=subject,
                file_paths=normalized_paths,
                symbols=normalized_symbols,
                scope=scope,
            )
            self._append({"operation": "upsert", **memory.to_dict()})
            return memory

    def forget(self, memory_id: str) -> bool:
        with self._lock:
            active, _, _ = self._load()
            if memory_id not in active:
                return False
            self._append(
                {
                    "operation": "delete",
                    "id": memory_id,
                    "scope": self.scope,
                    "deleted_at": datetime.now(UTC).isoformat(),
                }
            )
            return True

    def clear(self) -> int:
        """Deactivate every active memory while preserving the audit log."""
        with self._lock:
            active, _, _ = self._load()
            deleted_at = datetime.now(UTC).isoformat()
            for memory_id in active:
                self._append(
                    {
                        "operation": "delete",
                        "id": memory_id,
                        "scope": self.scope,
                        "deleted_at": deleted_at,
                    }
                )
            return len(active)

    def get(self, memory_id: str | None) -> ProjectMemory | None:
        if not memory_id:
            return None
        with self._lock:
            active, _, _ = self._load()
            return active.get(memory_id)

    def list(
        self, *, category: str | None = None, limit: int | None = 100
    ) -> list[ProjectMemory]:
        with self._lock:
            active, _, _ = self._load()
            memories = list(active.values())
            if category:
                memories = [item for item in memories if item.category == category]
            memories.sort(
                key=lambda item: (item.importance, item.updated_at), reverse=True
            )
            if limit is None:
                return memories
            return memories[: max(0, min(limit, 500))]

    def search(
        self,
        query: str,
        *,
        category: str | None = None,
        limit: int = 6,
        file_path: str | None = None,
        symbol: str | None = None,
        source_session_id: str | None = None,
    ) -> list[ProjectMemory]:
        return [
            item["memory"]
            for item in self.search_detailed(
                query,
                category=category,
                limit=limit,
                file_path=file_path,
                symbol=symbol,
                source_session_id=source_session_id,
            )
        ]

    def search_detailed(
        self,
        query: str,
        *,
        category: str | None = None,
        limit: int = 6,
        file_path: str | None = None,
        symbol: str | None = None,
        source_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        candidates = self.list(category=category, limit=500)
        if file_path:
            needle = file_path.strip().replace("\\", "/").casefold()
            candidates = [
                item
                for item in candidates
                if any(needle == value.casefold() for value in item.file_paths)
            ]
        if symbol:
            needle = symbol.casefold()
            candidates = [item for item in candidates if any(needle == value.casefold() for value in item.symbols)]
        if source_session_id:
            candidates = [item for item in candidates if item.source_session_id == source_session_id]
        query = query.strip()
        result_limit = max(0, min(limit, 50))
        if not query:
            selected = candidates[:result_limit]
            return self._annotate(
                selected,
                {
                    item.id: (
                        0.0,
                        0.0,
                        _recency_score(item.updated_at),
                        item.importance * 0.35 + _recency_score(item.updated_at),
                    )
                    for item in selected
                },
            )
        query_terms = _terms(query)
        lowered_query = query.casefold()
        query_vector = self._embedding(query)
        scored: list[tuple[float, float, float, float, ProjectMemory]] = []
        for memory in candidates:
            searchable = f"{memory.content} {' '.join(memory.keywords)} {memory.subject} {' '.join(memory.file_paths)} {' '.join(memory.symbols)}".casefold()
            memory_terms = _terms(searchable)
            overlap = len(query_terms & memory_terms)
            exact_bonus = 5 if lowered_query in searchable else 0
            keyword_bonus = sum(2 for keyword in memory.keywords if keyword.casefold() in lowered_query)
            lexical = float(overlap * 3 + exact_bonus + keyword_bonus)
            semantic = _cosine(query_vector, self._embedding(searchable)) if query_vector else 0.0
            recency = _recency_score(memory.updated_at)
            score = lexical + semantic * 6 + memory.importance * 0.35 + recency
            if lexical > 0 or semantic > 0.15:
                scored.append((score, lexical, semantic, recency, memory))
        scored.sort(key=lambda item: (item[0], item[4].updated_at), reverse=True)
        selected = _select_with_conflicts(scored, candidates, result_limit)
        scores = {
            item.id: (lexical, semantic, recency, score)
            for score, lexical, semantic, recency, item in scored
        }
        for item in selected:
            scores.setdefault(
                item.id,
                (
                    0.0,
                    0.0,
                    _recency_score(item.updated_at),
                    item.importance * 0.35 + _recency_score(item.updated_at),
                ),
            )
        return self._annotate(selected, scores)

    def _embedding(self, value: str) -> list[float] | None:
        if self.embedding_provider is None:
            return None
        try:
            vector = self.embedding_provider(value)
            return [float(item) for item in vector] if vector else None
        except Exception:  # Optional semantic ranking must degrade to lexical search.
            return None

    def _annotate(
        self,
        memories: list[ProjectMemory],
        scores: dict[str, tuple[float, float, float, float]],
    ) -> list[dict[str, Any]]:
        active = self.list(limit=None)
        groups: dict[tuple[str, str], list[ProjectMemory]] = {}
        for item in active:
            if item.subject:
                groups.setdefault((item.category, item.subject.casefold()), []).append(item)
        detailed: list[dict[str, Any]] = []
        for item in memories:
            conflicts = [
                other for other in groups.get((item.category, item.subject.casefold()), [])
                if other.id != item.id and other.content.casefold() != item.content.casefold()
            ] if item.subject else []
            lexical, semantic, recency, score = scores.get(
                item.id, (0.0, 0.0, 0.0, 0.0)
            )
            detailed.append({
                **item.to_dict(),
                "memory": item,
                "lexical_score": round(lexical, 4),
                "semantic_score": round(semantic, 4),
                "recency_score": round(recency, 4),
                "score": round(score, 4),
                "conflict_ids": [other.id for other in sorted(conflicts, key=lambda value: value.updated_at, reverse=True)],
                "is_latest_for_subject": not conflicts or item.updated_at >= max(other.updated_at for other in conflicts),
                "repository_evidence": self._repository_evidence(item),
            })
        return detailed

    def _repository_evidence(self, memory: ProjectMemory) -> str:
        if not memory.file_paths or self.workspace_root is None:
            return "not_applicable"
        workspace_root = self.workspace_root.resolve()
        exists: list[bool] = []
        for path in memory.file_paths:
            target = (workspace_root / path).resolve()
            exists.append(target.is_relative_to(workspace_root) and target.is_file())
        if all(exists):
            return "verified"
        if any(exists):
            return "partial"
        return "missing"

    def stats(self) -> dict[str, Any]:
        with self._lock:
            active, record_count, invalid_records = self._load()
        memories = list(active.values())
        memories.sort(key=lambda item: (item.importance, item.updated_at), reverse=True)
        categories = {category: 0 for category in sorted(MEMORY_CATEGORIES)}
        for memory in memories:
            categories[memory.category] += 1
        return {
            "count": len(memories),
            "categories": categories,
            "audit_records": record_count,
            "invalid_records": invalid_records,
            "recent": [item.to_dict() for item in sorted(memories, key=lambda item: item.updated_at, reverse=True)[:5]],
        }

    def _append(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line + "\n")
            handle.flush()

    def _load(self) -> tuple[dict[str, ProjectMemory], int, int]:
        active: dict[str, ProjectMemory] = {}
        if not self.path.exists():
            return active, 0, 0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return active, 0, 0
        invalid_records = 0
        for line in lines:
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("Memory record must be an object")
                memory_id = str(record["id"]).strip()
                if not memory_id:
                    raise ValueError("Memory id cannot be empty")
                if record.get("operation") == "delete":
                    if record.get("scope", self.scope) != self.scope:
                        raise ValueError("Memory scope does not match store scope")
                    active.pop(memory_id, None)
                    continue
                if record.get("operation") != "upsert":
                    raise ValueError("Unknown memory operation")
                memory = _memory_from_record(memory_id, record)
                if memory.scope != self.scope:
                    raise ValueError("Memory scope does not match store scope")
                active[memory_id] = memory
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                invalid_records += 1
        return active, len(lines), invalid_records


def _normalize_keywords(values: list[str]) -> list[str]:
    if not isinstance(values, list) or len(values) > 12:
        raise ValueError("Memory keywords must be an array with at most 12 items")
    normalized: list[str] = []
    for value in values:
        keyword = str(value).strip().casefold()
        if not keyword or len(keyword) > 60:
            raise ValueError("Each memory keyword must contain 1 to 60 characters")
        if keyword not in normalized:
            normalized.append(keyword)
    return normalized


def _normalize_metadata(values: list[str], label: str) -> list[str]:
    if not isinstance(values, list) or len(values) > 24:
        raise ValueError(f"Memory {label}s must be an array with at most 24 items")
    normalized: list[str] = []
    for value in values:
        item = str(value).strip().replace("\\", "/")
        if not item or len(item) > 300:
            raise ValueError(f"Each memory {label} must contain 1 to 300 characters")
        if item not in normalized:
            normalized.append(item)
    return normalized


def _memory_from_record(memory_id: str, record: dict[str, Any]) -> ProjectMemory:
    category = record["category"]
    content = record["content"]
    importance = record.get("importance", 3)
    if not isinstance(category, str) or category not in MEMORY_CATEGORIES:
        raise ValueError("Invalid stored memory category")
    if not isinstance(content, str) or not content or len(content) > 2_000:
        raise ValueError("Invalid stored memory content")
    if (
        not isinstance(importance, int)
        or isinstance(importance, bool)
        or not 1 <= importance <= 5
    ):
        raise ValueError("Invalid stored memory importance")
    keywords = _normalize_keywords(record.get("keywords", []))
    file_paths = _normalize_metadata(record.get("file_paths", []), "file path")
    symbols = _normalize_metadata(record.get("symbols", []), "symbol")
    subject = record.get("subject", "")
    scope = record.get("scope", "project")
    created_at = record["created_at"]
    updated_at = record["updated_at"]
    if not isinstance(subject, str) or len(subject) > 200:
        raise ValueError("Invalid stored memory subject")
    if scope not in {"project", "user"}:
        raise ValueError("Invalid stored memory scope")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise ValueError("Invalid stored memory timestamp")
    datetime.fromisoformat(created_at)
    datetime.fromisoformat(updated_at)
    return ProjectMemory(
        id=memory_id,
        category=category,
        content=content,
        keywords=keywords,
        importance=importance,
        source_session_id=str(record.get("source_session_id", "")),
        source_turn_id=str(record.get("source_turn_id", "")),
        created_at=created_at,
        updated_at=updated_at,
        subject=subject,
        file_paths=file_paths,
        symbols=symbols,
        scope=scope,
    )


def _select_with_conflicts(
    scored: list[tuple[float, float, float, float, ProjectMemory]],
    candidates: list[ProjectMemory],
    limit: int,
) -> list[ProjectMemory]:
    if limit <= 0:
        return []
    groups: dict[tuple[str, str], list[ProjectMemory]] = {}
    for item in candidates:
        if item.subject:
            groups.setdefault((item.category, item.subject.casefold()), []).append(item)
    selected: list[ProjectMemory] = []
    selected_ids: set[str] = set()
    for *_, memory in scored:
        related = [memory]
        if memory.subject:
            related.extend(
                sorted(
                    (
                        item
                        for item in groups[(memory.category, memory.subject.casefold())]
                        if item.id != memory.id
                        and item.content.casefold() != memory.content.casefold()
                    ),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )
            )
        for item in related:
            if item.id in selected_ids:
                continue
            selected.append(item)
            selected_ids.add(item.id)
            if len(selected) >= limit:
                return selected
    return selected


def _recency_score(value: str) -> float:
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age_days = max(0.0, (datetime.now(UTC) - timestamp).total_seconds() / 86_400)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return 1.5 / (1.0 + age_days / 30.0)


def _cosine(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    if denominator == 0:
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True)) / denominator))


def _terms(value: str) -> set[str]:
    lowered = value.casefold()
    terms = {match.group(0) for match in _ASCII_TERM.finditer(lowered)}
    for match in _CHINESE_RUN.finditer(lowered):
        run = match.group(0)
        terms.add(run)
        terms.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return terms
