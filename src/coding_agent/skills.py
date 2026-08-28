from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SkillSummary:
    name: str
    description: str
    when_to_use: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
        }


class SkillLibrary:
    """Safe, read-only loader for project-local Markdown skills."""

    def __init__(self, root: Path, *, max_chars: int = 12_000) -> None:
        self.root = root.resolve()
        self.max_chars = max_chars

    def list_summaries(self) -> list[SkillSummary]:
        if not self.root.is_dir():
            return []
        summaries: list[SkillSummary] = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            path = directory / "SKILL.md"
            if not path.is_file():
                continue
            summaries.append(self._summary(directory.name, path))
        return summaries

    def load(self, name: str) -> tuple[SkillSummary, str] | None:
        if not name or Path(name).name != name or name in {".", ".."}:
            return None
        path = (self.root / name / "SKILL.md").resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return self._summary(name, path), content[: self.max_chars]

    def _summary(self, name: str, path: Path) -> SkillSummary:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return SkillSummary(name, "Project skill", "When the task matches this skill")
        description = "Project skill"
        when_to_use = "When the task matches this skill"
        for line in lines[:40]:
            lower = line.lower().strip()
            if lower.startswith("description:"):
                description = line.split(":", 1)[1].strip()
            elif lower.startswith("when_to_use:") or lower.startswith("when to use:"):
                when_to_use = line.split(":", 1)[1].strip()
        return SkillSummary(name, description, when_to_use)
