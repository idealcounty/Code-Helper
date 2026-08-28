from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4


MEMORY_CATEGORIES = {"fact", "decision", "preference", "task"}
_ASCII_TERM = re.compile(r"[a-z0-9_./-]+", re.IGNORECASE)
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]+")
_STORE_LOCKS: dict[Path, Lock] = {}
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryStore:
    """Append-only, project-scoped long-term memory store."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "memories.jsonl"
        resolved = self.path.resolve()
        with _STORE_LOCKS_GUARD:
            self._lock = _STORE_LOCKS.setdefault(resolved, Lock())

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
        now = datetime.now(UTC).isoformat()
        existing = self.get(memory_id) if memory_id else None
        if memory_id and existing is None:
            raise ValueError(f"Unknown project memory: {memory_id}")
        if existing is None:
            existing = next(
                (
                    item
                    for item in self.list(limit=500)
                    if item.category == category and item.content.casefold() == content.casefold()
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
        )
        self._append({"operation": "upsert", **memory.to_dict()})
        return memory

    def forget(self, memory_id: str) -> bool:
        memory = self.get(memory_id)
        if memory is None:
            return False
        self._append(
            {
                "operation": "delete",
                "id": memory_id,
                "deleted_at": datetime.now(UTC).isoformat(),
            }
        )
        return True

    def get(self, memory_id: str | None) -> ProjectMemory | None:
        if not memory_id:
            return None
        return self._active().get(memory_id)

    def list(self, *, category: str | None = None, limit: int = 100) -> list[ProjectMemory]:
        memories = list(self._active().values())
        if category:
            memories = [item for item in memories if item.category == category]
        memories.sort(key=lambda item: (item.importance, item.updated_at), reverse=True)
        return memories[: max(0, min(limit, 500))]

    def search(
        self,
        query: str,
        *,
        category: str | None = None,
        limit: int = 6,
    ) -> list[ProjectMemory]:
        candidates = self.list(category=category, limit=500)
        query = query.strip()
        if not query:
            return candidates[: max(0, min(limit, 50))]
        query_terms = _terms(query)
        lowered_query = query.casefold()
        scored: list[tuple[float, ProjectMemory]] = []
        for memory in candidates:
            searchable = f"{memory.content} {' '.join(memory.keywords)}".casefold()
            memory_terms = _terms(searchable)
            overlap = len(query_terms & memory_terms)
            exact_bonus = 5 if lowered_query in searchable else 0
            keyword_bonus = sum(2 for keyword in memory.keywords if keyword.casefold() in lowered_query)
            score = overlap * 3 + exact_bonus + keyword_bonus + memory.importance * 0.35
            if score > memory.importance * 0.35:
                scored.append((score, memory))
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [memory for _, memory in scored[: max(0, min(limit, 50))]]

    def stats(self) -> dict[str, Any]:
        memories = self.list(limit=500)
        categories = {category: 0 for category in sorted(MEMORY_CATEGORIES)}
        for memory in memories:
            categories[memory.category] += 1
        return {
            "count": len(memories),
            "categories": categories,
            "recent": [item.to_dict() for item in sorted(memories, key=lambda item: item.updated_at, reverse=True)[:5]],
        }

    def _append(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line + "\n")
            handle.flush()

    def _active(self) -> dict[str, ProjectMemory]:
        active: dict[str, ProjectMemory] = {}
        if not self.path.exists():
            return active
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return active
        for line in lines:
            try:
                record = json.loads(line)
                memory_id = str(record["id"])
                if record.get("operation") == "delete":
                    active.pop(memory_id, None)
                    continue
                if record.get("operation") != "upsert":
                    continue
                active[memory_id] = ProjectMemory(
                    id=memory_id,
                    category=str(record["category"]),
                    content=str(record["content"]),
                    keywords=[str(item) for item in record.get("keywords", [])],
                    importance=int(record.get("importance", 3)),
                    source_session_id=str(record.get("source_session_id", "")),
                    source_turn_id=str(record.get("source_turn_id", "")),
                    created_at=str(record["created_at"]),
                    updated_at=str(record["updated_at"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return active


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


def _terms(value: str) -> set[str]:
    lowered = value.casefold()
    terms = {match.group(0) for match in _ASCII_TERM.finditer(lowered)}
    for match in _CHINESE_RUN.finditer(lowered):
        run = match.group(0)
        terms.add(run)
        terms.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return terms
