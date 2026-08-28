from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.context import ContextManager
from coding_agent.config import AppConfig
from coding_agent.memory import MemoryStore
from coding_agent.permissions import PermissionDecision, PermissionPolicy
from coding_agent.session import AgentState
from coding_agent.runtime import create_runtime
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry
from coding_agent.tools.memory import register_memory_tools


def test_memory_persists_searches_and_forgets_across_instances(tmp_path: Path) -> None:
    root = tmp_path / ".code-helper" / "memory"
    first = MemoryStore(root)
    decision = first.remember(
        category="decision",
        content="前端使用原生 JavaScript，不引入 React 构建链。",
        keywords=["frontend", "javascript", "architecture"],
        importance=5,
        source_session_id="session-old",
        source_turn_id="turn-old",
    )
    first.remember(
        category="task",
        content="后续补充 Windows 安装包测试。",
        keywords=["windows", "package"],
        importance=2,
    )

    restored = MemoryStore(root)
    matches = restored.search("前端 JavaScript 技术栈", limit=5)

    assert matches and matches[0].id == decision.id
    assert matches[0].source_session_id == "session-old"
    assert restored.stats()["categories"]["decision"] == 1
    assert restored.forget(decision.id) is True
    assert MemoryStore(root).get(decision.id) is None
    assert MemoryStore(root).search("前端 JavaScript") == []


def test_duplicate_memory_updates_existing_record(tmp_path: Path) -> None:
    store_a = MemoryStore(tmp_path / "memory")
    store_b = MemoryStore(tmp_path / "memory")
    first = store_a.remember(category="fact", content="Python 3.11 is required.")
    second = store_b.remember(
        category="fact",
        content="Python 3.11 is required.",
        keywords=["python"],
        importance=5,
    )

    assert first.id == second.id
    assert len(MemoryStore(tmp_path / "memory").list()) == 1
    assert MemoryStore(tmp_path / "memory").get(first.id).importance == 5


def test_context_automatically_recalls_relevant_cross_session_memory(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / ".code-helper" / "memory")
    remembered = store.remember(
        category="preference",
        content="用户偏好先运行最小测试，再执行完整测试套件。",
        keywords=["test", "verification"],
        importance=4,
        source_session_id="previous-session",
    )
    new_state = AgentState.create(session_id="new-session")
    new_state.messages.append(
        {"role": "user", "content": "修改完成以后应该怎样运行 test 进行验证？"}
    )

    context = ContextManager(memory_store=store).build(new_state, [])
    system = context.messages[0]["content"]

    assert "Relevant project memory from earlier conversations" in system
    assert remembered.content in system
    assert new_state.recalled_memories[0]["id"] == remembered.id


def test_memory_tools_use_existing_permission_pipeline(tmp_path: Path) -> None:
    state = AgentState.create(session_id="session-1")
    registry = ToolRegistry()
    store = MemoryStore(tmp_path / ".code-helper" / "memory")
    register_memory_tools(registry, store, state)
    executor = ToolExecutor(registry)

    saved = asyncio.run(
        executor.execute(
            "remember_project_memory",
            {
                "category": "fact",
                "content": "服务默认监听 127.0.0.1。",
                "keywords": ["server", "localhost"],
                "importance": 4,
            },
        )
    )
    searched = asyncio.run(
        executor.execute("search_project_memory", {"query": "服务 localhost"})
    )

    memory_id = saved.data["memory"]["id"]
    assert saved.ok and searched.data["memories"][0]["id"] == memory_id
    assert saved.data["memory"]["source_session_id"] == "session-1"

    policy = PermissionPolicy()
    remember_spec = registry.get("remember_project_memory")
    forget_spec = registry.get("forget_project_memory")
    arguments = {"category": "fact", "content": "x"}
    assert policy.evaluate(mode="act", spec=remember_spec, arguments=arguments).decision is PermissionDecision.ASK
    assert policy.evaluate(mode="ask", spec=remember_spec, arguments=arguments).decision is PermissionDecision.DENY
    assert policy.evaluate(mode="act", spec=forget_spec, arguments={"memory_id": memory_id}).decision is PermissionDecision.ASK


def test_invalid_memory_is_rejected_without_writing(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    try:
        store.remember(category="guess", content="not allowed")
    except ValueError as exc:
        assert "category" in str(exc)
    else:
        raise AssertionError("Invalid category should be rejected")
    assert store.list() == []


def test_new_runtime_recalls_memory_written_by_another_session(tmp_path: Path) -> None:
    config = AppConfig(api_key="test-key", base_url="https://example.invalid/v1")
    previous = create_runtime(
        config=config,
        workspace_path=tmp_path,
        session_id="previous-session",
    )
    previous.memory_store.remember(
        category="decision",
        content="Use pytest as the primary verification command.",
        keywords=["pytest", "verification"],
        importance=5,
        source_session_id=previous.state.session_id,
    )

    current = create_runtime(
        config=config,
        workspace_path=tmp_path,
        session_id="current-session",
    )
    current.state.messages.append(
        {"role": "user", "content": "Which pytest verification command should I run?"}
    )
    context = current.context_manager.build(current.state, current.registry.schemas())

    assert "Use pytest as the primary verification command." in context.messages[0]["content"]
    assert {
        "search_project_memory",
        "remember_project_memory",
        "forget_project_memory",
    }.issubset(current.registry.names())
