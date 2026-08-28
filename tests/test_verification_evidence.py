from __future__ import annotations

from coding_agent.verification_evidence import build_verification_evidence
from coding_agent.session import AgentState


def _result(*, ok: bool = True, exit_code: int = 0) -> dict:
    return {
        "ok": ok,
        "data": {"exit_code": exit_code, "stdout": "verification output"},
    }


def _evidence(
    command: str,
    *,
    objective: str = "Fix the bug",
    changed_files: set[str] | None = None,
):
    return build_verification_evidence(
        command=command,
        purpose="verify",
        result=_result(),
        objective=objective,
        changed_files=changed_files or {"src/app.py"},
        started_sequence=4,
        finished_sequence=5,
    )


def test_informational_command_cannot_pose_as_verification() -> None:
    evidence = _evidence("echo ok")

    assert evidence.accepted is False
    assert evidence.kind == "unknown"
    assert "not verification" in evidence.reason


def test_known_test_command_is_accepted_with_structured_scope() -> None:
    evidence = _evidence("python -m pytest -q")

    assert evidence.accepted is True
    assert evidence.kind == "test"
    assert evidence.source == "related_test_inferred"
    assert evidence.related_files == ("src/app.py",)
    assert evidence.started_sequence == 4
    assert evidence.finished_sequence == 5


def test_explicit_user_custom_command_is_accepted() -> None:
    command = "python scripts/project_check.py --strict"
    evidence = _evidence(
        command,
        objective=f"After the edit, run `{command}` to verify the result.",
    )

    assert evidence.accepted is True
    assert evidence.kind == "custom"
    assert evidence.source == "user_requested"


def test_exit_status_masking_is_rejected() -> None:
    evidence = _evidence("python -m pytest -q || true")

    assert evidence.accepted is False
    assert "mask" in evidence.reason


def test_targeted_test_must_cover_every_changed_file() -> None:
    evidence = _evidence(
        "python -m pytest tests/test_app.py",
        changed_files={"src/app.py", "src/worker.py"},
    )

    assert evidence.accepted is False
    assert "worker.py" in evidence.reason


def test_verification_freshness_is_identical_after_event_restore() -> None:
    state = AgentState.create(session_id="restored")
    state.restore_from_events(
        [
            {
                "type": "turn_started",
                "turn_id": "turn-1",
                "sequence": 1,
                "payload": {"message": "Fix app.py"},
            },
            {
                "type": "tool_result",
                "turn_id": "turn-1",
                "sequence": 4,
                "payload": {
                    "id": "write-1",
                    "name": "apply_patch",
                    "result": {
                        "ok": True,
                        "metadata": {"mutated_files": ["app.py"]},
                    },
                },
            },
            {
                "type": "verification_recorded",
                "turn_id": "turn-1",
                "sequence": 7,
                "payload": {
                    "evidence": {
                        "command": "python -m pytest -q",
                        "accepted": True,
                    }
                },
            },
        ]
    )

    assert state.last_mutation_sequence == 4
    assert state.last_successful_verification_sequence == 7
    assert state.verification_is_fresh is True


def test_new_turn_does_not_inherit_old_verification_evidence() -> None:
    state = AgentState.create(session_id="restored")
    state.restore_from_events(
        [
            {"type": "turn_started", "turn_id": "turn-1", "sequence": 1, "payload": {"message": "First"}},
            {
                "type": "verification_recorded",
                "turn_id": "turn-1",
                "sequence": 2,
                "payload": {"evidence": {"command": "pytest", "accepted": True}},
            },
            {"type": "turn_started", "turn_id": "turn-2", "sequence": 3, "payload": {"message": "Second"}},
        ]
    )

    assert state.turn_id == "turn-2"
    assert state.verification_evidence == []
    assert state.last_successful_verification_sequence == 0
