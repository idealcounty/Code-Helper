from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .base import ToolError


DEFAULT_IGNORED_DIRECTORIES = {
    ".git",
    ".code-helper",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
}

SENSITIVE_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
}


@dataclass(frozen=True, slots=True)
class FileObservation:
    sha256: str
    size: int
    modified_ns: int


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f"Workspace is not a directory: {self.root}")
        self.observations: dict[Path, FileObservation] = {}
        self.summary_cache: dict[Path, tuple[str, dict[str, object]]] = {}
        # Parsed Repo Map summaries are keyed by content hash so a new
        # builder instance can reuse work while external edits invalidate it.
        self.repo_map_cache: dict[
            Path, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]
        ] = {}
        # Dependency edges are independent of the query score, so cache the
        # graph metadata by the complete path/content signature.
        self.repo_graph_cache: tuple[
            tuple[tuple[str, str], ...],
            dict[str, tuple[tuple[str, ...], tuple[str, ...], int]],
        ] | None = None
        self._repo_map_cache_file = self.root / ".code-helper" / "cache" / "repo-map.json"
        self._load_repo_map_cache()

    def resolve(
        self,
        user_path: str,
        *,
        must_exist: bool = False,
        allow_sensitive: bool = False,
    ) -> Path:
        candidate = Path(user_path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.expanduser().resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise ToolError("FILE_NOT_FOUND", f"Path does not exist: {user_path}") from exc

        try:
            common = os.path.commonpath((str(self.root), str(resolved)))
        except ValueError as exc:
            raise ToolError(
                "PATH_OUTSIDE_WORKSPACE", f"Path is outside workspace: {user_path}"
            ) from exc
        if os.path.normcase(common) != os.path.normcase(str(self.root)):
            raise ToolError(
                "PATH_OUTSIDE_WORKSPACE", f"Path is outside workspace: {user_path}"
            )

        runtime_root = (self.root / ".code-helper").resolve()
        if resolved == runtime_root or runtime_root in resolved.parents:
            raise ToolError(
                "RESERVED_PATH", "The .code-helper runtime directory is not user-editable"
            )

        relative_parts = resolved.relative_to(self.root).parts
        if any(part in DEFAULT_IGNORED_DIRECTORIES for part in relative_parts):
            raise ToolError(
                "RESERVED_PATH", f"Access denied for ignored runtime path: {user_path}"
            )

        if not allow_sensitive and self.is_sensitive(resolved):
            raise ToolError("SENSITIVE_PATH", f"Access denied for sensitive path: {user_path}")
        return resolved

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def is_ignored(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.root)
        except ValueError:
            return True
        return any(part in DEFAULT_IGNORED_DIRECTORIES for part in relative.parts)

    def is_sensitive(self, path: Path) -> bool:
        name = path.name.lower()
        return name in SENSITIVE_NAMES or name.endswith((".pem", ".key", ".p12"))

    def observe(self, path: Path) -> FileObservation:
        observation = _file_observation(path)
        resolved = path.resolve()
        previous = self.observations.get(resolved)
        self.observations[resolved] = observation
        if previous is not None and previous.sha256 != observation.sha256:
            self.summary_cache.pop(resolved, None)
        return observation

    def file_summary(self, path: Path, observation: FileObservation | None = None) -> dict[str, object]:
        resolved = path.resolve()
        observation = observation or self.observe(resolved)
        cached = self.summary_cache.get(resolved)
        if cached and cached[0] == observation.sha256:
            return dict(cached[1])
        text = resolved.read_text(encoding="utf-8")
        summary: dict[str, object] = {
            "path": self.relative(resolved), "sha256": observation.sha256,
            "bytes": observation.size, "lines": len(text.splitlines()),
            "preview": "\n".join(text.splitlines()[:8]),
        }
        self.summary_cache[resolved] = (observation.sha256, summary)
        return dict(summary)

    def persist_repo_map_cache(self) -> None:
        """Persist metadata-only Repo Map caches for reuse after a restart."""
        files = {
            self.relative(path): {
                "sha256": digest,
                "imports": list(imports),
                "symbols": list(symbols),
                "calls": list(calls),
            }
            for path, (digest, imports, symbols, calls) in self.repo_map_cache.items()
            if path.exists() and path.is_file()
        }
        graph: dict[str, object] | None = None
        if self.repo_graph_cache is not None:
            signature, metadata = self.repo_graph_cache
            graph = {
                "signature": [list(item) for item in signature],
                "metadata": {
                    path: {
                        "dependencies": list(values[0]),
                        "dependents": list(values[1]),
                        "centrality": values[2],
                    }
                    for path, values in metadata.items()
                },
            }
        payload = {"version": 1, "files": files, "graph": graph}
        try:
            self._repo_map_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n",
                dir=self._repo_map_cache_file.parent,
                prefix="repo-map-", suffix=".tmp", delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                temporary = Path(handle.name)
            temporary.replace(self._repo_map_cache_file)
        except OSError:
            # A read-only workspace must still be usable with in-memory cache.
            return

    def _load_repo_map_cache(self) -> None:
        try:
            payload = json.loads(self._repo_map_cache_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if payload.get("version") != 1 or not isinstance(payload.get("files"), dict):
            return
        for relative, item in payload["files"].items():
            if not isinstance(relative, str) or not isinstance(item, dict):
                continue
            digest = item.get("sha256")
            imports = item.get("imports")
            symbols = item.get("symbols")
            calls = item.get("calls", [])
            if not isinstance(digest, str) or not isinstance(imports, list) or not isinstance(symbols, list) or not isinstance(calls, list):
                continue
            try:
                path = (self.root / relative).resolve()
                if path.is_relative_to(self.root) and path.is_file():
                    self.repo_map_cache[path] = (
                        digest,
                        tuple(str(value) for value in imports),
                        tuple(str(value) for value in symbols),
                        tuple(str(value) for value in calls),
                    )
            except (OSError, ValueError):
                continue
        graph = payload.get("graph")
        if not isinstance(graph, dict):
            return
        raw_signature = graph.get("signature")
        raw_metadata = graph.get("metadata")
        if not isinstance(raw_signature, list) or not isinstance(raw_metadata, dict):
            return
        signature: list[tuple[str, str]] = []
        for item in raw_signature:
            if isinstance(item, list) and len(item) == 2 and all(isinstance(value, str) for value in item):
                signature.append((item[0], item[1]))
        metadata: dict[str, tuple[tuple[str, ...], tuple[str, ...], int]] = {}
        for path, item in raw_metadata.items():
            if not isinstance(path, str) or not isinstance(item, dict):
                continue
            dependencies = item.get("dependencies")
            dependents = item.get("dependents")
            centrality = item.get("centrality")
            if isinstance(dependencies, list) and isinstance(dependents, list) and isinstance(centrality, int):
                metadata[path] = (
                    tuple(str(value) for value in dependencies),
                    tuple(str(value) for value in dependents),
                    centrality,
                )
        self.repo_graph_cache = (tuple(sorted(signature)), metadata)

    def require_fresh_observation(self, path: Path) -> FileObservation:
        resolved = path.resolve()
        previous = self.observations.get(resolved)
        if previous is None:
            raise ToolError(
                "FILE_NOT_READ",
                f"File must be read before modification: {self.relative(path)}",
            )
        current = _file_observation(resolved)
        if current.sha256 != previous.sha256:
            raise ToolError(
                "FILE_CHANGED",
                f"File changed after it was read: {self.relative(path)}",
                {"expected_sha256": previous.sha256, "actual_sha256": current.sha256},
            )
        return current


def _file_observation(path: Path) -> FileObservation:
    if not path.is_file():
        raise ToolError("NOT_A_FILE", f"Not a regular file: {path}")
    data = path.read_bytes()
    stat = path.stat()
    return FileObservation(
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        modified_ns=stat.st_mtime_ns,
    )
