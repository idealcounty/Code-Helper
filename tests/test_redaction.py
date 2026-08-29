from __future__ import annotations

import asyncio
import json
from pathlib import Path

from coding_agent.events import AgentEvent, EventBus, EventStore
from coding_agent.redaction import REDACTED, Redactor
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools.registry import ToolRegistry


def test_event_store_and_listeners_receive_redacted_data(tmp_path: Path) -> None:
    secret = "custom-secret-value-123"
    redactor = Redactor([secret])
    store = EventStore(tmp_path, "session", redactor=redactor)
    bus = EventBus(store)
    seen: list[AgentEvent] = []
    bus.subscribe(seen.append)
    event = AgentEvent(
        type="tool_requested",
        session_id="session",
        turn_id="turn",
        payload={
            "api_key": secret,
            "command": f"curl -H 'Authorization: Bearer {secret}' https://user:pass@example.test",
        },
    )

    returned = asyncio.run(bus.publish(event))
    persisted_text = store.path.read_text(encoding="utf-8")

    assert returned.payload["api_key"] == secret
    assert secret not in persisted_text
    assert secret not in json.dumps(seen[0].to_dict(), ensure_ascii=False)
    assert seen[0].payload["api_key"] == REDACTED
    assert store.load()[0]["payload"]["api_key"] == REDACTED


def test_redactor_catches_common_key_shapes() -> None:
    redactor = Redactor()
    text = "sk-abcdefghijklmnopqrstuvwxyz and AKIA1234567890ABCDEF"

    safe = redactor.redact_text(text)

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in safe
    assert "AKIA1234567890ABCDEF" not in safe
    assert safe.count(REDACTED) == 2


def test_full_tool_output_reference_is_redacted(tmp_path: Path) -> None:
    secret = "output-secret-456"
    registry = ToolRegistry()
    executor = ToolExecutor(
        registry,
        result_store=tmp_path / "tool-results",
        redactor=Redactor([secret]),
    )
    from coding_agent.tools.base import ToolResult

    result = ToolResult.success(
        "large output",
        metadata={"_full_stdout": f"prefix {secret} suffix", "_full_stderr": ""},
    )
    executor._persist_full_output(result)

    artifact = next((tmp_path / "tool-results").glob("*.json"))
    assert secret not in artifact.read_text(encoding="utf-8")
    assert result.data["result_reference"].startswith(".code-helper")


def test_full_tool_output_store_prunes_old_results_to_quota(tmp_path: Path) -> None:
    from coding_agent.tools.base import ToolResult

    result_store = tmp_path / "tool-results"
    result_store.mkdir()
    (result_store / "tool-result-old.json").write_text("x" * 120, encoding="utf-8")
    (result_store / "tool-result-newer.json").write_text("y" * 120, encoding="utf-8")
    executor = ToolExecutor(
        ToolRegistry(),
        result_store=result_store,
        result_store_max_bytes=260,
        result_store_max_files=4,
    )

    result = ToolResult.success(
        "large output", metadata={"_full_stdout": "z" * 80, "_full_stderr": ""}
    )
    executor._persist_full_output(result)

    files = list(result_store.glob("tool-result-*.json"))
    assert len(files) <= 2
    assert result.data.get("result_reference")
    assert result.data.get("result_store_pruned", 0) >= 1


def test_full_tool_output_larger_than_quota_is_not_persisted(tmp_path: Path) -> None:
    from coding_agent.tools.base import ToolResult

    result_store = tmp_path / "tool-results"
    executor = ToolExecutor(
        ToolRegistry(), result_store=result_store, result_store_max_bytes=32
    )
    result = ToolResult.success(
        "large output", metadata={"_full_stdout": "z" * 500, "_full_stderr": ""}
    )

    executor._persist_full_output(result)

    assert result.data["result_store_error"] == "RESULT_STORE_QUOTA"
    assert not list(result_store.glob("tool-result-*.json"))
