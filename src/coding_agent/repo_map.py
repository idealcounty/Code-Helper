from __future__ import annotations

import ast
import posixpath
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
_JAVASCRIPT_SUFFIXES = {".js", ".jsx", ".mjs", ".ts", ".tsx"}
_JAVA_SUFFIXES = {".java"}
_C_CPP_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"}


@dataclass(frozen=True, slots=True)
class RepoMapFile:
    path: str
    kind: str
    score: int
    reason: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
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
            "calls": self.calls,
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
            calls: list[str]
            observation = self.workspace.observe(path)
            cached = self.workspace.repo_map_cache.get(path.resolve())
            if cached is not None and cached[0] == observation.sha256:
                imports = list(cached[1])
                symbols = list(cached[2])
                calls = list(cached[3]) if len(cached) > 3 else []
                totals["summary_cache_hits"] += 1
            else:
                totals["summary_cache_misses"] += 1
                imports = []
                symbols = []
                calls = []
                if path.suffix.lower() == ".py":
                    imports, symbols, calls = _python_summary(path)
                elif path.suffix.lower() in _GENERIC_CODE_SUFFIXES:
                    imports, symbols, calls = _generic_code_summary(path)
                self.workspace.repo_map_cache[path.resolve()] = (
                    observation.sha256,
                    tuple(imports),
                    tuple(symbols),
                    tuple(calls),
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
                    calls=calls[:MAX_SYMBOLS_PER_FILE],
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


def _python_summary(path: Path) -> tuple[list[str], list[str], list[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return [], [], []

    imports: list[str] = []
    symbols: list[str] = []
    calls: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.append(module)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "class" if isinstance(node, ast.ClassDef) else "def"
            symbols.append(f"{prefix} {node.name}")
    # Include only statically knowable dynamic imports. Runtime-computed
    # module names are intentionally ignored so the graph remains conservative.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        is_dynamic_import = (
            isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}
        ) or (
            isinstance(function, ast.Attribute) and function.attr == "import_module"
        )
        if is_dynamic_import and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            imports.append(node.args[0].value)
    return sorted(set(imports)), symbols, sorted(set(calls))


def _generic_code_summary(path: Path) -> tuple[list[str], list[str], list[str]]:
    """Extract conservative imports and top-level symbols without an LSP."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], [], []

    suffix = path.suffix.lower()
    imports: list[str] = []
    if suffix in {".js", ".jsx", ".mjs", ".ts", ".tsx"}:
        imports.extend(re.findall(r"\bimport\s+[^;\n]*?\s+from\s+[\"']([^\"']+)", text))
        imports.extend(
            re.findall(
                r"\bexport\s+(?:\*|\{[^}]*\})\s+from\s+[\"']([^\"']+)",
                text,
            )
        )
        imports.extend(re.findall(r"\b(?:require|import)\s*\(\s*[\"']([^\"']+)", text))
    elif suffix == ".java":
        imports.extend(re.findall(r"^\s*import\s+([\w.]+)\s*;", text, flags=re.MULTILINE))
    elif suffix == ".go":
        imports.extend(
            re.findall(
                r"^\s*import\s+(?:[A-Za-z_]\w*\s+)?[\"']([^\"']+)[\"']",
                text,
                flags=re.MULTILINE,
            )
        )
        block = re.search(r"\bimport\s*\((.*?)\)", text, flags=re.DOTALL)
        if block:
            imports.extend(re.findall(r"[\"']([^\"']+)[\"']", block.group(1)))
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
    calls = _generic_call_names(text)
    return sorted(set(imports)), symbols[:MAX_SYMBOLS_PER_FILE], calls[:MAX_SYMBOLS_PER_FILE]


def _generic_call_names(text: str) -> list[str]:
    """Extract identifier calls; dependency resolution filters unknown names."""
    names = re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", text)
    excluded = {"if", "for", "while", "switch", "catch", "sizeof"}
    return sorted({name for name in names if name not in excluded})


def _attach_dependency_graph(
    files: list[RepoMapFile], workspace: Workspace | None = None
) -> list[RepoMapFile]:
    """Attach conservative cross-language import edges without an indexer."""
    indexes = _dependency_indexes(files, workspace)

    dependency_map: dict[str, set[str]] = {item.path: set() for item in files}
    dependent_map: dict[str, set[str]] = {item.path: set() for item in files}
    for item in files:
        for target in _resolve_item_dependencies(item, indexes):
            if target == item.path:
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
                calls=item.calls,
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
            enriched = _attach_dependency_graph(files, workspace)
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

    enriched = _attach_dependency_graph(files, workspace)
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
    """Update only sources affected by changed modules/importers."""
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
    current_paths = {item.path for item in files}
    indexes = _dependency_indexes(files, workspace)
    dependencies_by_path: dict[str, set[str]] = {}
    for item in files:
        previous = old_metadata.get(item.path)
        previous_dependencies = set(previous[0]) if previous is not None else set()
        if item.path in changed_paths or _imports_changed_path(item, changed_paths, indexes):
            dependencies_by_path[item.path] = _resolve_item_dependencies(item, indexes)
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


def _imports_changed_path(item: RepoMapFile, changed_paths: set[str], indexes: dict[str, Any]) -> bool:
    """Return whether an unchanged importer currently points at a changed file."""
    if not changed_paths:
        return False
    return bool(_resolve_item_dependencies(item, indexes) & changed_paths)


def _dependency_indexes(
    files: list[RepoMapFile], workspace: Workspace | None = None
) -> dict[str, Any]:
    paths = {item.path for item in files}
    python_modules: dict[str, str] = {}
    java_modules: dict[str, set[str]] = {}
    java_files: set[str] = set()
    go_module = _go_module_name(workspace) if workspace is not None else ""
    go_packages: dict[str, set[str]] = {}
    for item in files:
        suffix = Path(item.path).suffix.lower()
        if suffix == ".py":
            module = _python_module_name(item.path)
            python_modules[module] = item.path
        elif suffix == ".java":
            java_files.add(item.path)
            dotted = item.path[:-5].replace("/", ".")
            java_modules.setdefault(dotted, set()).add(item.path)
            java_modules.setdefault(Path(item.path).stem, set()).add(item.path)
            if workspace is not None:
                package = _java_package_name(workspace.root / item.path)
                if package:
                    java_modules.setdefault(
                        f"{package}.{Path(item.path).stem}", set()
                    ).add(item.path)
        elif suffix == ".go" and go_module:
            package_dir = posixpath.dirname(item.path)
            package_name = package_dir.strip("/")
            go_packages.setdefault(package_name, set()).add(item.path)
    return {
        "paths": paths,
        "python_modules": python_modules,
        "java_modules": java_modules,
        "java_files": java_files,
        "go_module": go_module,
        "go_packages": go_packages,
        "symbols": _symbol_index(files),
    }


def _symbol_index(files: list[RepoMapFile]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for item in files:
        for symbol in item.symbols:
            _kind, separator, name = symbol.partition(" ")
            if separator and name:
                index.setdefault(name, set()).add(item.path)
    return index


def _java_package_name(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    match = re.search(r"^\s*package\s+([\w.]+)\s*;", text, flags=re.MULTILINE)
    return match.group(1) if match else ""


def _go_module_name(workspace: Workspace) -> str:
    try:
        text = (workspace.root / "go.mod").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    match = re.search(r"^\s*module\s+([^\s]+)", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _resolve_item_dependencies(item: RepoMapFile, indexes: dict[str, Any]) -> set[str]:
    suffix = Path(item.path).suffix.lower()
    dependencies: set[str] = set()
    for imported in item.imports:
        if suffix == ".py":
            source_module = _python_module_name(item.path)
            source_package = source_module.rsplit(".", 1)[0] if "." in source_module else ""
            candidates = _import_candidates(imported, source_package)
            target = next(
                (indexes["python_modules"][name] for name in candidates if name in indexes["python_modules"]),
                None,
            )
            if target is not None:
                dependencies.add(target)
        elif suffix in _JAVASCRIPT_SUFFIXES:
            target = _resolve_javascript_import(item.path, imported, indexes["paths"])
            if target is not None:
                dependencies.add(target)
        elif suffix in _JAVA_SUFFIXES:
            dependencies.update(
                target
                for target in _resolve_java_import(imported, indexes["java_modules"], indexes["java_files"])
                if target != item.path
            )
        elif suffix in _C_CPP_SUFFIXES:
            target = _resolve_c_cpp_include(item.path, imported, indexes["paths"])
            if target is not None:
                dependencies.add(target)
        elif suffix == ".go":
            dependencies.update(_resolve_go_import(imported, indexes))
    for call in item.calls:
        dependencies.update(
            target
            for target in indexes.get("symbols", {}).get(call, ())
            if target != item.path
        )
    return dependencies


def _resolve_javascript_import(source_path: str, imported: str, paths: set[str]) -> str | None:
    """Resolve only local relative JS/TS imports; package imports stay external."""
    if not imported.startswith((".", "/")):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), imported))
    extensions = ("", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".d.ts")
    candidates = [base + extension for extension in extensions]
    candidates.extend(posixpath.join(base, "index" + extension) for extension in extensions[1:])
    return next((candidate for candidate in candidates if candidate in paths), None)


def _resolve_c_cpp_include(
    source_path: str, imported: str, paths: set[str]
) -> str | None:
    """Resolve local C/C++ quoted includes while ignoring system headers."""
    if imported.startswith("<") or imported.startswith("/"):
        return None
    normalized_import = imported.replace("\\", "/")
    candidates = [
        posixpath.normpath(posixpath.join(posixpath.dirname(source_path), normalized_import)),
        posixpath.normpath(normalized_import),
    ]
    return next((candidate for candidate in candidates if candidate in paths), None)


def _resolve_go_import(imported: str, indexes: dict[str, Any]) -> set[str]:
    """Resolve imports that point inside the workspace's declared Go module."""
    module = str(indexes.get("go_module") or "")
    if not module or imported != module and not imported.startswith(module + "/"):
        return set()
    package = imported[len(module) :].lstrip("/")
    return set(indexes.get("go_packages", {}).get(package, ()))


def _resolve_java_import(
    imported: str, java_modules: dict[str, set[str]], java_files: set[str]
) -> set[str]:
    """Resolve local Java classes by dotted path, ignoring JDK/external packages."""
    name = imported.removesuffix(".*")
    direct = java_modules.get(name)
    if direct:
        return set(direct)
    suffix = "." + name
    return {
        path
        for path in java_files
        if path[:-5].replace("/", ".").endswith(suffix)
        or (".*" in imported and path[:-5].replace("/", ".").startswith(name + "."))
    }


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
                calls=item.calls,
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
    calls = ", ".join(item.calls[:4])
    deps = ", ".join(item.dependencies[:4])
    return f"{item.path} [{item.kind}] score={item.score} symbols={symbols} calls={calls} deps={deps}"


def _keywords(query: str) -> set[str]:
    raw = query.lower().replace("_", " ").replace("-", " ").split()
    return {word for word in raw if len(word) >= 3}
