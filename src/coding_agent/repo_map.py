from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools.workspace import Workspace


MAX_SYMBOLS_PER_FILE = 20
MAX_FILES = 80
MAX_FILE_BYTES = 200_000


@dataclass(frozen=True, slots=True)
class RepoMapFile:
    path: str
    kind: str
    score: int
    reason: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "score": self.score,
            "reason": self.reason,
            "imports": self.imports,
            "symbols": self.symbols,
        }


class RepoMapBuilder:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def build(self, *, query: str = "", max_files: int = MAX_FILES) -> dict[str, Any]:
        keywords = _keywords(query)
        files: list[RepoMapFile] = []
        totals = {"files_seen": 0, "python_files": 0, "test_files": 0}

        for path in sorted(self.workspace.root.rglob("*")):
            if (
                not path.is_file()
                or self.workspace.is_ignored(path)
                or self.workspace.is_sensitive(path)
            ):
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue

            totals["files_seen"] += 1
            relative = self.workspace.relative(path)
            kind = _kind_for(path, relative)
            score, reason = _score_file(relative, kind, keywords)
            imports: list[str] = []
            symbols: list[str] = []
            if path.suffix == ".py":
                totals["python_files"] += 1
                imports, symbols = _python_summary(path)
                if symbols:
                    score += 2
                    reason.append("python symbols")
            if kind == "test":
                totals["test_files"] += 1

            files.append(
                RepoMapFile(
                    path=relative,
                    kind=kind,
                    score=score,
                    reason=reason,
                    imports=imports[:MAX_SYMBOLS_PER_FILE],
                    symbols=symbols[:MAX_SYMBOLS_PER_FILE],
                )
            )

        ranked = sorted(files, key=lambda item: (-item.score, item.path))[:max_files]
        return {
            "root": str(self.workspace.root),
            "query": query,
            "totals": totals,
            "files": [item.to_dict() for item in ranked],
            "truncated": len(files) > len(ranked),
        }


def _kind_for(path: Path, relative: str) -> str:
    lowered = relative.lower()
    name = path.name.lower()
    if "test" in path.parts or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    if name in {"pyproject.toml", "package.json", "requirements.txt", "setup.py"}:
        return "config"
    if name.startswith("readme"):
        return "docs"
    if path.suffix == ".py":
        return "python"
    if path.suffix in {".md", ".txt", ".rst"}:
        return "docs"
    return path.suffix.lstrip(".") or "file"


def _score_file(relative: str, kind: str, keywords: set[str]) -> tuple[int, list[str]]:
    lowered = relative.lower()
    score = 0
    reason: list[str] = []
    for keyword in keywords:
        if keyword and keyword in lowered:
            score += 4
            reason.append(f"keyword:{keyword}")
    if kind in {"config", "docs"}:
        score += 3
        reason.append(kind)
    if kind == "test":
        score += 3
        reason.append("test")
    if relative.startswith("src/"):
        score += 2
        reason.append("source")
    return score, reason


def _python_summary(path: Path) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return [], []

    imports: list[str] = []
    symbols: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.append(module)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "class" if isinstance(node, ast.ClassDef) else "def"
            symbols.append(f"{prefix} {node.name}")
    return sorted(set(imports)), symbols


def _keywords(query: str) -> set[str]:
    raw = query.lower().replace("_", " ").replace("-", " ").split()
    return {word for word in raw if len(word) >= 3}
