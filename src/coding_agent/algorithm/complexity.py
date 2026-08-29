"""Conservative, dependency-free source complexity estimates for algorithm tasks."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any


_LOOP_PATTERN = re.compile(r"\b(?:for|while)\b")
_GENERIC_TOKEN_PATTERN = re.compile(r"\b(?:for|while)\b|[{}]")
_FUNCTION_PATTERN = re.compile(
    r"\b(?:function|def|func|public|private|protected|static|async)?\s*"
    r"(?:[A-Za-z_$][\w$<>\[\],.:*&\s]*\s+)?([A-Za-z_$][\w$]*)\s*\([^;{}\n]*\)"
)


def analyze_file(path: Path) -> dict[str, Any]:
    """Return a bounded, explainable complexity estimate for a source file."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "path": path.as_posix(),
            "language": _language(path),
            "status": "unavailable",
            "error": str(exc),
        }

    language = _language(path)
    if language == "python":
        report = _analyze_python(source)
    else:
        report = _analyze_generic(source)
    return {
        "path": path.as_posix(),
        "language": language,
        "status": "ok",
        "lines": len(source.splitlines()),
        **report,
    }


def _analyze_python(source: str) -> dict[str, Any]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError) as exc:
        return {
            "parser": "ast",
            "parse_error": str(exc),
            **_estimate_from_text(source),
        }

    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    loops = 0
    max_nesting = 0
    recursive: set[str] = set()

    def visit(node: ast.AST, nesting: int, owner: str | None = None) -> None:
        nonlocal loops, max_nesting
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            loops += 1
            nesting += 1
            max_nesting = max(max_nesting, nesting)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if owner and node.func.id == owner:
                recursive.add(owner)
        next_owner = owner
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            next_owner = node.name
        for child in ast.iter_child_nodes(node):
            visit(child, nesting, next_owner)

    visit(tree, 0)
    comprehensions = sum(
        len(node.generators)
        for node in ast.walk(tree)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
    )
    max_nesting = max(max_nesting, _python_comprehension_depth(tree))
    loops += comprehensions
    return _report(
        loops=loops,
        max_nesting=max_nesting,
        recursive=sorted(recursive),
        parser="ast",
    )


def _python_comprehension_depth(tree: ast.AST) -> int:
    depths = [
        len(node.generators)
        for node in ast.walk(tree)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
    ]
    return max(depths, default=0)


def _analyze_generic(source: str) -> dict[str, Any]:
    # This intentionally avoids pretending to parse C-like grammars. Brace
    # depth gives a stable lower-bound estimate while preserving a clear
    # warning for syntax that needs a language server or compiler.
    lines = source.splitlines()
    loops = 0
    max_nesting = 0
    brace_depth = 0
    loop_depths: list[int] = []
    active_loop_bodies: list[int] = []
    pending_loop = False
    for line in lines:
        clean = _strip_line_comment(line)
        # Process tokens in source order so two loops on one line with nested
        # braces are distinguished (``for (...) { for (...) { ... } }``).
        for token in _GENERIC_TOKEN_PATTERN.finditer(clean):
            value = token.group(0)
            if value in {"for", "while"}:
                loops += 1
                pending_loop = True
                loop_depths.append(len(active_loop_bodies) + 1)
            elif value == "{":
                brace_depth += 1
                if pending_loop:
                    active_loop_bodies.append(brace_depth)
                    pending_loop = False
            else:
                brace_depth = max(0, brace_depth - 1)
                active_loop_bodies[:] = [
                    depth for depth in active_loop_bodies if depth <= brace_depth
                ]
        # A braceless loop cannot safely establish a persistent nesting scope;
        # keep the estimate conservative and do not leak it into the next line.
        if pending_loop:
            pending_loop = False
    if loop_depths:
        max_nesting = max(loop_depths)
    functions = set(_FUNCTION_PATTERN.findall(source))
    recursive = sorted(
        name for name in functions if re.search(rf"\b{re.escape(name)}\s*\(", source[source.find(name) + len(name) :])
    )
    report = _report(
        loops=loops,
        max_nesting=max_nesting,
        recursive=recursive,
        parser="heuristic",
    )
    report["warning"] = "Heuristic estimate; use compiler/LSP for language-accurate complexity."
    return report


def _estimate_from_text(source: str) -> dict[str, Any]:
    return _report(
        loops=len(_LOOP_PATTERN.findall(source)),
        max_nesting=0,
        recursive=[],
        parser="heuristic",
    )


def _report(
    *, loops: int, max_nesting: int, recursive: list[str], parser: str
) -> dict[str, Any]:
    if max_nesting <= 0:
        estimate = "O(1)"
    elif max_nesting == 1:
        estimate = "O(n)"
    else:
        estimate = f"O(n^{max_nesting})"
    warnings: list[str] = []
    if max_nesting >= 2:
        warnings.append("nested loops may be super-linear")
    if recursive:
        warnings.append("recursive calls require stack/branching analysis")
    return {
        "parser": parser,
        "loop_count": loops,
        "max_loop_nesting": max_nesting,
        "recursive_functions": recursive,
        "estimated_time_complexity": estimate,
        "warnings": warnings,
    }


def _strip_line_comment(line: str) -> str:
    # Good enough for a lower-bound estimate; quoted comment markers are left
    # intact rather than attempting an unsafe source transformation.
    return line.split("//", 1)[0].split("#", 1)[0]


def _language(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"}:
        return "cpp"
    if suffix == ".java":
        return "java"
    if suffix == ".go":
        return "go"
    if suffix in {".js", ".jsx", ".mjs", ".ts", ".tsx"}:
        return "javascript"
    return "unknown"
