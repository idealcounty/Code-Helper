from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from difflib import unified_diff
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
        entry: dict[str, Any] = {
            "existed": existed,
            "backup": None,
            "sha256": None,
            "agent_existed": None,
            "agent_backup": None,
            "agent_sha256": None,
            "mutation_sequence": None,
            "tool": None,
        }
        if existed:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            entry["backup"] = backup.relative_to(turn_root).as_posix()
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

        manifest["files"][relative] = entry
        self._write_manifest(turn_id, manifest)
        return CheckpointCapture(turn_id, relative, True, existed)

    def record_mutation(
        self,
        turn_id: str,
        user_path: str,
        *,
        sequence: int,
        tool: str,
        expected_sha256: str | None = None,
    ) -> None:
        """Record the latest Agent-owned result used as the safe restore precondition."""
        path = self.workspace.resolve(user_path, must_exist=False)
        relative = self.workspace.relative(path)
        manifest = self._load_manifest(turn_id, create=False)
        entry = manifest.get("files", {}).get(relative)
        if not isinstance(entry, dict):
            raise ToolError(
                "CHECKPOINT_NOT_FOUND",
                f"No baseline checkpoint exists for mutated file: {relative}",
            )

        turn_root = self.root / turn_id
        agent_backup = turn_root / "agent" / Path(relative)
        existed = path.exists()
        if existed and not path.is_file():
            raise ToolError(
                "CHECKPOINT_CORRUPT",
                f"Agent mutation result is not a file: {relative}",
            )
        if existed:
            actual_sha256 = _sha256(path)
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise ToolError(
                    "CHECKPOINT_CONFLICT",
                    f"File changed before the Agent result could be checkpointed: {relative}",
                )
            agent_backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, agent_backup)
            entry["agent_backup"] = agent_backup.relative_to(turn_root).as_posix()
            entry["agent_sha256"] = actual_sha256
        else:
            entry["agent_backup"] = None
            entry["agent_sha256"] = None
        entry["agent_existed"] = existed
        entry["mutation_sequence"] = sequence
        entry["tool"] = tool
        self._write_manifest(turn_id, manifest)

    def list_files(self, turn_id: str) -> list[dict[str, Any]]:
        manifest = self._load_manifest(turn_id, create=False)
        return [
            {"path": path, **entry}
            for path, entry in sorted(manifest.get("files", {}).items())
        ]

    def preview_restore(
        self, turn_id: str, *, paths: list[str] | None = None
    ) -> list[dict[str, Any]]:
        manifest = self._load_manifest(turn_id, create=False)
        files = manifest.get("files", {})
        if not files:
            raise ToolError("CHECKPOINT_NOT_FOUND", "No checkpoint exists for this turn")

        selected = self._selected_entries(files, paths)
        turn_root = self.root / turn_id
        return [
            self._preview_entry(turn_root, relative, entry)
            for relative, entry in selected
        ]

    def restore(
        self,
        turn_id: str,
        *,
        paths: list[str] | None = None,
        force: bool = False,
        confirmed_hashes: dict[str, str | None] | None = None,
    ) -> list[str]:
        manifest = self._load_manifest(turn_id, create=False)
        files = manifest.get("files", {})
        if not files:
            raise ToolError("CHECKPOINT_NOT_FOUND", "No checkpoint exists for this turn")

        selected = self._selected_entries(files, paths)
        preview = self.preview_restore(turn_id, paths=[path for path, _ in selected])
        conflicts = [item for item in preview if item["conflict"]]
        if conflicts and not force:
            raise ToolError(
                "RESTORE_CONFLICT",
                "Files changed after the Agent's last recorded mutation",
                data={"conflicts": conflicts, "preview": preview},
            )
        if conflicts and force:
            confirmations = confirmed_hashes or {}
            stale = [
                item
                for item in conflicts
                if item["path"] not in confirmations
                or confirmations[item["path"]] != item["current_sha256"]
            ]
            if stale:
                raise ToolError(
                    "RESTORE_CONFIRMATION_STALE",
                    "Conflicting files changed after restore confirmation",
                    data={"conflicts": stale, "preview": preview},
                )

        restored: list[str] = []
        turn_root = self.root / turn_id
        for relative, entry in selected:
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
                if _sha256(backup) != entry.get("sha256"):
                    raise ToolError(
                        "CHECKPOINT_CORRUPT",
                        f"Baseline backup hash mismatch for {relative}",
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

    def _selected_entries(
        self,
        files: dict[str, Any],
        paths: list[str] | None,
    ) -> list[tuple[str, dict[str, Any]]]:
        if paths is None:
            selected_paths = sorted(files)
        else:
            selected_paths = []
            for user_path in paths:
                resolved = self.workspace.resolve(user_path, must_exist=False)
                relative = self.workspace.relative(resolved)
                if relative not in files:
                    raise ToolError(
                        "CHECKPOINT_NOT_FOUND",
                        f"File is not part of this checkpoint: {relative}",
                    )
                if relative not in selected_paths:
                    selected_paths.append(relative)
        if not selected_paths:
            raise ToolError("INVALID_ARGUMENTS", "At least one checkpoint file is required")
        return [(path, files[path]) for path in selected_paths]

    def _preview_entry(
        self,
        turn_root: Path,
        relative: str,
        entry: dict[str, Any],
    ) -> dict[str, Any]:
        target = self.workspace.resolve(relative, must_exist=False)
        current_exists = target.is_file()
        current_hash = _sha256(target) if current_exists else None
        agent_existed = entry.get("agent_existed")
        agent_hash = entry.get("agent_sha256")
        tracked = isinstance(agent_existed, bool)
        conflict = not tracked or current_exists != agent_existed or (
            current_exists and current_hash != agent_hash
        )

        agent_path = _manifest_path(turn_root, entry.get("agent_backup"))
        baseline_path = _manifest_path(turn_root, entry.get("backup"))
        return {
            "path": relative,
            "conflict": conflict,
            "reason": (
                "Agent output was not recorded"
                if not tracked
                else "Current file differs from the latest Agent output"
                if conflict
                else "Current file still matches the latest Agent output"
            ),
            "baseline_existed": bool(entry.get("existed")),
            "baseline_sha256": entry.get("sha256"),
            "agent_existed": agent_existed,
            "agent_sha256": agent_hash,
            "current_existed": current_exists,
            "current_sha256": current_hash,
            "mutation_sequence": entry.get("mutation_sequence"),
            "tool": entry.get("tool"),
            "external_diff": _text_diff(agent_path, target, relative) if conflict else "",
            "restore_diff": _text_diff(target, baseline_path, relative),
        }

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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_path(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = (root / value).resolve()
    if root.resolve() not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def _text_diff(before: Path | None, after: Path | None, label: str) -> str:
    before_lines = _text_lines(before)
    after_lines = _text_lines(after)
    if before_lines is None or after_lines is None:
        return "Binary, missing, or oversized content cannot be previewed"
    return "".join(
        unified_diff(
            before_lines,
            after_lines,
            fromfile=f"agent/{label}",
            tofile=f"current/{label}",
            n=3,
        )
    )[:20_000]


def _text_lines(path: Path | None) -> list[str] | None:
    if path is None:
        return []
    try:
        if path.stat().st_size > 1_000_000:
            return None
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
