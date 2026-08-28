from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.checkpoints import CheckpointManager
from coding_agent.tools import Workspace
from coding_agent.tools.base import ToolError


def test_checkpoint_restores_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("before\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))

    capture = manager.capture("turn-1", "sample.py")
    path.write_text("after\n", encoding="utf-8")
    manager.record_mutation("turn-1", "sample.py", sequence=3, tool="apply_patch")
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
    manager.record_mutation("turn-2", "new.py", sequence=4, tool="write_file")
    manager.restore("turn-2")

    assert capture.existed is False
    assert path.exists() is False


def test_first_snapshot_wins_for_multiple_edits(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("original\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))

    first = manager.capture("turn-3", "sample.py")
    path.write_text("first edit\n", encoding="utf-8")
    manager.record_mutation("turn-3", "sample.py", sequence=2, tool="apply_patch")
    second = manager.capture("turn-3", "sample.py")
    path.write_text("second edit\n", encoding="utf-8")
    manager.record_mutation("turn-3", "sample.py", sequence=5, tool="apply_patch")
    manager.restore("turn-3")

    assert first.created is True
    assert second.created is False
    assert path.read_text(encoding="utf-8") == "original\n"


def test_restore_rejects_external_edit_after_agent_mutation(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("before\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))
    manager.capture("turn-conflict", "sample.py")
    path.write_text("agent edit\n", encoding="utf-8")
    manager.record_mutation(
        "turn-conflict", "sample.py", sequence=8, tool="apply_patch"
    )

    path.write_text("user edit\n", encoding="utf-8")

    with pytest.raises(ToolError) as raised:
        manager.restore("turn-conflict")

    assert raised.value.code == "RESTORE_CONFLICT"
    assert path.read_text(encoding="utf-8") == "user edit\n"
    conflict = raised.value.data["conflicts"][0]
    assert conflict["path"] == "sample.py"
    assert "-agent edit" in conflict["external_diff"]
    assert "+user edit" in conflict["external_diff"]


def test_checkpoint_rejects_file_changed_before_agent_result_is_recorded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sample.py"
    path.write_text("before\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))
    manager.capture("turn-race", "sample.py")
    path.write_text("unexpected external edit\n", encoding="utf-8")

    with pytest.raises(ToolError) as raised:
        manager.record_mutation(
            "turn-race",
            "sample.py",
            sequence=8,
            tool="apply_patch",
            expected_sha256="0" * 64,
        )

    assert raised.value.code == "CHECKPOINT_CONFLICT"
    preview = manager.preview_restore("turn-race")
    assert preview[0]["conflict"] is True


def test_force_restore_can_overwrite_an_acknowledged_conflict(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("before\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))
    manager.capture("turn-force", "sample.py")
    path.write_text("agent edit\n", encoding="utf-8")
    manager.record_mutation("turn-force", "sample.py", sequence=8, tool="apply_patch")
    path.write_text("user edit\n", encoding="utf-8")

    conflict = manager.preview_restore("turn-force")[0]
    restored = manager.restore(
        "turn-force",
        force=True,
        confirmed_hashes={"sample.py": conflict["current_sha256"]},
    )

    assert restored == ["sample.py"]
    assert path.read_text(encoding="utf-8") == "before\n"


def test_force_restore_rejects_a_stale_conflict_confirmation(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("before\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))
    manager.capture("turn-stale-confirmation", "sample.py")
    path.write_text("agent edit\n", encoding="utf-8")
    manager.record_mutation(
        "turn-stale-confirmation", "sample.py", sequence=8, tool="apply_patch"
    )
    path.write_text("first user edit\n", encoding="utf-8")
    observed_hash = manager.preview_restore("turn-stale-confirmation")[0][
        "current_sha256"
    ]
    path.write_text("second user edit\n", encoding="utf-8")

    with pytest.raises(ToolError) as raised:
        manager.restore(
            "turn-stale-confirmation",
            force=True,
            confirmed_hashes={"sample.py": observed_hash},
        )

    assert raised.value.code == "RESTORE_CONFIRMATION_STALE"
    assert path.read_text(encoding="utf-8") == "second user edit\n"


def test_multi_file_restore_preflights_all_conflicts_atomically(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first before\n", encoding="utf-8")
    second.write_text("second before\n", encoding="utf-8")
    manager = CheckpointManager(Workspace(tmp_path))
    for sequence, path in enumerate((first, second), start=1):
        manager.capture("turn-multi", path.name)
        path.write_text(f"{path.stem} agent\n", encoding="utf-8")
        manager.record_mutation(
            "turn-multi", path.name, sequence=sequence, tool="apply_patch"
        )
    second.write_text("second user\n", encoding="utf-8")

    with pytest.raises(ToolError) as raised:
        manager.restore("turn-multi")

    assert raised.value.code == "RESTORE_CONFLICT"
    assert first.read_text(encoding="utf-8") == "first agent\n"
    assert second.read_text(encoding="utf-8") == "second user\n"


def test_restore_can_select_a_subset_of_checkpoint_files(tmp_path: Path) -> None:
    manager = CheckpointManager(Workspace(tmp_path))
    for path_name in ("first.py", "second.py"):
        path = tmp_path / path_name
        path.write_text("before\n", encoding="utf-8")
        manager.capture("turn-select", path_name)
        path.write_text("after\n", encoding="utf-8")
        manager.record_mutation(
            "turn-select", path_name, sequence=2, tool="apply_patch"
        )

    restored = manager.restore("turn-select", paths=["second.py"])

    assert restored == ["second.py"]
    assert (tmp_path / "first.py").read_text(encoding="utf-8") == "after\n"
    assert (tmp_path / "second.py").read_text(encoding="utf-8") == "before\n"
