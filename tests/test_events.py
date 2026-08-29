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


def test_event_store_rejects_future_schema_without_silent_recovery(tmp_path: Path) -> None:
    store = EventStore(tmp_path, "session")
    future = AgentEvent(type="turn_started", session_id="session", turn_id="turn").to_dict()
    future["schema_version"] = 99
    store.path.write_text(json.dumps(future) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="schema"):
        store.load()
    assert store.last_load_diagnostics[-1]["code"] == "UNSUPPORTED_EVENT_SCHEMA"
