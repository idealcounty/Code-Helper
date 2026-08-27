from __future__ import annotations

from pathlib import Path

from coding_agent.checkpoints import CheckpointManager
from coding_agent.tools import Workspace


def test_checkpoint_restores_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("before\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))

    capture = manager.capture("turn-1", "sample.py")
    path.write_text("after\n", encoding="utf-8")
    restored = manager.restore("turn-1")

    assert capture.created is True
    assert capture.existed is True
    assert restored == ["sample.py"]
    assert path.read_text(encoding="utf-8") == "before\n"


def test_checkpoint_removes_file_created_by_agent(tmp_path: Path) -> None:
    path = tmp_path / "new.py"
    manager = CheckpointManager(Workspace(tmp_path))

    capture = manager.capture("turn-2", "new.py")
    path.write_text("created\n", encoding="utf-8")
    manager.restore("turn-2")

    assert capture.existed is False
    assert path.exists() is False


def test_first_snapshot_wins_for_multiple_edits(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("original\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))

    first = manager.capture("turn-3", "sample.py")
    path.write_text("first edit\n", encoding="utf-8")
    second = manager.capture("turn-3", "sample.py")
    path.write_text("second edit\n", encoding="utf-8")
    manager.restore("turn-3")

    assert first.created is True
    assert second.created is False
    assert path.read_text(encoding="utf-8") == "original\n"

