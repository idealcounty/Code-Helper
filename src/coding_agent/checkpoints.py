from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tools.base import ToolError
from .tools.workspace import Workspace


@dataclass(frozen=True, slots=True)
class CheckpointCapture:
    turn_id: str
    path: str
    created: bool
    existed: bool


class CheckpointManager:
    """Task-scoped snapshots of files immediately before their first mutation."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = workspace.root / ".code-helper" / "checkpoints"

    def capture(self, turn_id: str, user_path: str) -> CheckpointCapture:
        path = self.workspace.resolve(user_path, must_exist=False)
        relative = self.workspace.relative(path)
        manifest = self._load_manifest(turn_id)
        if relative in manifest["files"]:
            entry = manifest["files"][relative]
            return CheckpointCapture(turn_id, relative, False, bool(entry["existed"]))

        existed = path.exists()
        if existed and not path.is_file():
            raise ToolError("NOT_A_FILE", f"Cannot checkpoint non-file path: {relative}")

        turn_root = self.root / turn_id
        backup = turn_root / "files" / Path(relative)
        entry: dict[str, Any] = {"existed": existed, "backup": None, "sha256": None}
        if existed:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            entry["backup"] = backup.relative_to(turn_root).as_posix()
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

        manifest["files"][relative] = entry
        self._write_manifest(turn_id, manifest)
        return CheckpointCapture(turn_id, relative, True, existed)

    def list_files(self, turn_id: str) -> list[dict[str, Any]]:
        manifest = self._load_manifest(turn_id, create=False)
        return [
            {"path": path, **entry}
            for path, entry in sorted(manifest.get("files", {}).items())
        ]

    def restore(self, turn_id: str) -> list[str]:
        manifest = self._load_manifest(turn_id, create=False)
        files = manifest.get("files", {})
        if not files:
            raise ToolError("CHECKPOINT_NOT_FOUND", "No checkpoint exists for this turn")

        restored: list[str] = []
        turn_root = self.root / turn_id
        for relative, entry in files.items():
            target = self.workspace.resolve(relative, must_exist=False)
            if entry["existed"]:
                backup_value = entry.get("backup")
                if not backup_value:
                    raise ToolError(
                        "CHECKPOINT_CORRUPT", f"Missing backup metadata for {relative}"
                    )
                backup = (turn_root / backup_value).resolve(strict=True)
                if turn_root.resolve() not in backup.parents:
                    raise ToolError(
                        "CHECKPOINT_CORRUPT", f"Invalid backup path for {relative}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            elif target.exists():
                if not target.is_file():
                    raise ToolError(
                        "RESTORE_CONFLICT", f"Created path is no longer a file: {relative}"
                    )
                target.unlink()
            restored.append(relative)

        self.workspace.observations.clear()
        return restored

    def _load_manifest(
        self, turn_id: str, *, create: bool = True
    ) -> dict[str, Any]:
        path = self.root / turn_id / "manifest.json"
        if not path.exists():
            return {"turn_id": turn_id, "files": {}} if create else {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError("CHECKPOINT_CORRUPT", f"Cannot read checkpoint: {exc}") from exc
        if payload.get("turn_id") != turn_id or not isinstance(payload.get("files"), dict):
            raise ToolError("CHECKPOINT_CORRUPT", "Checkpoint manifest is invalid")
        return payload

    def _write_manifest(self, turn_id: str, manifest: dict[str, Any]) -> None:
        path = self.root / turn_id / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
