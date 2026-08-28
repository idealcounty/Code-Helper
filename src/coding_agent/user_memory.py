from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .memory import MemoryStore, ProjectMemory


class UserMemoryService:
    """Opt-in user memory stored outside every project workspace."""

    def __init__(self, root: Path, *, initially_enabled: bool = False) -> None:
        self.root = root.resolve()
        self.settings_path = self.root / "settings.json"
        self.store = MemoryStore(self.root / "records", scope="user")
        if not self.settings_path.exists() and initially_enabled:
            self.set_enabled(True)

    @property
    def enabled(self) -> bool:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return data.get("enabled") is True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def set_enabled(self, enabled: bool) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"enabled": bool(enabled)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.settings_path)
        return self.enabled

    def search(self, query: str, *, limit: int = 6) -> list[ProjectMemory]:
        return self.store.search(query, limit=limit) if self.enabled else []

    def export(self) -> dict[str, Any]:
        memories = [item.to_dict() for item in self.store.list(limit=500)]
        return {"version": 1, "scope": "user", "enabled": self.enabled, "memories": memories}

    def clear(self) -> int:
        memories = self.store.list(limit=500)
        return sum(1 for item in memories if self.store.forget(item.id))

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "storage": str(self.root),
            **self.store.stats(),
        }
