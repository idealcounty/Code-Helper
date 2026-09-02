from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from coding_agent.context import ContextManager
from coding_agent.config import AppConfig
from coding_agent.memory import MemoryStore
from coding_agent.memory_summary import SessionSummaryStore
from coding_agent.permissions import PermissionDecision, PermissionPolicy
from coding_agent.session import AgentState, AgentStatus
from coding_agent.user_memory import UserMemoryService
from coding_agent.runtime import create_runtime
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry
from coding_agent.tools.memory import register_memory_tools, register_user_memory_tools


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


def test_memory_lifecycle_metadata_is_persisted_and_search_filters_archived_expired(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    pinned = store.remember(category="fact", content="Keep this pinned")
    expired = store.remember(category="fact", content="Temporary fact")
    store.update_metadata(pinned.id, pinned=True, verification_status="verified")
    store.update_metadata(expired.id, expires_at="2000-01-01T00:00:00+00:00")
    assert store.get(pinned.id).pinned is True
    assert store.search("Keep this pinned")[0].id == pinned.id
    assert store.search("Temporary fact") == []
    assert store.stats()["expired"] == 1
    store.update_metadata(pinned.id, archived=True)
    assert store.search("Keep this pinned") == []
    assert store.list(limit=None)[0].archived is True


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


def test_turn_summary_candidate_requires_confirmation(tmp_path: Path) -> None:
    memories = MemoryStore(tmp_path / "memory")
    summaries = SessionSummaryStore(tmp_path / "summaries")
    state = AgentState.create(session_id="session-summary")
    state.current_objective = "我希望以后先运行最小测试"
    state.plan = [
        {"step": "实现功能", "status": "completed"},
        {"step": "执行完整回归测试", "status": "pending"},
    ]
    state.changed_files.add("src/app.py")

    summary = summaries.create(state, AgentStatus.PARTIAL, "基础功能已完成", memories)

    assert summary.objective == state.current_objective
    assert summary.completed_items == ["实现功能"]
    assert summary.pending_items == ["执行完整回归测试"]
    assert memories.list() == []
    candidate = summaries.candidates()[0]
    confirmed = summaries.confirm(candidate["id"], memories)
    assert confirmed and confirmed["status"] == "confirmed"
    assert memories.list()[0].content == candidate["content"]


def test_turn_summary_builds_readable_keyword_suggestion_and_tracks_repetition(
    tmp_path: Path,
) -> None:
    memories = MemoryStore(tmp_path / "memory")
    summaries = SessionSummaryStore(tmp_path / "summaries")
    state = AgentState.create(session_id="keyword-session")
    state.turn_id = "turn-1"
    state.current_objective = "请使用 C++ 完成这道算法题，并分析时间复杂度"

    first = summaries.create(state, AgentStatus.COMPLETED, "done", memories)
    first_suggestion = next(
        candidate for candidate in first.candidates if candidate.source_kind == "conversation_keywords"
    )

    assert first_suggestion.keywords == ["C++", "算法", "复杂度"]
    assert first_suggestion.occurrence_count == 1
    assert first_suggestion.work_type == "算法问题求解"
    assert first_suggestion.prompt.startswith("本轮对话中识别到")
    assert "是否将" in first_suggestion.prompt and "存入记忆区" in first_suggestion.prompt
    assert memories.list() == []

    state.turn_id = "turn-2"
    state.current_objective = "继续用 C++ 解决算法题，注意复杂度分析"
    second = summaries.create(state, AgentStatus.COMPLETED, "done", memories)
    repeated = next(
        candidate for candidate in second.candidates if candidate.source_kind == "conversation_keywords"
    )

    assert repeated.occurrence_count == 2
    assert repeated.prompt.startswith("我们检测到你在最近 2 次对话中经常使用")
    assert "完成算法问题求解" in repeated.prompt
    pending_profiles = [
        item for item in summaries.candidates() if item["source_kind"] == "conversation_keywords"
    ]
    assert [item["id"] for item in pending_profiles] == [repeated.id]

    confirmed = summaries.confirm(repeated.id, memories)
    assert confirmed and confirmed["status"] == "confirmed"
    assert memories.list()[0].keywords == ["c++", "算法", "复杂度"]


def test_keyword_work_type_does_not_match_short_english_marker_inside_word(
    tmp_path: Path,
) -> None:
    summaries = SessionSummaryStore(tmp_path / "summaries")
    state = AgentState.create(session_id="english-keywords")
    state.current_objective = "Build the authentication feature and update repository code"

    summary = summaries.create(
        state,
        AgentStatus.COMPLETED,
        "done",
        MemoryStore(tmp_path / "memory"),
    )
    suggestion = next(
        candidate for candidate in summary.candidates if candidate.source_kind == "conversation_keywords"
    )

    assert suggestion.work_type == "项目功能开发"


def test_keyword_suggestion_does_not_treat_because_as_use_decision(
    tmp_path: Path,
) -> None:
    summaries = SessionSummaryStore(tmp_path / "summaries")
    state = AgentState.create(session_id="english-decision-boundary")
    state.current_objective = "Explain this algorithm because performance matters"

    summary = summaries.create(
        state,
        AgentStatus.COMPLETED,
        "done",
        MemoryStore(tmp_path / "memory"),
    )
    suggestion = next(
        candidate for candidate in summary.candidates if candidate.source_kind == "conversation_keywords"
    )

    assert suggestion.category == "preference"
    assert suggestion.work_type == "算法问题求解"


def test_memory_filters_hybrid_ranking_and_conflicts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "api.py").write_text("def serve(): pass\n", encoding="utf-8")

    def embedding(text: str) -> list[float]:
        return [1.0, 0.0] if "semantic-match" in text else [0.8, 0.2]

    store = MemoryStore(tmp_path / "memory", workspace_root=tmp_path, embedding_provider=embedding)
    older = store.remember(category="decision", subject="web-stack", content="Use Flask", file_paths=["missing.py"], symbols=["serve"], source_session_id="old")
    newer = store.remember(category="decision", subject="web-stack", content="Use FastAPI semantic-match", file_paths=["src/api.py"], symbols=["serve"], source_session_id="new")

    matches = store.search_detailed("semantic-match", file_path="src/api.py", symbol="serve", source_session_id="new")
    conflicts = store.search_detailed("Use", symbol="serve", limit=10)

    assert matches[0]["id"] == newer.id and matches[0]["semantic_score"] > 0
    assert matches[0]["repository_evidence"] == "verified"
    by_id = {item["id"]: item for item in conflicts}
    assert older.id in by_id[newer.id]["conflict_ids"]
    assert by_id[newer.id]["is_latest_for_subject"] is True
    assert by_id[older.id]["repository_evidence"] == "missing"


def test_search_returns_nonmatching_conflict_with_matching_memory(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    older = store.remember(
        category="decision", subject="framework", content="Keep the legacy Flask server"
    )
    newer = store.remember(
        category="decision", subject="framework", content="Migrate the API to FastAPI"
    )

    matches = store.search_detailed("legacy Flask", limit=6)

    assert [item["id"] for item in matches] == [older.id, newer.id]
    assert matches[0]["conflict_ids"] == [newer.id]
    assert all(item["recency_score"] > 0 for item in matches)


def test_user_memory_is_opt_in_separate_exportable_and_clearable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_root = tmp_path / "outside-workspace" / "user-memory"
    service = UserMemoryService(user_root)
    registry = ToolRegistry()
    state = AgentState.create(session_id="user-session")
    register_user_memory_tools(registry, service, state)
    executor = ToolExecutor(registry)

    disabled = asyncio.run(executor.execute("search_user_memory", {"query": "tests"}))
    assert disabled.ok is False and service.enabled is False
    service.set_enabled(True)
    saved = asyncio.run(executor.execute("remember_user_memory", {"category": "preference", "content": "Always run focused tests first."}))
    exported = service.export()

    assert saved.ok and saved.data["memory"]["scope"] == "user"
    assert user_root not in workspace.parents and not str(user_root).startswith(str(workspace))
    assert exported["memories"][0]["content"] == "Always run focused tests first."
    assert service.clear() == 1 and service.store.list() == []
    service.set_enabled(False)
    assert service.enabled is False


def test_memory_store_rejects_cross_scope_writes(tmp_path: Path) -> None:
    project_store = MemoryStore(tmp_path / "project", scope="project")
    try:
        project_store.remember(category="preference", content="global preference", scope="user")
    except ValueError as exc:
        assert "Cannot write user memory" in str(exc)
    else:
        raise AssertionError("Cross-scope memory write should be rejected")


def test_store_ignores_invalid_and_cross_scope_audit_records(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    valid = store.remember(category="fact", content="The API is local only.")
    record = json.loads(store.path.read_text(encoding="utf-8").splitlines()[0])
    invalid_category = {**record, "id": "invalid-category", "category": "guess"}
    wrong_scope = {**record, "id": "wrong-scope", "scope": "user"}
    wrong_scope_delete = {
        "operation": "delete",
        "id": valid.id,
        "scope": "user",
        "deleted_at": "2026-08-28T00:00:00+00:00",
    }
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(invalid_category) + "\n")
        handle.write(json.dumps(wrong_scope) + "\n")
        handle.write(json.dumps(wrong_scope_delete) + "\n")

    stats = MemoryStore(tmp_path / "memory").stats()

    assert [item.id for item in store.list(limit=None)] == [valid.id]
    assert stats["audit_records"] == 4
    assert stats["invalid_records"] == 3


def test_user_memory_export_and_clear_are_not_limited_to_500_records(
    tmp_path: Path,
) -> None:
    service = UserMemoryService(tmp_path / "user-memory")
    service.store.root.mkdir(parents=True)
    records = [
        {
            "operation": "upsert",
            "id": f"memory-{index}",
            "category": "preference",
            "content": f"Preference {index}",
            "keywords": [],
            "importance": 3,
            "source_session_id": "",
            "source_turn_id": "",
            "created_at": "2026-08-28T00:00:00+00:00",
            "updated_at": "2026-08-28T00:00:00+00:00",
            "scope": "user",
        }
        for index in range(501)
    ]
    service.store.path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    assert len(service.export()["memories"]) == 501
    assert service.clear() == 501
    assert service.store.list(limit=None) == []


def test_memory_duplicate_and_candidate_resolution_are_serialized(
    tmp_path: Path,
) -> None:
    memory_root = tmp_path / "memory"
    stores = [MemoryStore(memory_root), MemoryStore(memory_root)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = list(
            pool.map(
                lambda store: store.remember(
                    category="fact", content="Python 3.11 is required."
                ),
                stores,
            )
        )
    assert saved[0].id == saved[1].id
    assert len(stores[0].list(limit=None)) == 1

    summary_root = tmp_path / "summaries"
    summaries = [SessionSummaryStore(summary_root), SessionSummaryStore(summary_root)]
    state = AgentState.create(session_id="concurrent-session")
    state.current_objective = "I prefer focused tests first"
    summaries[0].create(state, AgentStatus.COMPLETED, "done", stores[0])
    candidate_id = summaries[0].candidates()[0]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmed = pool.submit(summaries[0].confirm, candidate_id, stores[0])
        rejected = pool.submit(summaries[1].reject, candidate_id)
    assert sum(result is not None for result in (confirmed.result(), rejected.result())) == 1


def test_internal_observation_does_not_change_user_memory_recall(tmp_path: Path) -> None:
    service = UserMemoryService(tmp_path / "user-memory", initially_enabled=True)
    preferred = service.store.remember(
        category="preference",
        content="Run focused pytest checks before the full suite.",
        keywords=["pytest", "focused"],
        scope="user",
    )
    service.store.remember(
        category="preference",
        content="Use cargo for Rust verification.",
        keywords=["cargo", "verification"],
        scope="user",
    )
    state = AgentState.create()
    state.messages.extend(
        [
            {"role": "user", "content": "How should I run pytest?"},
            {
                "role": "user",
                "content": "SYSTEM OBSERVATION: cargo verification is still required",
            },
        ]
    )

    ContextManager(user_memory=service).build(state, [])

    assert state.recalled_user_memories[0]["id"] == preferred.id


def test_project_memory_tool_handles_candidates_and_missing_records(tmp_path: Path) -> None:
    state = AgentState.create(session_id="tool-session")
    store = MemoryStore(tmp_path / "memory")
    registry = ToolRegistry()
    register_memory_tools(registry, store, state)
    executor = ToolExecutor(registry)

    missing_forget = asyncio.run(
        executor.execute("forget_project_memory", {"memory_id": "missing"})
    )
    missing_confirm = asyncio.run(
        executor.execute("confirm_memory_candidate", {"candidate_id": "missing"})
    )
    missing_reject = asyncio.run(
        executor.execute("reject_memory_candidate", {"candidate_id": "missing"})
    )
    invalid = asyncio.run(
        executor.execute(
            "remember_project_memory", {"category": "invalid", "content": "bad"}
        )
    )

    assert missing_forget.code == "MEMORY_NOT_FOUND"
    assert missing_confirm.code == "MEMORY_CANDIDATE_NOT_FOUND"
    assert missing_reject.code == "MEMORY_CANDIDATE_NOT_FOUND"
    assert invalid.code == "INVALID_ARGUMENTS"


def test_project_memory_candidate_tool_lists_confirms_and_rejects(
    tmp_path: Path,
) -> None:
    state = AgentState.create(session_id="candidate-session")
    state.turn_id = "turn-1"
    state.current_objective = "I prefer focused tests first"
    summaries = SessionSummaryStore(tmp_path / "summaries")
    store = MemoryStore(tmp_path / "memory")
    summaries.create(state, AgentStatus.PARTIAL, "finish later", store)
    registry = ToolRegistry()
    register_memory_tools(registry, store, state, summaries)
    executor = ToolExecutor(registry)

    listed = asyncio.run(executor.execute("list_memory_candidates", {}))
    candidate_id = listed.data["candidates"][0]["id"]
    confirmed = asyncio.run(
        executor.execute("confirm_memory_candidate", {"candidate_id": candidate_id})
    )
    empty = asyncio.run(executor.execute("list_memory_candidates", {}))

    assert listed.ok and listed.data["candidates"]
    assert confirmed.ok and confirmed.data["candidate"]["status"] == "confirmed"
    assert len(empty.data["candidates"]) == 1
    assert empty.data["candidates"][0]["category"] == "task"

    state.current_objective = "I always document decisions"
    state.turn_id = "turn-2"
    summaries.create(state, AgentStatus.PARTIAL, "later", store)
    pending = asyncio.run(executor.execute("list_memory_candidates", {}))
    rejected = asyncio.run(
        executor.execute(
            "reject_memory_candidate",
            {"candidate_id": pending.data["candidates"][0]["id"]},
        )
    )
    assert rejected.ok and rejected.data["candidate"]["status"] == "rejected"


def test_project_memory_tool_without_summary_store_returns_empty_candidates(
    tmp_path: Path,
) -> None:
    state = AgentState.create(session_id="no-summary")
    registry = ToolRegistry()
    register_memory_tools(registry, MemoryStore(tmp_path / "memory"), state)
    result = asyncio.run(ToolExecutor(registry).execute("list_memory_candidates", {}))

    assert result.ok and result.data["candidates"] == []


def test_user_memory_tools_enforce_opt_in_and_toggle_state(tmp_path: Path) -> None:
    service = UserMemoryService(tmp_path / "user-memory")
    state = AgentState.create(session_id="user-tool")
    registry = ToolRegistry()
    register_user_memory_tools(registry, service, state)
    executor = ToolExecutor(registry)

    disabled_search = asyncio.run(executor.execute("search_user_memory", {"query": "x"}))
    disabled_remember = asyncio.run(
        executor.execute(
            "remember_user_memory", {"category": "fact", "content": "x"}
        )
    )
    disabled_clear = asyncio.run(executor.execute("clear_user_memory", {}))
    exported = asyncio.run(executor.execute("export_user_memory", {}))
    invalid_toggle = asyncio.run(
        executor.execute("set_user_memory_enabled", {"enabled": "yes"})
    )
    enabled = asyncio.run(
        executor.execute("set_user_memory_enabled", {"enabled": True})
    )
    saved = asyncio.run(
        executor.execute(
            "remember_user_memory", {"category": "fact", "content": "remembered"}
        )
    )
    searched = asyncio.run(executor.execute("search_user_memory", {"query": "remembered"}))
    cleared = asyncio.run(executor.execute("clear_user_memory", {}))
    disabled = asyncio.run(
        executor.execute("set_user_memory_enabled", {"enabled": False})
    )

    assert disabled_search.code == "USER_MEMORY_DISABLED"
    assert disabled_remember.code == "USER_MEMORY_DISABLED"
    assert disabled_clear.code == "USER_MEMORY_DISABLED"
    assert exported.ok and exported.data["enabled"] is False
    assert invalid_toggle.code == "INVALID_ARGUMENTS"
    assert enabled.ok and enabled.data["enabled"] is True
    assert saved.ok and searched.data["memories"]
    assert cleared.ok and cleared.data["cleared"] == 1
    assert disabled.ok and disabled.data["enabled"] is False
