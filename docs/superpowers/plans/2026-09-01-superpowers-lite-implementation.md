# Superpowers Lite V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有单 Agent Runtime 上实现可恢复、可观察、带最小门禁的 `inspect → plan → implement → verify → finish` 轻量开发流程。

**Architecture:** 复用现有 `AgentRunner`、`AgentState`、`SessionReducer`、`EventBus`、`ContextManager`、`ToolExecutor`、`PermissionPolicy` 与 `Verifier`。工作流事实写入追加事件，在线状态和重启恢复都由同一 Reducer 投影；状态感知 PreTool Hook 只收紧行为，不扩大权限。

**Tech Stack:** Python 3.13、dataclasses、FastAPI、现有 pytest/ScriptedModel、现有静态 Web UI；不新增依赖、不新增数据库、不新增 Agent Loop。

**Spec:** `docs/superpowers/specs/2026-09-01-superpowers-lite-development-workflow-design.md`

## Global Constraints

- 保持单 Agent 优先，不增加子 Agent、DAG、并行写操作或第二套执行循环。
- 所有写操作继续经过工作区边界、读取哈希、权限审批、检查点和现有 ToolExecutor。
- 工作流 Hook 只能拒绝或补充上下文，不能把原有 DENY/ASK 改成 ALLOW。
- 旧 Session 没有工作流事件时恢复为 `workflow_name=None`、`workflow_stage="idle"`，继续旧行为。
- 工作流状态属于当前 Turn；`begin_new_turn()` 必须清空上一轮工作流和已加载 Skill。
- `acceptance` 是自然语言展示字段，不能作为未经批准的 Shell 命令执行。
- 每个阶段先运行针对性测试，再运行受影响的全量测试；不混入无关 UI 重构或依赖升级。

### Task 1: Skill 内容与 Plan acceptance 契约

**Files:**
- Create: `skills/development-workflow/SKILL.md`
- Modify: `skills/add-feature/SKILL.md`
- Modify: `skills/bug-fix/SKILL.md`
- Modify: `skills/code-review/SKILL.md`
- Modify: `src/coding_agent/tools/plan.py`
- Test: `tests/test_skills.py`
- Test: `tests/test_plan.py`

**Interfaces:**
- `update_plan` 每个规范化步骤保留 `step`、`status`，可选 `acceptance`。
- `load_skill` 成功时仍返回现有 `ToolResult` 结构；四个 Skill 使用现有目录加载机制。

- [x] **Step 1: Write the failing tests**

```python
def test_update_plan_preserves_valid_acceptance(state_and_registry):
    result = run_tool("update_plan", {"steps": [
        {"step": "实现负数处理", "status": "in_progress", "acceptance": "回归测试通过"}
    ]})
    assert result.ok
    assert result.data["plan"][0]["acceptance"] == "回归测试通过"

def test_update_plan_rejects_acceptance_over_300_chars(state_and_registry):
    result = run_tool("update_plan", {"steps": [
        {"step": "实现功能", "acceptance": "x" * 301}
    ]})
    assert result.code == "INVALID_ARGUMENTS"

def test_development_workflow_routes_to_concrete_skill(tmp_path):
    library = SkillLibrary(tmp_path)
    assert library.load("development-workflow") is not None
    assert "add-feature" in library.load("development-workflow")[1]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_plan.py tests/test_skills.py`
Expected: FAIL because `acceptance` is currently discarded and the total workflow Skill is absent.

- [x] **Step 3: Write minimal implementation**

在 `update_plan` 中读取 `item.get("acceptance")`，省略时不写入字段；填写时要求字符串长度 1–300，并把规范化结果传给 `state.plan`。新增 `development-workflow/SKILL.md`，并让三个具体 Skill 明确触发条件、流程、完成证据和禁止事项。

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_plan.py tests/test_skills.py`
Expected: PASS，旧的无 `acceptance` 调用仍保持原结构兼容。

- [x] **Step 5: Run affected regression tests**

Run: `python -m pytest -q tests/test_plan.py tests/test_skills.py tests/test_agent_loop.py tests/test_context.py`
Expected: PASS。

### Task 2: 工作流状态、事件恢复与上下文注入

**Files:**
- Modify: `src/coding_agent/session.py`
- Modify: `src/coding_agent/session_reducer.py`
- Modify: `src/coding_agent/context.py`
- Modify: `src/coding_agent/agent_loop.py`
- Modify: `src/coding_agent/tools/skills.py`
- Test: `tests/test_session_reducer.py`
- Test: `tests/test_context.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- `AgentState.workflow_name: str | None`、`workflow_stage: str`、`loaded_skills: set[str]`。
- 新事件类型：`skill_loaded`、`workflow_selected`、`workflow_stage_changed`。
- Reducer 对新事件幂等；旧事件恢复默认 idle；`source_manifest` 增加 `workflow_state`。

- [x] **Step 1: Write the failing tests**

```python
def test_reducer_restores_workflow_events_like_live_projection():
    events = [
        event("turn_started", {"message": "修复 bug"}),
        event("skill_loaded", {"name": "bug-fix"}),
        event("workflow_selected", {"name": "bug-fix", "stage": "inspect"}),
        event("workflow_stage_changed", {"from": "inspect", "to": "implement"}),
    ]
    live = AgentState.create(); replay = AgentState.create()
    for item in events: live.apply_event(item)
    replay.restore_from_events(events)
    assert (replay.workflow_name, replay.workflow_stage, replay.loaded_skills) == (
        live.workflow_name, live.workflow_stage, live.loaded_skills
    )

def test_new_turn_clears_workflow_state(state):
    state.workflow_name = "bug-fix"; state.workflow_stage = "verify"; state.loaded_skills.add("bug-fix")
    state.begin_new_turn()
    assert state.workflow_name is None and state.workflow_stage == "idle" and not state.loaded_skills

def test_workflow_context_is_bounded_and_manifested(state, context_manager):
    state.workflow_name = "add-feature"; state.workflow_stage = "implement"; state.loaded_skills.add("add-feature")
    context = context_manager.build(state, [])
    assert "Current development workflow" in context.messages[0]["content"]
    assert any(item["kind"] == "workflow_state" for item in context.source_manifest)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_session_reducer.py tests/test_context.py tests/test_agent_loop.py`
Expected: FAIL because `AgentState` has no workflow fields/events and tool loading does not emit workflow facts.

- [x] **Step 3: Write minimal implementation**

增加固定阶段常量和三个状态字段；在 `begin_new_turn`、`restore_from_events` 与 Reducer reset 中清空/恢复它们。`load_skill` 成功后由 Runner 根据具体 Skill 名称发布 `skill_loaded`，并对三个具体 Skill 发布 `workflow_selected`；`development-workflow` 只作为路由 Skill。ContextManager 注入有字符上限的 `workflow_state` 块，不重复注入 Skill 全文。

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_session_reducer.py tests/test_context.py tests/test_agent_loop.py`
Expected: PASS，在线投影和离线恢复一致。

- [x] **Step 5: Run affected regression tests**

Run: `python -m pytest -q tests/test_session_reducer.py tests/test_context.py tests/test_agent_loop.py tests/test_events.py tests/test_recovery_matrix.py`
Expected: PASS，旧事件和旧 Session 无回归。

### Task 3: 工作流 Hook 守卫与完成门禁

**Files:**
- Modify: `src/coding_agent/runtime.py`
- Modify: `src/coding_agent/agent_loop.py`
- Modify: `src/coding_agent/verifier.py`
- Modify: `src/coding_agent/tools/plan.py`
- Test: `tests/test_workflow_guard.py`
- Test: `tests/test_verifier.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- `create_workflow_guard(state) -> PreToolHook` 只返回允许或拒绝，不执行工具、不修改计划、不改变原权限决定。
- `Verifier.evaluate` 在有具体工作流时要求可转换到 `finish`；无工作流保持旧逻辑。

- [x] **Step 1: Write the failing tests**

```python
async def test_add_feature_write_without_plan_is_denied(runtime):
    runtime.state.workflow_name = "add-feature"
    runtime.state.workflow_stage = "inspect"
    result = await runtime.tool_executor.execute("write_file", {"path": "x.py", "content": "pass"})
    assert result.code == "HOOK_DENIED"

async def test_code_review_write_is_denied(runtime):
    runtime.state.workflow_name = "code-review"
    runtime.state.workflow_stage = "inspect"
    result = await runtime.tool_executor.execute("write_file", {"path": "x.py", "content": "pass"})
    assert result.code == "WORKFLOW_DENIED"

def test_add_feature_requires_fresh_verification_and_acceptance(state, response):
    state.workflow_name = "add-feature"; state.workflow_stage = "verify"
    state.plan = [{"step": "实现功能", "status": "completed", "acceptance": "测试通过"}]
    state.changed_files.add("x.py")
    assert Verifier().evaluate(state, response).status is CompletionStatus.CONTINUE
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_workflow_guard.py tests/test_verifier.py tests/test_agent_loop.py`
Expected: FAIL because Runtime 尚未注册状态感知 Hook，Verifier 也不检查工作流完成条件。

- [x] **Step 3: Write minimal implementation**

在 Runtime 创建闭包 Hook：`add-feature` 无计划或无 `in_progress` 拒绝写，`code-review` 拒绝所有写工具，`finish` 拒绝写；其他情况交给原权限策略。Runner 在计划更新、成功变更、验证记录和验证失败时发布真实阶段转换事件。Verifier 对 add-feature/bug-fix/code-review 增加完成条件，并继续沿用 repair/rejection 上限。

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_workflow_guard.py tests/test_verifier.py tests/test_agent_loop.py`
Expected: PASS。

- [x] **Step 5: Run affected regression tests**

Run: `python -m pytest -q tests/test_workflow_guard.py tests/test_verifier.py tests/test_agent_loop.py tests/test_permissions.py tests/test_stuck_recovery.py tests/test_fault_injection_smoke.py`
Expected: PASS，Ask/Plan/Act、审批和取消行为不变。

### Task 4: API/UI 展示、确定性 Eval 与文档

**Files:**
- Modify: `src/coding_agent/web/app.py`
- Modify: `src/coding_agent/web/static/app.js`
- Modify: `src/coding_agent/web/static/index.html`
- Modify: `evals/scenarios.py`
- Create: `evals/tasks/12_add_feature_workflow.json`
- Create: `evals/tasks/13_bug_fix_workflow.json`
- Create: `evals/tasks/14_code_review_workflow.json`
- Modify: `docs/evals.md`
- Test: `tests/test_workflow_api.py`
- Test: `tests/test_workflow_eval.py`

**Interfaces:**
- Session JSON 增加可选 `workflow: {name, stage, loaded_skills}`。
- 计划步骤 JSON 可包含 `acceptance`；UI 展示流程名称、阶段、已加载 Skill 和验收条件。
- 轨迹显示三个新事件；旧前端忽略新增字段仍可用。

- [x] **Step 1: Write the failing tests**

```python
def test_session_api_exposes_workflow_state(client, session_id):
    payload = client.get(f"/api/sessions/{session_id}").json()
    assert payload["workflow"] == {"name": "bug-fix", "stage": "verify", "loaded_skills": ["bug-fix"]}

def test_plan_endpoint_returns_acceptance(client, session_id):
    response = client.get(f"/api/sessions/{session_id}")
    assert response.json()["plan"][0]["acceptance"] == "测试通过"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_workflow_api.py tests/test_workflow_eval.py`
Expected: FAIL because Session API and UI currently do not expose workflow projection or acceptance copy.

- [x] **Step 3: Write minimal implementation**

在 Session API、报告接口和 intelligence payload 中加入可选 workflow 对象；前端在计划面板顶部显示流程/阶段/Skill，在步骤下显示 acceptance，并把新事件映射到轨迹。扩展三个固定 Eval，记录 Skill 加载率、计划建立率、验证新鲜率、Review 写入次数、恢复阶段一致率；不引入真实模型作为唯一门禁。

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_workflow_api.py tests/test_workflow_eval.py`
Expected: PASS。

- [x] **Step 5: Run complete verification**

Run: `python scripts/run_quality_tests.py --include-evals --include-coverage --include-mutation --output-dir test-results/quality-run-superpowers-lite`
Expected: 所有确定性门禁通过；新增工作流契约 100%，Code Review 写入 0，修改任务新鲜验证率 100%，旧任务完成率不低于基线。

- [x] **Step 6: Update documentation**

更新 `docs/evals.md` 和本规格对应的实施状态，记录真实指标、已知限制以及没有实现多 Agent/DAG/第二循环的范围边界。

## Completion Audit

Before claiming completion, verify every checkbox above, inspect the final Session API and event log, replay a workflow after restart, run the full deterministic gate, and confirm `rg` finds no implementation that bypasses `PermissionPolicy`, `ToolExecutor`, or workspace boundaries.

## Final verification snapshot

2026-09-01 的最终收尾复验运行：

```powershell
python scripts/run_quality_tests.py --include-evals --include-coverage --include-mutation --output-dir test-results/quality-run-superpowers-lite-final8
```

结果为 16 个检查全部通过、397 个 Pytest 用例通过；14 个确定性 Eval 的契约/完成/安全/验证指标均为 100%，三个工作流 Eval 的 Skill 加载、计划完成、只读审查、恢复一致性和修改后新鲜验证指标均为 100%。完整机器可读证据见 `test-results/quality-run-superpowers-lite-final8/summary.md` 与 `test-results/quality-run-superpowers-lite-final8/eval/report.json`。
