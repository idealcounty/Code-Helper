from __future__ import annotations

import hashlib
import os
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
