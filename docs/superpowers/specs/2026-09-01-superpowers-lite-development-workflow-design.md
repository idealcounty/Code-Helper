# Code Helper 轻量 Superpowers 开发流程设计

## 1. 文档信息

- 文档类型：架构与开发流程设计
- 适用项目：`Code-Helper`
- 设计日期：2026-09-01
- 目标版本：Superpowers Lite V1
- 实施范围：现有单 Agent Runtime 的轻量扩展
- 本文状态：实现完成；确定性质量门禁与真实 DeepSeek 对照均已记录

## 2. 背景与目标

Code Helper 已经实现了一个自研、事件驱动、可审批、可恢复、会验证的本地 Coding Agent。当前主闭环具备以下能力：

- `AgentRunner` 推进模型请求、工具调用、观察和完成判断；
- `ContextManager` 注入项目规则、Repo Map、Skills、记忆和历史摘要；
- `ToolExecutor` 统一执行工具并经过参数校验、权限、Hooks 和结果规范化；
- `EventBus` 与 `SessionReducer` 共同提供在线状态投影和会话恢复；
- `SkillLibrary` 以摘要目录加 `load_skill` 的方式按需加载项目 Skills；
- `update_plan` 提供最多 12 步、单一 `in_progress` 的可见计划；
- `Verifier` 以计划状态和新鲜验证证据约束完成声明；
- 文件读取哈希、检查点、审批、取消、预算和卡死检测已经形成安全执行边界。

当前 Skills 仍然主要是提示性说明。模型即使没有严格执行 Skill，也可能继续修改代码；计划只表达步骤状态，无法记录验收条件；系统也没有明确记录“当前正在执行哪一种开发流程、处于哪个阶段”。

本设计的目标是在不引入多 Agent、DAG 调度器或新基础设施的前提下，让现有 Skills 从“建议性提示”升级为“有状态、可恢复、可验证的轻量开发流程”。

一句话目标：

> 复用现有 Skill、Plan、Event、Hook 和 Verifier，在单 Agent 内实现可观察、可恢复、带最小门禁的 inspect → plan → implement → verify → finish 开发流程。

## 3. 设计原则

### 3.1 必须保持的原则

1. **单 Agent 优先**：不增加子智能体、角色代理、并行实现者或独立审查代理。
2. **复用现有核心**：工作流必须经过现有 `AgentRunner`、`PermissionPolicy`、`ToolExecutor`、`EventBus` 和验证证据管线。
3. **状态来自事件**：工作流名称、阶段和已加载 Skill 必须能够从只追加事件恢复，不能只存在于模型上下文中。
4. **模型提出动作，代码执行门禁**：流程判断可以由模型参与，但安全边界和完成条件必须由确定性代码验证。
5. **小步纵向交付**：每个实施阶段都应形成可以独立测试和回滚的完整切片。
6. **保持向后兼容**：旧 Session 没有工作流事件时仍按现有逻辑恢复和运行。
7. **不扩大权限**：工作流和 Skill 只能收紧行为，不能绕过 Ask/Plan/Act、审批策略或工作区边界。

### 3.2 明确不做

Superpowers Lite V1 不包含：

- 多 Agent 或子 Agent；
- 任务依赖 DAG；
- 实现者、评审者和控制器的独立进程；
- 并行写操作或并行任务实现；
- 自动创建 Git worktree、分支、Commit、Push 或 PR；
- 独立工作流数据库；
- 通用 BPMN、工作流 DSL 或插件市场；
- 自动解析任意第三方 Superpowers 技能包；
- 对所有小任务强制生成正式设计文档；
- 取代现有权限、检查点、验证或恢复机制。

## 4. 当前架构与可复用扩展点

| 当前组件 | 已有能力 | Superpowers Lite 的复用方式 |
| --- | --- | --- |
| `src/coding_agent/skills.py` | 安全列出和加载项目 Skill | 保留懒加载，扩充工作流 Skill 内容 |
| `src/coding_agent/tools/skills.py` | 暴露 `list_skills`、`load_skill` | 成功加载后形成 `skill_loaded` 事实 |
| `src/coding_agent/tools/plan.py` | 创建和更新可见计划 | 为步骤增加可选 `acceptance` 字段 |
| `src/coding_agent/session.py` | 保存 Turn、计划、变更和验证状态 | 增加最小工作流状态 |
| `src/coding_agent/session_reducer.py` | 在线和恢复共用事件归约 | 恢复工作流选择、阶段和已加载 Skill |
| `src/coding_agent/context.py` | 构造系统上下文 | 注入当前工作流、阶段和阶段约束 |
| `src/coding_agent/agent_loop.py` | 工具循环、事件、完成检查 | 根据工具事实记录工作流事件 |
| `src/coding_agent/runtime.py` | 组装 Runtime 和内置 Hooks | 增加状态感知的轻量工作流守卫 Hook |
| `src/coding_agent/verifier.py` | 检查计划和新鲜验证 | 增加工作流级完成门禁 |
| `src/coding_agent/web/` | 展示计划、事件和运行状态 | 后续只增加工作流名称和阶段展示 |
| `tests/` 与 `evals/` | 确定性 Runtime 和 Agent 契约测试 | 增加三种工作流的端到端契约 |

该方案不需要改变模型 API、不需要更换消息协议，也不需要为工作流建立第二套执行循环。

## 5. 总体架构

```text
用户任务
  ↓
ContextManager 注入 Skill 摘要
  ↓
模型调用 load_skill
  ↓
AgentRunner 记录 skill_loaded / workflow_selected
  ↓
SessionReducer 投影 workflow_name / workflow_stage / loaded_skills
  ↓
模型按 Skill 执行 inspect / plan / implement / verify
  ↓
Runtime Hook 在写操作前执行最小流程门禁
  ↓
ToolExecutor 继续执行原有权限、审批、检查点和工具管线
  ↓
AgentRunner 根据计划、变更与验证事实推进阶段
  ↓
Verifier 检查计划、阶段、新鲜验证和流程约束
  ↓
COMPLETED / PARTIAL / FAILED / CANCELLED
```

Superpowers Lite 是现有 Agent Loop 上的一层流程约束，不是新的 Agent Loop。

## 6. Skill 体系设计

### 6.1 Skill 目录

保留当前三个 Skill，并新增一个总入口：

```text
skills/
├── development-workflow/
│   └── SKILL.md
├── add-feature/
│   └── SKILL.md
├── bug-fix/
│   └── SKILL.md
└── code-review/
    └── SKILL.md
```

### 6.2 `development-workflow`

职责：判断应加载哪个具体工作流，不直接承担实现步骤。

触发范围：

- 用户请求修改、修复或审查代码；
- 任务同时包含分析、修改和验证；
- 模型无法确定 `add-feature`、`bug-fix` 或 `code-review` 中哪一个适用。

路由规则：

| 用户目标 | 具体 Skill |
| --- | --- |
| 新行为、新接口、新页面、新工具 | `add-feature` |
| 已知缺陷、失败测试、回归、异常行为 | `bug-fix` |
| 只要求审查、审计、风险分析 | `code-review` |
| 纯项目问答或代码解释 | 不选择开发工作流，保持只读探索 |

该 Skill 必须明确：完成路由后加载具体 Skill；不能只加载总入口后直接实现。

### 6.3 `add-feature`

目标：以最小纵向切片实现新行为。

流程要求：

1. 明确可观察行为、边界和验收标准；
2. 阅读现有实现、调用方和相关测试；
3. 创建至少一个计划步骤，并为关键步骤填写 `acceptance`；
4. 每次只保持一个计划步骤为 `in_progress`；
5. 修改最小必要文件，不进行无关重构；
6. 增加或更新针对行为的测试；
7. 先运行针对性验证，必要时扩大到更广测试；
8. 所有计划步骤完成且验证新鲜后才总结。

### 6.4 `bug-fix`

目标：通过复现和回归测试证明根因与修复。

流程要求：

1. 读取报告、错误输出和最小相关路径；
2. 稳定复现问题；
3. 在修改前形成具体根因假设；
4. 增加或更新能捕获缺陷的回归测试；
5. 做最小修复；
6. 运行回归测试并确认通过；
7. 运行受影响范围的验证；
8. 总结根因、修改、证据和未覆盖风险。

简单单文件 Bug 不强制创建计划；跨文件或包含多个独立修复步骤时应使用现有 `update_plan`。

### 6.5 `code-review`

目标：只读检查 Diff 和相关代码，输出可执行的分级发现。

流程要求：

1. 读取 Diff、相关实现和测试；
2. 优先检查正确性、安全、数据损坏和回归；
3. 用测试或最小复现核实关键判断；
4. 按严重级别输出文件与行号；
5. 区分必须修复的问题和可选改进；
6. 不修改文件，不自动实施建议。

`code-review` 工作流下，写工具应被工作流 Hook 拒绝；用户后续明确要求修复时开始新的 Turn，并选择 `bug-fix` 或 `add-feature`。

## 7. 状态模型

### 7.1 `AgentState` 新增字段

只增加三个字段：

```python
workflow_name: str | None = None
workflow_stage: str = "idle"
loaded_skills: set[str] = field(default_factory=set)
```

字段语义：

| 字段 | 含义 |
| --- | --- |
| `workflow_name` | 当前 Turn 选择的具体工作流；允许值为 `add-feature`、`bug-fix`、`code-review` 或 `None` |
| `workflow_stage` | 当前流程阶段；无工作流时为 `idle` |
| `loaded_skills` | 当前 Turn 已成功加载的 Skill 名称集合 |

这些状态属于 Turn，而不是整个 Session。`begin_new_turn()` 必须清空它们，避免上一轮的 Skill 和流程隐式授权下一轮。

### 7.2 固定阶段

```text
idle
inspect
plan
implement
verify
finish
```

不增加自定义阶段或任意字符串扩展，以保持状态迁移可测试。

### 7.3 阶段含义

| 阶段 | 含义 | 允许行为 |
| --- | --- | --- |
| `idle` | 尚未选择开发工作流 | 对话、只读工具、加载 Skill |
| `inspect` | 阅读需求、规则、代码和测试 | 只读工具；Bug 可运行复现命令 |
| `plan` | 建立或调整多步骤计划 | 只读工具、`update_plan` |
| `implement` | 执行当前计划或最小修复 | 受权限和工作流 Hook 约束的写操作与命令 |
| `verify` | 修改完成，正在获取有效验证证据 | 验证命令、只读检查、必要修复 |
| `finish` | 计划完成且证据满足门禁 | 输出最终总结，不再继续修改 |

## 8. 阶段转换规则

### 8.1 状态转换图

```text
idle
  └─ 成功加载具体工作流 ─→ inspect

inspect
  ├─ add-feature 创建计划 ─→ plan
  ├─ bug-fix 开始最小修改 ─→ implement
  └─ code-review 获得足够审查证据 ─→ verify

plan
  └─ 存在一个 in_progress 步骤 ─→ implement

implement
  ├─ 仍有未完成步骤 ─→ implement
  ├─ 修改后尚无新鲜验证 ─→ verify
  └─ 验证失败并继续修复 ─→ implement

verify
  ├─ 验证失败且仍有修复次数 ─→ implement
  ├─ 验证被拒绝 ─→ verify
  └─ 计划完成且验证新鲜 ─→ finish

finish
  └─ Turn 结束
```

### 8.2 事实驱动原则

阶段不能只由模型文本声明。转换应基于现有事件事实：

- 成功的 `load_skill` 结果；
- `plan_updated` 事件；
- 成功写工具返回的 `mutated_files`；
- `verification_recorded` 事件中的 `accepted`；
- 当前计划是否仍包含 `pending` 或 `in_progress`；
- `changed_files` 与验证序列的新鲜度关系。

### 8.3 具体工作流规则

#### Add Feature

- 加载 `add-feature` 后进入 `inspect`；
- 没有非空计划时禁止首次写操作；
- 计划存在一个 `in_progress` 步骤时进入 `implement`；
- 全部步骤完成但验证不新鲜时进入 `verify`；
- 全部步骤完成且验证新鲜时进入 `finish`。

#### Bug Fix

- 加载 `bug-fix` 后进入 `inspect`；
- 不强制计划，但仍受读取后修改和验证门禁约束；
- 首次成功修改进入 `implement`；
- 修改后模型停止调用工具时，若验证不新鲜则进入 `verify`；
- 验证成功后进入 `finish`。

#### Code Review

- 加载 `code-review` 后进入 `inspect`；
- 任何写工具调用均被拒绝；
- 读取 Diff 和必要上下文后进入 `verify`；
- 审查可以在无文件修改的情况下进入 `finish`，不要求代码修改型的新鲜验证；
- 最终结果必须是发现列表或明确的“未发现阻塞问题”，不能声称修改完成。

## 9. Plan 数据契约

### 9.1 步骤结构

在现有 `step` 和 `status` 基础上增加可选 `acceptance`：

```json
{
  "step": "为 add 函数增加负数回归测试",
  "status": "in_progress",
  "acceptance": "tests/test_calculator.py 中相关用例通过"
}
```

### 9.2 校验规则

- `steps` 保持 1～12 项；
- `step` 保持 1～240 个字符；
- `acceptance` 可省略，填写时为 1～300 个字符；
- `status` 仍只允许 `pending`、`in_progress`、`completed`；
- 同一计划最多一个 `in_progress`；
- `add-feature` 至少一个步骤必须包含非空 `acceptance`；
- `bug-fix` 使用计划时建议包含验收条件，但不作为硬门禁；
- 计划验收文本只用于上下文和报告，不由系统尝试执行任意自然语言命令。

### 9.3 不引入的字段

V1 不增加：

- `depends_on`；
- `assignee`；
- `subtasks`；
- `parallelizable`；
- `reviewer`；
- `estimated_tokens`；
- 任意脚本或命令形式的验收字段。

## 10. 事件协议

### 10.1 新事件

#### `skill_loaded`

```json
{
  "type": "skill_loaded",
  "payload": {
    "name": "bug-fix",
    "description": "Diagnose and safely fix a reported defect."
  }
}
```

只在 `load_skill` 返回成功时记录。加载失败不能进入 `loaded_skills`。

#### `workflow_selected`

```json
{
  "type": "workflow_selected",
  "payload": {
    "name": "bug-fix",
    "source": "skill_loaded",
    "stage": "inspect"
  }
}
```

具体工作流首次成功加载时记录。`development-workflow` 是路由 Skill，不作为最终 `workflow_name`。

#### `workflow_stage_changed`

```json
{
  "type": "workflow_stage_changed",
  "payload": {
    "from": "implement",
    "to": "verify",
    "reason": "files changed and no fresh verification exists"
  }
}
```

只有实际阶段变化时记录，避免重复事件污染日志。

### 10.2 归约规则

`SessionReducer` 必须处理新事件：

- `skill_loaded`：向 `loaded_skills` 添加名称；
- `workflow_selected`：设置 `workflow_name` 和初始阶段；
- `workflow_stage_changed`：设置新阶段；
- `turn_started`：清空工作流状态；
- `restore_from_events`：按事件顺序恢复，不执行任何工具。

旧事件没有这些类型时：

- `workflow_name = None`；
- `workflow_stage = "idle"`；
- `loaded_skills = set()`；
- 继续使用当前完成判断，不因升级而使旧 Session 无法恢复。

## 11. 上下文注入

`ContextManager` 在存在具体工作流时追加一个短且锁定的上下文块：

```text
Current development workflow:
- name: add-feature
- stage: implement
- loaded skills: development-workflow, add-feature
- constraints:
  - keep exactly one plan step in progress
  - complete acceptance criteria before marking the step complete
  - obtain fresh verification after the latest mutation
```

注入内容必须满足：

- 来自 `AgentState`，而不是重新从模型回复推测；
- 不重复注入完整 `SKILL.md`；
- 不超过固定字符预算；
- 在 `source_manifest` 中以 `workflow_state` 单独记录；
- 属于核心流程状态，历史压缩时不能被丢弃；
- 不覆盖 `AGENTS.md`、权限和用户直接指令。

## 12. 工作流守卫 Hook

### 12.1 复用方式

在 `runtime.py` 中以闭包方式创建状态感知的 PreTool Hook，并注册到现有 `HookManager`。该 Hook 只做流程约束，不重复实现权限策略。

权限判定仍由：

```text
Mode / Profile 工具集合
  → PermissionPolicy
  → 工作流 PreTool Hook
  → ToolExecutor
```

工作流 Hook 不能把 `DENY` 或 `ASK` 变成 `ALLOW`。

### 12.2 最小门禁

| 条件 | 行为 |
| --- | --- |
| 当前为 `add-feature` 且计划为空，调用写工具 | 拒绝，提示先创建计划 |
| 当前为 `add-feature` 且没有 `in_progress` 步骤，调用写工具 | 拒绝，提示选择当前步骤 |
| 当前为 `code-review`，调用写工具 | 拒绝，提示开始新的修复 Turn |
| 当前阶段为 `finish`，调用写工具 | 拒绝，防止完成后继续修改 |
| 没有选择工作流但调用写工具 | V1 不硬拒绝；记录诊断并保留旧行为 |

最后一项选择兼容优先：现有用户可能不需要工作流即可完成简单编辑。是否将 Skill 选择升级为所有写任务的强制门禁，应由后续 Eval 证明收益后再决定。

### 12.3 不在 Hook 中实现的逻辑

- 不执行测试；
- 不自动修改计划；
- 不自动加载 Skill；
- 不决定命令是否危险；
- 不创建检查点；
- 不判断验证证据是否真实；
- 不改变审批结果。

这些职责继续由现有组件承担。

## 13. 完成门禁

### 13.1 现有门禁继续保留

`Verifier` 继续检查：

- 计划中是否存在未完成步骤；
- 是否仍有待审批操作；
- 文件修改后是否存在更新的有效验证证据；
- 修复尝试是否达到上限。

### 13.2 新增工作流规则

#### 通用规则

- 有具体工作流时，只有 `finish` 阶段可以 `COMPLETED`；
- 阶段未满足时返回 `CONTINUE`，并通过现有 `verification_required` 事件给模型确定性原因；
- 达到现有完成拒绝上限时返回 `PARTIAL`，不无限循环。

#### Add Feature

完成需要同时满足：

- `add-feature` 已加载；
- 计划非空；
- 至少一个步骤包含 `acceptance`；
- 所有步骤为 `completed`；
- 存在变更文件；
- 最新修改之后存在被接受的验证证据；
- 阶段可转换为 `finish`。

#### Bug Fix

完成需要同时满足：

- `bug-fix` 已加载；
- 存在变更文件；
- 最新修改之后存在被接受的验证证据；
- 如创建了计划，则计划全部完成；
- 阶段可转换为 `finish`。

回归测试优先由 Skill 约束。V1 不尝试静态判断测试文件一定被修改，因为某些仓库已有失败测试，强制修改测试文件会造成误判。

#### Code Review

完成需要同时满足：

- `code-review` 已加载；
- 当前 Turn 没有修改文件；
- 没有待审批操作；
- 计划若存在则已完成；
- 阶段可转换为 `finish`。

代码审查不要求代码修改型的新鲜验证，但最终提示必须要求模型提供证据化发现。

## 14. 三类任务的标准流程

### 14.1 新功能

```text
加载 development-workflow
  → 加载 add-feature
  → 阅读 README / AGENTS.md / Repo Map
  → 阅读现有接口、调用方和测试
  → update_plan（含 acceptance）
  → 将一个步骤标记为 in_progress
  → 小范围修改
  → 更新测试
  → 运行针对性测试
  → 必要时运行更广验证
  → 完成所有计划步骤
  → 查看 Diff
  → 输出变更、证据、限制
```

### 14.2 Bug 修复

```text
加载 development-workflow
  → 加载 bug-fix
  → 读取错误和相关路径
  → 运行最小复现
  → 形成根因假设
  → 增加/确认回归测试
  → 最小修复
  → 运行回归测试
  → 运行受影响范围验证
  → 查看 Diff
  → 输出根因、修复、证据、风险
```

### 14.3 代码审查

```text
加载 development-workflow
  → 加载 code-review
  → 读取 Diff
  → 阅读修改点周边代码
  → 阅读相关测试和规则
  → 必要时运行只读或验证命令
  → 按严重程度整理发现
  → 输出文件、行号、影响和建议
```

## 15. 失败、恢复与降级

### 15.1 Skill 加载失败

- `load_skill` 返回 `SKILL_NOT_FOUND`；
- 不记录 `skill_loaded`；
- 不选择工作流；
- 模型可调用 `list_skills` 后重试；
- 不能因为名称猜测进入工作流状态。

### 15.2 计划不合法

- 保持现有 `INVALID_ARGUMENTS`；
- 多个 `in_progress`、过长文本或非法状态均拒绝；
- 原计划不被部分覆盖；
- 模型根据错误重新提交完整计划。

### 15.3 工作流 Hook 拒绝

- 返回结构化 `HOOK_DENIED`；
- 不执行底层工具；
- 事件轨迹保留 Hook 名称、原因和阶段；
- 模型应通过加载 Skill、更新计划或开始新 Turn 解决，而不是重复调用。

### 15.4 验证失败

- 沿用 `VerificationEvidence` 的分类和接受规则；
- 阶段从 `verify` 回到 `implement`；
- 沿用 `repair_attempts` 和 `max_repair_attempts`；
- 达到上限后以 `PARTIAL` 结束，并报告失败证据。

### 15.5 进程中断与恢复

- 新工作流事件由 `SessionReducer` 重放；
- 未完成工具仍按 `INTERRUPTED_UNKNOWN` 处理；
- 不因工作流恢复而自动重放写操作或命令；
- 待审批恢复继续使用现有 `resume_approval`；
- 恢复后重新计算阶段时必须以持久事件和验证证据为准。

### 15.6 老 Session 降级

没有工作流事件的旧 Session：

- 不补造 Skill 已加载事实；
- 不强制进入任何工作流；
- 保持当前 Verifier 行为；
- 在新 Turn 中可正常选择工作流。

## 16. UI 与可观察性

V1 只做最小展示，不重新设计 Web UI。

建议在现有计划面板顶部增加：

```text
开发流程：Bug Fix
当前阶段：验证
已加载：development-workflow, bug-fix
```

展示数据来自 Session API 或事件投影，不由前端自行推断。

计划步骤增加一行可选验收条件：

```text
[进行中] 为 add 函数增加负数测试
验收：tests/test_calculator.py 中相关用例通过
```

轨迹面板显示：

- Skill 加载；
- 工作流选择；
- 阶段转换；
- 工作流 Hook 拒绝；
- 完成门禁拒绝原因。

普通模式可以只显示流程名称和当前阶段；专业模式显示事件原因和完整验证证据。

## 17. 测试策略

### 17.1 单元测试

#### SkillLibrary 与 Skill Tools

- 新总入口和三个具体 Skill 都能列出、加载；
- 目录穿越和禁用 Skill 仍被拒绝；
- 只有成功 `load_skill` 才产生 Skill 事实；
- `development-workflow` 不会被当作最终具体工作流。

#### Plan Tool

- 接受合法 `acceptance`；
- 拒绝空或超过 300 字符的 `acceptance`；
- 保持最多一个 `in_progress`；
- 非法更新失败后原计划保持不变；
- 不含 `acceptance` 的旧调用仍兼容。

#### SessionReducer

- 在线事件和离线恢复得到相同工作流状态；
- 重复 `skill_loaded` 事件保持集合幂等；
- 新 Turn 清空上轮工作流；
- 未知工作流或非法阶段不污染状态；
- 老事件恢复得到 `idle` 默认值。

#### ContextManager

- 工作流上下文正确注入；
- 无工作流时不增加额外内容；
- `source_manifest` 包含 `workflow_state`；
- 历史压缩后仍保留当前工作流状态；
- Skill 全文不会被重复注入。

#### Workflow Hook

- `add-feature` 无计划时拒绝写；
- `add-feature` 无 `in_progress` 步骤时拒绝写；
- `code-review` 拒绝写；
- `bug-fix` 允许经过原权限策略的最小写操作；
- `finish` 阶段拒绝继续写；
- Hook 不能绕过原有权限拒绝和审批。

#### Verifier

- 新功能计划未完成时继续执行；
- 新功能没有验收条件时继续执行；
- 修改后验证过期时继续执行；
- Bug 修复有新鲜验证时可以完成；
- Code Review 无变更时可以完成；
- Code Review 出现变更时不能完成；
- 多次拒绝后按现有上限进入 `PARTIAL`。

### 17.2 Runtime 集成测试

使用 `ScriptedModel` 覆盖完整事件链：

1. **Add Feature 正常路径**：加载 Skill → 创建计划 → 修改 → 验证 → 完成。
2. **Add Feature 门禁路径**：未创建计划就写入 → Hook 拒绝 → 创建计划 → 继续成功。
3. **Bug Fix 正常路径**：复现 → 修复 → 回归验证 → 完成。
4. **Bug Fix 验证失败**：首次测试失败 → 修复 → 第二次验证成功。
5. **Code Review 只读路径**：读取 Diff → 输出发现 → 无变更完成。
6. **Code Review 越界路径**：尝试写文件 → Hook 拒绝 → 保持只读完成。
7. **恢复路径**：在计划中断后重建 Runtime → 工作流和阶段一致 → 不重复副作用。
8. **兼容路径**：旧 Session 无工作流事件 → 按旧逻辑恢复。

### 17.3 Eval 扩展

在现有固定任务基础上增加工作流契约指标：

- 适用任务的 Skill 加载率；
- 新功能任务的计划建立率；
- 计划完成率；
- 修改后新鲜验证率；
- Review 任务无写入率；
- 中断恢复后的阶段一致率；
- 工作流带来的平均 Step 和 Token 变化。

确定性门禁建议：

- 三种工作流契约通过率 100%；
- Code Review 写入次数为 0；
- 修改任务新鲜验证率 100%；
- 恢复后重复副作用次数为 0；
- 旧任务完成率不低于当前批准基线。

真实模型 Eval 仍显式付费运行，不作为唯一发布门禁。至少比较启用和禁用 Superpowers Lite 时的完成率、验证率、无关修改数、Step 和 Token，确认流程约束没有导致明显退化。

## 18. 分阶段实施顺序

每一阶段必须单独通过相关测试后再进入下一阶段。

### 阶段 A：Skill 内容和 Plan 契约

改动范围：

- 新增 `skills/development-workflow/SKILL.md`；
- 扩充三个现有 Skill；
- 为 `update_plan` 增加可选 `acceptance`；
- 增加 Skill 和 Plan 单元测试。

完成标准：

- 所有 Skill 可被安全列出和按需加载；
- 新旧计划参数均兼容；
- 不改变 Agent Loop 行为。

### 阶段 B：工作流状态与事件恢复

改动范围：

- 扩展 `AgentState`；
- 增加三个事件；
- 扩展 `SessionReducer`；
- 在成功加载具体 Skill 时选择工作流；
- 注入工作流上下文。

完成标准：

- 在线状态和事件恢复完全一致；
- 新 Turn 不继承旧工作流；
- 老 Session 无回归；
- UI/API 暂时不展示也不影响核心行为。

### 阶段 C：守卫与完成门禁

改动范围：

- 注册状态感知的工作流 PreTool Hook；
- 扩展 `Verifier`；
- 根据计划、变更和验证事件推进阶段；
- 增加 Runtime 集成测试。

完成标准：

- Add Feature 无计划不能写；
- Code Review 不能写；
- Bug Fix 保持轻量流程；
- 只有满足对应工作流完成条件时才能 `COMPLETED`；
- 失败循环仍受现有上限控制。

### 阶段 D：最小 UI 与 Eval

改动范围：

- Session API 输出工作流名称、阶段和已加载 Skill；
- 计划面板展示流程与验收条件；
- 轨迹面板展示新事件；
- 增加工作流确定性 Eval 和基线报告。

完成标准：

- UI 与后端状态一致；
- 刷新和会话恢复后阶段显示正确；
- 工作流契约门禁全部通过；
- 现有质量基线无退化。

## 19. 建议的文件改动边界

后续实施预计只需要修改或新增以下文件：

```text
skills/development-workflow/SKILL.md
skills/add-feature/SKILL.md
skills/bug-fix/SKILL.md
skills/code-review/SKILL.md

src/coding_agent/session.py
src/coding_agent/session_reducer.py
src/coding_agent/context.py
src/coding_agent/agent_loop.py
src/coding_agent/runtime.py
src/coding_agent/verifier.py
src/coding_agent/tools/plan.py

src/coding_agent/web/app.py
src/coding_agent/web/static/app.js
src/coding_agent/web/static/index.html

tests/test_skills.py
tests/test_agent_loop.py
tests/test_context.py
tests/test_web_app.py
evals/scenarios.py
evals/tasks/12_add_feature_workflow.json
evals/tasks/13_bug_fix_workflow.json
evals/tasks/14_code_review_workflow.json
docs/evals.md
```

如果实施中需要新增大量核心模块、数据库表或新的执行循环，应停止并重新评估范围，因为这说明方案正在偏离“轻量扩展”的约束。

## 20. 兼容性与迁移

### 20.1 配置兼容

- `enabled_skills` 继续生效；
- 禁用某个具体工作流 Skill 时不能选择对应工作流；
- 不增加必须配置的环境变量；
- 默认行为继续支持没有主动加载 Skill 的简单任务。

### 20.2 事件兼容

- 新事件使用当前事件 schema 机制；
- 旧事件无需离线重写；
- 新 Reducer 必须忽略不认识但处于兼容范围内的非关键事件；
- 工作流状态只由当前 Turn 的事件恢复。

### 20.3 API 兼容

Session 响应只增加可选字段：

```json
{
  "workflow": {
    "name": "bug-fix",
    "stage": "verify",
    "loaded_skills": ["development-workflow", "bug-fix"]
  }
}
```

旧前端忽略该字段即可继续工作。

## 21. 安全不变量

实现 Superpowers Lite 后，以下不变量必须继续成立：

1. Skill 不能直接访问文件系统或命令执行器；
2. 所有机器操作仍通过工具协议；
3. 所有写操作仍受工作区边界、读取哈希和检查点保护；
4. 工作流 Hook 只能拒绝或补充上下文，不能扩大权限；
5. `code-review` 不能修改文件；
6. 中断恢复不能自动重放未知副作用；
7. 没有新鲜验证证据时，修改任务不能成功完成；
8. 验收条件文本不能被当作未经批准的 Shell 命令执行；
9. Skill、计划和工作流事件中的敏感内容继续经过现有脱敏管线；
10. 工作流失败不能导致无限模型循环。

## 22. 工程完成定义

Superpowers Lite V1 只有满足以下条件才视为完成：

- [x] 四个项目 Skill 均有明确触发条件、流程、完成证据和禁止事项；
- [x] Add Feature、Bug Fix、Code Review 三种工作流可被选择和恢复；
- [x] `acceptance` 计划字段兼容旧调用并有严格校验；
- [x] 工作流状态完全由事件恢复，在线和离线投影一致；
- [x] Add Feature 无计划写入被阻止；
- [x] Code Review 写操作被阻止；
- [x] 修改任务只有在验证新鲜时才能完成；
- [x] Code Review 可以在零修改情况下完成；
- [x] 中断、审批、取消、预算、检查点和回滚行为无回归；
- [x] 旧 Session 和未启用工作流的任务保持兼容；
- [x] 工作流状态在 Web UI 刷新后展示一致；
- [x] 单元、集成、恢复和确定性 Eval 全部通过；
- [x] 文档记录已知限制和真实模型对照结果；
- [x] 没有引入多 Agent、任务 DAG 或第二套 Agent Loop。

> 2026-09-01 审计证据：确定性质量流水线见 `test-results/quality-run-superpowers-lite-final8/summary.md`（16 个检查、397 个 Pytest 用例全部通过）。在获得明确授权后，使用 `python -m evals.superpowers_comparison --mode real --allow-paid --repetitions 3` 完成真实 DeepSeek 对照，报告见 `test-results/superpowers-comparison-real/superpowers-comparison.md`。三次成对运行的聚合结果为：启用工作流 completion 22.22%、verification 0%，平均 4.553 Step、20,453 Token、6.33 次工具调用；禁用工作流 completion 44.45%、verification 33.33%，平均 4.443 Step、17,983 Token、5.55 次工具调用；两组无关修改均为 0。该结果证明真实模型对照链路可运行，但没有证明当前工作流提示在这组小样本上带来质量或速度提升；结果会受模型随机性、供应商限流和任务夹具影响，不应外推为普遍结论。后续应针对失败任务优化工作流提示、预算与验证闭环，再重复同一实验。

## 23. 后续实施时的统一迭代模板

每次只实施一个阶段，并使用以下模板记录结果：

1. **目标与不变量**：本轮增加什么，哪些现有行为不能改变；
2. **失败测试或基线**：改动前如何证明能力尚不存在；
3. **最小实现**：只修改形成当前纵向切片所需的文件；
4. **针对性验证**：运行受影响测试；
5. **完整验证**：运行全量测试和确定性 Eval；
6. **事件证据**：确认新增事件、恢复投影和终态正确；
7. **指标对比**：记录完成率、验证率、Step、Token 或无关修改数；
8. **已知限制**：明确未实现部分和降级行为；
9. **提交边界**：一个阶段一个逻辑提交，不混入无关 UI 或重构。

## 24. 最终建议

该设计应作为 Code Helper 的流程增强，而不是架构重写。最优实施路线是先增强 Skill 文本和 Plan 契约，再让事件系统记录工作流事实，最后增加少量 Hook 与 Verifier 门禁。这样可以利用项目最成熟的事件、权限、恢复和验证基础，获得接近 Superpowers 的工程纪律，同时保持当前单 Agent 架构的可解释性和实现规模。

若后续 Eval 证明某一门禁明显增加 Step 或阻碍简单任务，应优先调整具体规则，而不是引入通用编排框架。只有真实任务证据表明单 Agent 无法满足需求时，才重新评估多 Agent；该能力不属于本设计范围。
