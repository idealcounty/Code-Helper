from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tools.workspace import Workspace


MAX_SYMBOLS_PER_FILE = 20
MAX_FILES = 80
MAX_FILE_BYTES = 200_000
_GENERIC_CODE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".java",
    ".go",
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
}


@dataclass(frozen=True, slots=True)
class RepoMapFile:
    path: str
    kind: str
    score: int
    reason: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    centrality: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "score": self.score,
            "reason": self.reason,
            "imports": self.imports,
            "symbols": self.symbols,
            "dependencies": self.dependencies,
            "dependents": self.dependents,
            "centrality": self.centrality,
        }


class RepoMapBuilder:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def build(
        self,
        *,
        query: str = "",
        max_files: int = MAX_FILES,
        focus_paths: list[str] | None = None,
        max_chars: int | None = None,
        include_dependency_graph: bool = True,
    ) -> dict[str, Any]:
        keywords = _keywords(query)
        files: list[RepoMapFile] = []
        totals = {
            "files_seen": 0,
            "python_files": 0,
            "test_files": 0,
            "summary_cache_hits": 0,
            "summary_cache_misses": 0,
            "dependency_graph_cache_hits": 0,
            "dependency_graph_cache_misses": 0,
            "dependency_graph_incremental_updates": 0,
        }
        focus = {item.replace("\\", "/").lstrip("./") for item in (focus_paths or [])}
        seen_paths: set[Path] = set()

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
            seen_paths.add(path.resolve())
            relative = self.workspace.relative(path)
            kind = _kind_for(path, relative)
            score, reason = _score_file(relative, kind, keywords)
            if relative in focus:
                score += 5
                reason.append("recently touched")
            imports: list[str]
            symbols: list[str]
            observation = self.workspace.observe(path)
            cached = self.workspace.repo_map_cache.get(path.resolve())
            if cached is not None and cached[0] == observation.sha256:
                imports = list(cached[1])
                symbols = list(cached[2])
                totals["summary_cache_hits"] += 1
            else:
                totals["summary_cache_misses"] += 1
                imports = []
                symbols = []
                if path.suffix.lower() == ".py":
                    imports, symbols = _python_summary(path)
                elif path.suffix.lower() in _GENERIC_CODE_SUFFIXES:
                    imports, symbols = _generic_code_summary(path)
                self.workspace.repo_map_cache[path.resolve()] = (
                    observation.sha256,
                    tuple(imports),
                    tuple(symbols),
                )
            if path.suffix.lower() == ".py":
                totals["python_files"] += 1
            if symbols:
                score += 2
                reason.append("code symbols")
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

        # Drop deleted files so a later re-created path cannot reuse stale
        # metadata with an unrelated content hash.
        for cached_path in tuple(self.workspace.repo_map_cache):
            if cached_path not in seen_paths:
                self.workspace.repo_map_cache.pop(cached_path, None)

        if include_dependency_graph:
            files, graph_cache_hit, graph_incremental = _attach_dependency_graph_cached(
                files, self.workspace
            )
            if graph_cache_hit:
                totals["dependency_graph_cache_hits"] += 1
            elif graph_incremental:
                totals["dependency_graph_incremental_updates"] += 1
            else:
                totals["dependency_graph_cache_misses"] += 1
        self.workspace.persist_repo_map_cache()
        ranked = sorted(files, key=lambda item: (-item.score, -item.centrality, item.path))
        ranked = ranked[:max_files]
        budget_truncated = False
        if max_chars is not None and max_chars > 0:
            selected: list[RepoMapFile] = []
            used_chars = 0
            for item in ranked:
                item_chars = len(_render_file(item))
                separator = 1 if selected else 0
                if used_chars + separator + item_chars > max_chars:
                    budget_truncated = True
                    break
                selected.append(item)
                used_chars += separator + item_chars
            ranked = selected
        return {
            "root": str(self.workspace.root),
            "query": query,
            "totals": totals,
            "files": [item.to_dict() for item in ranked],
            "truncated": len(files) > len(ranked) or budget_truncated,
            "budget": max_chars,
            "selected_chars": sum(len(_render_file(item)) for item in ranked)
            + max(0, len(ranked) - 1),
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


def _generic_code_summary(path: Path) -> tuple[list[str], list[str]]:
    """Extract conservative imports and top-level symbols without an LSP."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], []

    suffix = path.suffix.lower()
    imports: list[str] = []
    if suffix in {".js", ".jsx", ".mjs", ".ts", ".tsx"}:
        imports.extend(re.findall(r"\bimport\s+[^;\n]*?\s+from\s+[\"']([^\"']+)", text))
        imports.extend(re.findall(r"\b(?:require|import)\s*\(\s*[\"']([^\"']+)", text))
    elif suffix == ".java":
        imports.extend(re.findall(r"^\s*import\s+([\w.]+)\s*;", text, flags=re.MULTILINE))
    elif suffix == ".go":
        imports.extend(re.findall(r"^\s*import\s+(?:\(\s*)?[\"']([^\"']+)[\"']", text, flags=re.MULTILINE))
    else:
        imports.extend(re.findall(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", text, flags=re.MULTILINE))

    symbols: list[str] = []
    symbols.extend(
        f"{kind} {name}"
        for kind, name in re.findall(
            r"\b(class|interface|struct|enum)\s+([A-Za-z_]\w*)", text
        )
    )
    function_pattern = re.compile(
        r"^[ \t]*(?:[\w:<>,~*&\[\].]+[ \t]+)+([A-Za-z_]\w*)[ \t]*\([^;{}\n]*\)[ \t]*(?:const\b[ \t]*)?(?:\{|$)",
        flags=re.MULTILINE,
    )
    excluded = {"if", "for", "while", "switch", "catch"}
    symbols.extend(
        f"def {name}"
        for name in function_pattern.findall(text)
        if name not in excluded
    )
    return sorted(set(imports)), symbols[:MAX_SYMBOLS_PER_FILE]


def _attach_dependency_graph(files: list[RepoMapFile]) -> list[RepoMapFile]:
    """Attach best-effort Python import edges without requiring an indexer."""
    modules: dict[str, str] = {}
    for item in files:
        if item.kind not in {"python", "test"}:
            continue
        module = item.path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        modules[module] = item.path

    dependency_map: dict[str, set[str]] = {item.path: set() for item in files}
    dependent_map: dict[str, set[str]] = {item.path: set() for item in files}
    for item in files:
        if item.kind not in {"python", "test"}:
            continue
        source_module = item.path[:-3].replace("/", ".")
        if source_module.endswith(".__init__"):
            source_module = source_module[:-9]
        source_package = source_module.rsplit(".", 1)[0] if "." in source_module else ""
        for imported in item.imports:
            candidates = _import_candidates(imported, source_package)
            target = next((modules[name] for name in candidates if name in modules), None)
            if target is None or target == item.path:
                continue
            dependency_map[item.path].add(target)
            dependent_map[target].add(item.path)

    enriched: list[RepoMapFile] = []
    for item in files:
        dependencies = sorted(dependency_map[item.path])
        dependents = sorted(dependent_map[item.path])
        centrality = len(dependents)
        score = item.score + min(centrality * 2, 8)
        reason = list(item.reason)
        if centrality:
            reason.append(f"imported by:{centrality}")
        enriched.append(
            RepoMapFile(
                path=item.path,
                kind=item.kind,
                score=score,
                reason=reason,
                imports=item.imports,
                symbols=item.symbols,
                dependencies=dependencies,
                dependents=dependents,
                centrality=centrality,
            )
        )
    return enriched


def _attach_dependency_graph_cached(
    files: list[RepoMapFile], workspace: Workspace
) -> tuple[list[RepoMapFile], bool, bool]:
    signature = tuple(
        sorted(
            (
                item.path,
                workspace.repo_map_cache.get(
                    (workspace.root / item.path).resolve(), ("", (), ())
                )[0],
            )
            for item in files
        )
    )
    cached = workspace.repo_graph_cache
    if cached is not None and cached[0] == signature:
        metadata = cached[1]
        return _apply_dependency_metadata(files, metadata), True, False

    if cached is not None:
        previous_paths = {path for path, _digest in cached[0]}
        current_paths = {path for path, _digest in signature}
        # Adding/removing/renaming a module can change package resolution in
        # ways the lightweight importer cannot prove locally. Rebuild the
        # graph conservatively for path-set changes; content-only edits use
        # the incremental path below.
        if previous_paths != current_paths:
            enriched = _attach_dependency_graph(files)
            metadata = {
                item.path: (
                    tuple(item.dependencies),
                    tuple(item.dependents),
                    item.centrality,
                )
                for item in enriched
            }
            workspace.repo_graph_cache = (signature, metadata)
            return enriched, False, False
        metadata = _incremental_dependency_metadata(files, workspace, cached)
        workspace.repo_graph_cache = (signature, metadata)
        return _apply_dependency_metadata(files, metadata), False, True

    enriched = _attach_dependency_graph(files)
    metadata = {
        item.path: (
            tuple(item.dependencies),
            tuple(item.dependents),
            item.centrality,
        )
        for item in enriched
    }
    workspace.repo_graph_cache = (signature, metadata)
    return enriched, False, False


def _incremental_dependency_metadata(
    files: list[RepoMapFile],
    workspace: Workspace,
    cached: tuple[
        tuple[tuple[str, str], ...],
        dict[str, tuple[tuple[str, ...], tuple[str, ...], int]],
    ],
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...], int]]:
    """Update only sources affected by changed Python modules/importers."""
    old_signature, old_metadata = cached
    current_signature = {
        path: digest
        for path, digest in (
            (
                item.path,
                workspace.repo_map_cache.get(
                    (workspace.root / item.path).resolve(), ("", (), ())
                )[0],
            )
            for item in files
        )
    }
    previous_signature = dict(old_signature)
    changed_paths = {
        path
        for path in set(previous_signature) | set(current_signature)
        if previous_signature.get(path) != current_signature.get(path)
    }
    changed_modules = {
        _python_module_name(path)
        for path in changed_paths
        if path.lower().endswith(".py")
    }
    current_paths = {item.path for item in files}
    modules = {
        _python_module_name(item.path): item.path
        for item in files
        if item.kind in {"python", "test"}
    }
    dependencies_by_path: dict[str, set[str]] = {}
    for item in files:
        previous = old_metadata.get(item.path)
        previous_dependencies = set(previous[0]) if previous is not None else set()
        if item.path in changed_paths or _imports_changed_module(item, changed_modules):
            dependencies_by_path[item.path] = _resolve_item_dependencies(item, modules)
        else:
            dependencies_by_path[item.path] = {
                path for path in previous_dependencies if path in current_paths
            }
    dependents_by_path: dict[str, set[str]] = {path: set() for path in current_paths}
    for source, dependencies in dependencies_by_path.items():
        for target in dependencies:
            if target in dependents_by_path:
                dependents_by_path[target].add(source)
    return {
        path: (
            tuple(sorted(dependencies_by_path[path])),
            tuple(sorted(dependents_by_path[path])),
            len(dependents_by_path[path]),
        )
        for path in current_paths
    }


def _python_module_name(path: str) -> str:
    module = path[:-3].replace("/", ".") if path.lower().endswith(".py") else ""
    return module[:-9] if module.endswith(".__init__") else module


def _imports_changed_module(item: RepoMapFile, changed_modules: set[str]) -> bool:
    if not changed_modules or item.kind not in {"python", "test"}:
        return False
    source_module = _python_module_name(item.path)
    source_package = source_module.rsplit(".", 1)[0] if "." in source_module else ""
    return any(
        candidate in changed_modules
        for imported in item.imports
        for candidate in _import_candidates(imported, source_package)
    )


def _resolve_item_dependencies(
    item: RepoMapFile, modules: dict[str, str]
) -> set[str]:
    if item.kind not in {"python", "test"}:
        return set()
    source_module = _python_module_name(item.path)
    source_package = source_module.rsplit(".", 1)[0] if "." in source_module else ""
    dependencies: set[str] = set()
    for imported in item.imports:
        candidates = _import_candidates(imported, source_package)
        target = next((modules[name] for name in candidates if name in modules), None)
        if target is not None and target != item.path:
            dependencies.add(target)
    return dependencies


def _apply_dependency_metadata(
    files: list[RepoMapFile],
    metadata: dict[str, tuple[tuple[str, ...], tuple[str, ...], int]],
) -> list[RepoMapFile]:
    enriched: list[RepoMapFile] = []
    for item in files:
        dependencies, dependents, centrality = metadata.get(item.path, ((), (), 0))
        reason = list(item.reason)
        if centrality:
            reason.append(f"imported by:{centrality}")
        enriched.append(
            RepoMapFile(
                path=item.path,
                kind=item.kind,
                score=item.score + min(centrality * 2, 8),
                reason=reason,
                imports=item.imports,
                symbols=item.symbols,
                dependencies=list(dependencies),
                dependents=list(dependents),
                centrality=centrality,
            )
        )
    return enriched


def _import_candidates(imported: str, source_package: str) -> list[str]:
    if not imported:
        return []
    if imported.startswith("."):
        level = len(imported) - len(imported.lstrip("."))
        name = imported[level:]
        package_parts = source_package.split(".") if source_package else []
        if level > 1:
            package_parts = package_parts[: max(0, len(package_parts) - level + 1)]
        qualified = ".".join([*package_parts, name]).strip(".")
        return [qualified, qualified.rsplit(".", 1)[0] if "." in qualified else qualified]
    parts = imported.split(".")
    return [".".join(parts[:index]) for index in range(len(parts), 0, -1)]


def _render_file(item: RepoMapFile) -> str:
    symbols = ", ".join(item.symbols[:8])
    deps = ", ".join(item.dependencies[:4])
    return f"{item.path} [{item.kind}] score={item.score} symbols={symbols} deps={deps}"


def _keywords(query: str) -> set[str]:
    raw = query.lower().replace("_", " ").replace("-", " ").split()
    return {word for word in raw if len(word) >= 3}
