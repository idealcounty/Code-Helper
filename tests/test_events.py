from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from coding_agent.events import AgentEvent, EventBus, EventStore


def test_event_store_prunes_old_sessions_but_keeps_active_session(tmp_path: Path) -> None:
    old = tmp_path / "old-session.jsonl"
    older = tmp_path / "older-session.jsonl"
    old.write_text("x" * 120, encoding="utf-8")
    older.write_text("y" * 120, encoding="utf-8")

    store = EventStore(
        tmp_path,
        "active-session",
        max_storage_bytes=260,
        max_session_files=2,
    )
    EventBus(store)
    store.append(
        AgentEvent(
            type="turn_started",
            session_id="active-session",
            turn_id="turn",
            payload={"message": "hello"},
        )
    )

    assert store.path.exists()
    assert len(list(tmp_path.glob("*.jsonl"))) <= 2
    assert any(item["code"] == "SESSION_PRUNED" for item in store.last_prune_diagnostics)


def test_event_bus_assigns_stable_metadata(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    bus = EventBus(store)
    event = asyncio.run(
        bus.publish(AgentEvent(type="turn_started", session_id="session", turn_id="turn"))
    )

    persisted = store.load()[0]
    assert event.sequence == 1
    assert event.schema_version == 1
    assert event.event_id
    assert persisted["event_id"] == event.event_id
    assert persisted["schema_version"] == 1


def test_event_bus_links_events_and_resumes_chain_after_restart(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    bus = EventBus(store)
    first = asyncio.run(
        bus.publish(AgentEvent(type="turn_started", session_id="session", turn_id="turn"))
    )
    second = asyncio.run(
        bus.publish(AgentEvent(type="context_built", session_id="session", turn_id="turn"))
    )
    explicit = asyncio.run(
        bus.publish(
            AgentEvent(
                type="tool_started",
                session_id="session",
                turn_id="turn",
                causation_id="approval-42",
            )
        )
    )

    assert second.causation_id == first.event_id
    assert explicit.causation_id == "approval-42"

    restarted = EventBus(store)
    resumed = asyncio.run(
        restarted.publish(AgentEvent(type="turn_finished", session_id="session", turn_id="turn"))
    )
    assert resumed.causation_id == explicit.event_id


def test_event_bus_reads_current_tail_without_loading_full_history(
    tmp_path: Path, monkeypatch
) -> None:
    store = EventStore(tmp_path, "session")
    bus = EventBus(store)
    last = asyncio.run(
        bus.publish(AgentEvent(type="turn_started", session_id="session", turn_id="turn"))
    )

    def fail_full_load():
        raise AssertionError("current event stores should use tail metadata")

    monkeypatch.setattr(store, "load", fail_full_load)
    restarted = EventBus(store)

    assert restarted.sequence == 1
    resumed = asyncio.run(
        restarted.publish(
            AgentEvent(type="turn_finished", session_id="session", turn_id="turn")
        )
    )
    assert resumed.sequence == 2
    assert resumed.causation_id == last.event_id


def test_event_store_discards_only_corrupt_trailing_line(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    valid = AgentEvent(type="turn_started", session_id="session", turn_id="turn").to_dict()
    store.path.write_text(
        json.dumps(valid, ensure_ascii=False) + "\n{" + "corrupt trailing" + "\n",
        encoding="utf-8",
    )

    loaded = store.load()

    assert len(loaded) == 1
    trailing = next(
        item
        for item in store.last_load_diagnostics
        if item["code"] == "TRAILING_EVENT_CORRUPT"
    )
    assert trailing["line"] == 2


def test_event_store_rejects_corruption_in_the_middle(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    valid = AgentEvent(type="turn_started", session_id="session", turn_id="turn").to_dict()
    line = json.dumps(valid, ensure_ascii=False)
    store.path.write_text(line + "\n{middle corruption\n" + line + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 2"):
        store.load()


def test_event_store_accepts_legacy_schema_with_diagnostic(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    legacy = AgentEvent(type="turn_started", session_id="session", turn_id="turn").to_dict()
    legacy.pop("schema_version")
    store.path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    assert len(store.load()) == 1
    assert any(
        item["code"] == "LEGACY_EVENT_SCHEMA_ASSUMED"
        for item in store.last_load_diagnostics
    )


def test_event_store_migrates_legacy_identity_for_restart(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    first = {
        "type": "turn_started",
        "session_id": "session",
        "turn_id": "turn",
        "payload": {"message": "hello"},
    }
    second = {
        "type": "context_built",
        "session_id": "session",
        "turn_id": "turn",
        "payload": {},
    }
    store.path.write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8"
    )

    loaded = store.load()
    assert [item["sequence"] for item in loaded] == [1, 2]
    assert all(str(item["event_id"]).startswith("legacy-") for item in loaded)
    assert len({item["event_id"] for item in loaded}) == 2
    assert any(
        item["code"] == "LEGACY_EVENT_SEQUENCE_ASSUMED"
        for item in store.last_load_diagnostics
    )
    assert any(
        item["code"] == "LEGACY_EVENT_ID_DERIVED"
        for item in store.last_load_diagnostics
    )

    restarted = EventBus(store)
    resumed = asyncio.run(
        restarted.publish(
            AgentEvent(type="turn_finished", session_id="session", turn_id="turn")
        )
    )
    assert resumed.sequence == 3
    assert resumed.causation_id == loaded[-1]["event_id"]


def test_event_store_migrates_legacy_envelope_fields_deterministically(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    store.path.write_text(
        json.dumps({"type": "turn_started"}) + "\n"
        + json.dumps({"type": "context_built", "payload": {}}) + "\n",
        encoding="utf-8",
    )

    loaded = store.load()

    assert all(item["session_id"] == "session" for item in loaded)
    assert len({item["turn_id"] for item in loaded}) == 1
    assert all(item["payload"] == {} for item in loaded)
    codes = {item["code"] for item in store.last_load_diagnostics}
    assert "LEGACY_EVENT_SESSION_ID_ASSUMED" in codes
    assert "LEGACY_EVENT_TURN_ID_ASSUMED" in codes
    assert "LEGACY_EVENT_PAYLOAD_ASSUMED" in codes


def test_event_store_rejects_non_object_legacy_payload(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    store.path.write_text(
        json.dumps({"type": "turn_started", "payload": ["unsafe"]}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="payload"):
        store.load()


@pytest.mark.parametrize("event_type", [None, "", "   ", 42])
def test_event_store_rejects_missing_or_invalid_event_type(
    tmp_path: Path, event_type: object
) -> None:
    store = EventStore(tmp_path, "session")
    store.path.write_text(
        json.dumps(
            {
                "type": event_type,
                "session_id": "session",
                "turn_id": "turn",
                "payload": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="event type"):
        store.load()


def test_event_store_rejects_event_from_another_session(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session-a")
    store.path.write_text(
        json.dumps(
            {
                "type": "turn_started",
                "session_id": "session-b",
                "turn_id": "turn",
                "payload": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="session_id"):
        store.load()


def test_event_store_rejects_invalid_live_event_before_append(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")

    with pytest.raises(ValueError, match="Event type"):
        store.append(
            AgentEvent(type="", session_id="session", turn_id="turn")
        )
    with pytest.raises(ValueError, match="session_id"):
        store.append(
            AgentEvent(
                type="turn_started",
                session_id="another-session",
                turn_id="turn",
            )
        )

    assert not store.path.exists()


def test_event_store_rejects_future_schema_without_silent_recovery(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    future = AgentEvent(type="turn_started", session_id="session", turn_id="turn").to_dict()
    future["schema_version"] = 99
    store.path.write_text(json.dumps(future) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="schema"):
        store.load()
    assert store.last_load_diagnostics[-1]["code"] == "UNSUPPORTED_EVENT_SCHEMA"
