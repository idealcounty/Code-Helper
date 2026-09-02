# Code Helper 差异化能力工作台开发方案

> 文档状态：设计方案，暂不进入代码实施  
> 目标版本：Differentiation Workbench V1  
> 核心定位：面向算法与教学场景的可观测、可验证、本地 Coding Agent 工作台

## 1. 文档目的

Code Helper 已经具备自研 Agent Loop、Tools、Repo Map、上下文压缩、事件日志、审批、验证 Hooks 和长期记忆等基础能力，但这些能力目前主要散落在“轨迹”和“智能”面板中。用户能够看到很多数字，却不容易立即理解这些能力能解决什么问题，也很难直接操作、对比和复用这些数据。

本方案不继续增加普通 Coding Agent 的通用功能，而是将现有基础组织成四个清晰、可操作、可演示的差异化工作台：

1. **Algorithm Reliability Lab**：算法可靠性实验室。
2. **Agent Time-Travel Debugger**：Agent 运行回放与调试器。
3. **Context Compiler**：可解释、可调节的上下文编译器。
4. **Human-governed Agent Memory**：用户治理的长期记忆中心。

最终目标不是做四个孤立页面，而是形成一个共同闭环：

```text
上下文编译
    ↓
Agent 执行与事件记录
    ↓
算法/项目验证
    ↓
运行回放与错误定位
    ↓
用户确认可长期保留的知识
```

## 2. 产品决策

### 2.1 一句话定位

> Code Helper 是一个能够解释“为什么这样执行”、证明“结果是否可靠”、并允许用户治理“以后记住什么”的本地 Coding Agent。

### 2.2 本阶段不追求的目标

- 不与 Codex、Claude Code 比拼通用模型能力。
- 不优先实现多 Agent、云端 Worktree、插件市场等大型生态能力。
- 不把更多统计数据继续堆入现有“智能”长页面。
- 不声称可以从任意自然语言算法题中自动推导绝对正确的 Oracle。
- 不持久化或展示供应商私有思维链。
- 不为了页面图表引入大型前端框架或重写现有 Web 架构。

### 2.3 设计原则

1. **证据优先**：正确性、复杂度、召回和记忆都必须展示证据来源。
2. **过程可回放**：重要结论能够追溯到某个 Step、Tool Result 或测试用例。
3. **用户可控制**：可调项必须明确，安全规则和核心系统上下文不可被误关闭。
4. **可信边界明确**：估算值、模型生成值和确定性执行结果必须使用不同标识。
5. **渐进展示**：首屏突出结论，细节通过检查器展开，避免信息噪声。
6. **复用现有事件**：优先从事件日志和现有报告派生页面，不重复维护状态。
7. **本地优先**：报告、回放和记忆继续保存在工作区 `.code-helper/` 中。

## 3. 当前能力与缺口

| 方向 | 已有能力 | 主要缺口 |
| --- | --- | --- |
| 算法可靠性 | `analyze_complexity`、`judge_algorithm`、固定 seed、失败分类、最小输入缩减、算法 Profile | 缺少题目结构化解析、自动用例生成、Oracle 对拍、耗时分布、持久报告和专用页面 |
| 运行回放 | JSONL 事件日志、Step、Tool Calls、Tool Result、Span、上下文元数据、会话恢复 | 缺少按 Step 聚合、上下文快照、对比、错误标记和安全分支语义 |
| 上下文编译 | 80,000 字符预算、Token 估算、Repo Map、规则、摘要、记忆召回、来源展示 | 缺少分来源 Token、可调来源、What-if 对比、重复检测和质量评分 |
| 记忆治理 | 候选确认/拒绝、项目/用户记忆分层、词法/可选向量召回、冲突和仓库证据 | 缺少候选聚类去重、过期、固定、归档、召回解释、冲突对比和批量治理 |

### 3.1 必须修正的现有展示口径

在扩展页面之前，需统一以下术语：

- “原始会话字符”与“实际发送上下文字符”分开显示。
- “当前 Step Token”“当前 Turn 累计 Token”“Session 累计 Token”分开显示。
- “压缩次数”说明是累计事件数，不表示摘要嵌套层数。
- “Repo Map 自动构建次数”与显式 `get_repo_map` Tool 调用次数分开。
- “工具成功率”不得等同于“任务正确率”。

这些口径将由四个工作台共用，避免同一数据在不同页面含义不同。

## 4. 总体信息架构

### 4.1 页面入口

不建议把四个页面继续放进右侧狭窄的 Agent Tabs。建议在左侧辅助导航中增加“研究工作台”入口，并让主编辑区切换为全宽分析页面。

```text
左侧导航
├─ 浏览文件
└─ 研究工作台
   ├─ 算法实验室
   ├─ 运行回放
   ├─ 上下文编译器
   └─ 记忆治理
```

现有右侧“智能”页调整为紧凑总览，只展示：

- 当前任务健康状态。
- 最近算法报告结论。
- 当前上下文使用率。
- 待处理记忆候选数。
- 最近一次失败 Step。
- “在研究工作台中打开”按钮。

### 4.2 工作区布局

采用“密集分析工作台”而不是卡片墙：

```text
┌──────────────────────────────────────────────────────────────┐
│ 工作台导航  算法实验室｜运行回放｜上下文｜记忆              │
├───────────────┬─────────────────────────────┬────────────────┤
│ 运行/筛选列表  │ 主报告、时间线或图表          │ 证据检查器      │
│               │                             │                │
│               │                             │                │
└───────────────┴─────────────────────────────┴────────────────┘
```

- 左列：运行、Step、上下文来源或记忆列表。
- 中列：主要图表、时间线、正确性矩阵或冲突对比。
- 右列：当前选中对象的证据和操作。
- 原有 Agent 对话栏保持可见；空间不足时可折叠为抽屉。

### 4.3 视觉方向

延续现有银白科技感，采用“冷静的工程分析仪器”风格：

- 青蓝：选中、运行中、信息来源。
- 绿色：确定性通过、Fresh Evidence。
- 琥珀色：估算、待确认、可能过期。
- 红色：失败、冲突、预算超限。
- 紫灰色：模型生成、尚未经过确定性验证的内容。
- 等宽字体只用于路径、Token、复杂度和事件字段。
- 图表不使用大面积渐变，优先使用清晰坐标、阈值线和证据标记。

## 5. 公共后端基础

四个页面应共享以下基础模型，避免各自重新解析事件。

### 5.1 统一运行标识

```text
workspace_id
session_id
turn_id
step
event_sequence
run_id
created_at
```

所有报告必须能够追溯到会话、轮次和事件序号。

### 5.2 统一证据等级

```text
deterministic   确定性工具或测试结果
observed        来自当前仓库和事件日志的观测
estimated       静态分析或 Token 估算
model_generated 模型提出但尚未验证
user_confirmed  用户明确确认
stale           来源发生变化或证据过期
```

前端任何“正确”“复杂度”“记忆事实”都必须显示证据等级。

### 5.3 报告存储

建议新增：

```text
.code-helper/
├─ algorithm-runs/<run_id>.json
├─ replay/<session_id>/<turn_id>-<step>.json.gz
├─ context-builds/<session_id>/<turn_id>-<step>.json
├─ context-profiles.json
└─ memory/
```

存储规则：

- 采用版本化 JSON Schema。
- 使用临时文件写入后原子替换。
- 受现有存储容量和文件数量上限控制。
- 不保存 API Key、环境变量、敏感文件正文和私有思维链。
- 报告删除不影响原始业务文件。

## 6. 第一优先级：算法可靠性实验室

### 6.1 目标

将现有“模型写代码，然后运行几个样例”升级为可重复的算法验证实验：

```text
解析题目
→ 建立约束模型
→ 生成样例/边界/随机用例
→ 使用可信 Oracle 计算期望输出
→ 运行候选程序
→ 最小化失败输入
→ 采集复杂度与耗时曲线
→ 生成正确性报告
```

### 6.2 页面首屏

首屏必须直接回答五个问题：

```text
理论复杂度     O(n log n)       estimated
测试用例       128              deterministic
通过 / 失败    127 / 1
最小反例       [0, 1, 0]
运行耗时 P95   14 ms
```

主区域分为：

1. **报告摘要条**：结论、可信等级、运行时间和 seed。
2. **正确性矩阵**：样例、边界、随机、回归、Oracle 对拍五类用例。
3. **复杂度曲线**：输入规模与 P50/P95 耗时。
4. **失败检查器**：输入、期望、实际、stderr、失败类型、最小反例。
5. **题目约束面板**：变量、范围、总和约束、多测限制和来源文本。

### 6.3 题目约束解析

新增 `ProblemSpec`：

```json
{
  "title": "Example Problem",
  "test_cases": {"min": 1, "max": 30000},
  "variables": [
    {"name": "n", "type": "integer", "min": 1, "max": 200000}
  ],
  "aggregate_constraints": ["sum(n) <= 200000"],
  "input_shape": [],
  "output_shape": [],
  "samples": [],
  "confidence": 0.86,
  "warnings": []
}
```

解析分两层：

- 确定性正则提取常见区间、总和约束和样例块。
- 模型补充结构关系，但必须标记为 `model_generated`，由用户在页面确认后才能用于批量生成测试。

不能把自然语言解析结果直接当作绝对正确事实。页面需提供“原文定位”和“确认约束”操作。

### 6.4 自动生成边界测试

基于确认后的 `ProblemSpec` 生成：

- 最小值、最大值。
- 空集、单元素、全相等、严格递增、严格递减。
- 0、1、负数、溢出边界（题目允许时）。
- 重复值、极端不平衡分布。
- 多测试总和恰好达到上限。
- 根据数据结构选择链、星、完全图、退化树等模板。

每个用例包含来源：

```text
sample / boundary / random / regression / user / metamorphic
```

V1 不追求覆盖所有题型，优先实现数组、字符串、单值查询和多测试输入四类模板。无法结构化生成时，明确退化为用户提供用例。

### 6.5 随机对拍与 Oracle

这一阶段需要明确实现“**暴力解法作为 Oracle**”的受控工作流，而不是让模型直接猜测随机用例的标准答案。

随机测试必须固定 seed，可完整复现。Oracle 支持三种等级：

1. **用户提供的暴力程序**：最高可信，受命令审批和超时限制。
2. **Agent 生成的暴力程序**：先展示代码，经用户批准和基础自测后使用。
3. **变形测试**：没有标准答案时验证排序不变性、单调性或其他关系。

禁止直接把模型生成的一段文本当作期望输出。Oracle 仅在小规模输入上运行，并配置：

- 最大规模。
- 单例超时。
- 总运行时间。
- 最大输出。
- 可取消状态。
- 去除敏感环境变量。

### 6.6 Judge 扩展

现有 `judge_algorithm` 最大 100 个显式用例，而目标页面示例需要 128 个。V1 应将接口扩展为批次运行，避免单次 Tool 参数过大：

```text
create_algorithm_run
generate_algorithm_cases
run_algorithm_batch
finalize_algorithm_report
```

每个 Case Result 增加：

```text
duration_ms
input_size
case_source
oracle_source
expected_hash
actual_hash
exit_code
memory_peak（可用时）
```

失败分类：

- Wrong Answer。
- Runtime Error。
- Time Limit Exceeded。
- Output Limit Exceeded。
- Oracle Error。
- Candidate Compile Error。
- Cancelled。

### 6.7 最小失败反例

保留现有删除输入行和整数折半逻辑，新增结构化 Shrinker：

- 数组：删除区间、删除元素、数值向 0/边界收缩。
- 字符串：删除子串、缩小字符集。
- 图：删除边、删除孤立点、压缩编号。
- 多测试：先删除测试组，再缩小失败组。

每次缩减都必须重新运行 Candidate 与 Oracle，确保失败性质仍然存在。页面显示缩减轨迹：

```text
原输入 1,284 bytes
→ 412 bytes
→ 83 bytes
→ 9 bytes
最终反例 [0, 1, 0]
```

### 6.8 复杂度实测曲线

新增 Benchmark Runner：

- 输入规模：例如 10、100、1,000、10,000。
- 每个规模预热 1 次，正式重复 5～10 次。
- 展示 P50、P95、最大值。
- 使用同一 seed 生成输入。
- 单独标记系统噪声和超时点。
- 静态复杂度与实测拟合并列展示，不宣称实测曲线能够证明 Big-O。

前端使用原生 SVG 绘制折线，支持悬停查看规模、耗时和样本数。

### 6.9 算法报告

报告必须包含：

- 源文件和源码 SHA-256。
- 编译命令与候选运行命令。
- 题目约束版本。
- Oracle 类型与源码哈希。
- seed、用例数量和分类。
- 通过、失败和首个失败。
- 最小反例。
- 静态复杂度估算及警告。
- Benchmark P50/P95。
- 生成时间、会话、Turn、Step 和事件序列。
- 正确性结论的证据等级。

支持导出 JSON 和 Markdown。报告结论建议使用：

```text
VERIFIED_FOR_CASES     所有已执行用例通过
FAILURE_FOUND          已找到稳定失败证据
INCONCLUSIVE           Oracle 或运行条件不足
```

不要使用“算法绝对正确”作为结论。

### 6.10 API 草案

```text
POST /api/sessions/{id}/algorithm-lab/spec/parse
POST /api/sessions/{id}/algorithm-lab/runs
GET  /api/sessions/{id}/algorithm-lab/runs
GET  /api/sessions/{id}/algorithm-lab/runs/{run_id}
POST /api/sessions/{id}/algorithm-lab/runs/{run_id}/cancel
GET  /api/sessions/{id}/algorithm-lab/runs/{run_id}/report.md
```

算法运行继续走现有 PermissionPolicy、CancellationToken 和命令安全边界。

## 7. 第二优先级：Agent Time-Travel Debugger

### 7.1 目标

把 JSONL 日志从“恢复数据”提升为“可调试执行记录”，回答：

- 这个 Step 模型看到了什么？
- 为什么选择了这个 Tool？
- Tool 返回了什么？
- 第一次失败从哪里开始？
- Fast 与 Deep 的执行路径有什么不同？
- 能否从历史节点开始一个安全的新尝试？

### 7.2 Step 聚合模型

新增 `StepFrame`，按 `turn_id + step` 聚合事件：

```json
{
  "step": 3,
  "started_sequence": 120,
  "finished_sequence": 148,
  "context_build": {},
  "model_request": {},
  "assistant_response": {},
  "tool_calls": [],
  "tool_results": [],
  "verification": [],
  "errors": [],
  "duration_ms": 7421
}
```

StepFrame 是事件日志的派生视图，原始事件仍是权威数据。

### 7.3 页面结构

- 左侧：Turn 和 Step 时间线。
- 中间：执行流图，展示 Context → Model → Tools → Results → Verification。
- 右侧：选中节点的结构化检查器。
- 顶部：播放、上一步、下一步、跳到首个错误、播放速度。

时间线状态颜色：

- 青蓝：正常决策。
- 绿色：验证通过。
- 琥珀：审批等待、压缩、重试。
- 红色：工具失败、模型协议错误、预算耗尽、验证拒绝。
- 灰色：取消或未执行。

### 7.4 查看 Step 实际上下文

当前 `context_built` 只保存总量和来源元数据，无法完整还原当时发送给模型的消息。要实现真正回放，需要新增脱敏快照：

```text
system segments
retained messages
history summary
tool schemas
repo map selection
memory recalls
token breakdown
redaction manifest
```

默认页面先显示 Context Manifest；用户主动展开后才加载脱敏正文。以下内容不得保存：

- API Key 和敏感环境变量。
- `.env`、密钥文件等受保护文件正文。
- 供应商私有 reasoning_content。
- 被安全清洗器删除的原始值。

### 7.5 Tool Calls 与结果检查

每个 Tool 节点显示：

- Tool 名称、风险等级、参数摘要。
- 权限决定及匹配规则。
- 是否经过审批。
- 开始、结束和耗时。
- Result code、message、metadata。
- 截断输出与完整输出引用。
- 修改文件和验证证据。

对于写操作，支持跳转到当前文件和检查点信息，但不自动回滚。

### 7.6 首个错误标记

自动错误规则：

1. 第一个失败 Tool Result。
2. 第一个 Model API 或 Tool Protocol 错误。
3. 第一个验证拒绝。
4. 第一个重复工具/卡死恢复事件。
5. 第一个预算耗尽事件。
6. 用户手动添加的书签。

页面区分“根因候选”和“后续连锁失败”，避免把最后的任务失败简单当作起点。

### 7.7 从 Step 创建分支

“从 Step 分支”必须有清晰安全语义。

V1 实现 **Context Fork**：

- 从该 Step 之前的消息和计划创建新会话。
- 使用当前工作区文件状态，不伪装为历史文件状态。
- 检测历史文件哈希与当前文件哈希的漂移。
- 默认进入 Ask 或 Plan，由用户确认后才能 Act。
- 不自动重放写操作和命令。

V1 不承诺精确恢复当时文件系统。精确 Execution Fork 只有在 Git Commit、Worktree 或完整检查点覆盖得到证明后再实现。

### 7.8 推理强度对比

对比对象必须来自同一任务定义和可识别的起始仓库状态。页面按阶段对齐：

```text
Fast                      Deep
4 Steps                   7 Steps
3 Tool Calls              8 Tool Calls
12k Tokens                31k Tokens
8.2s                      28.4s
验证通过                   验证通过
```

进一步比较：

- 首次相关文件命中 Step。
- 重复工具调用。
- 修改文件数量。
- 验证次数。
- 最终证据等级。
- 成本和耗时。

不比较或展示私有思维文本。

### 7.9 API 草案

```text
GET  /api/sessions/{id}/replay
GET  /api/sessions/{id}/replay/turns/{turn_id}/steps/{step}
POST /api/sessions/{id}/replay/turns/{turn_id}/steps/{step}/fork
POST /api/replay/compare
POST /api/sessions/{id}/replay/bookmarks
```

## 8. 第三优先级：Context Compiler

### 8.1 目标

将上下文从一个不可见的大字符串，转化为有来源、有预算、有锁定规则、可以做 What-if 分析的编译产物。

### 8.2 Context Build Manifest

每次构建上下文时生成：

```json
{
  "build_id": "...",
  "total_chars": 85000,
  "estimated_tokens": 24000,
  "segments": [
    {"kind": "system", "tokens": 2100, "locked": true},
    {"kind": "repo_map", "tokens": 1300, "enabled": true},
    {"kind": "recent_messages", "tokens": 12400, "locked": true},
    {"kind": "history_summary", "tokens": 780, "enabled": true},
    {"kind": "tool_schemas", "tokens": 3600, "locked": true}
  ]
}
```

分段 Token 是近似值。由于消息序列化和分词边界并非严格可加，报告需额外提供 `serialization_overhead`，保证各段加总与总估算能够解释。

### 8.3 来源分类

建议统一为：

```text
core_system
mode_and_profile
verification_rules
project_rules
repo_map
current_plan
skill_catalog
project_memory
user_memory
history_summary
recent_messages
tool_schemas
serialization_overhead
```

### 8.4 页面结构

1. 顶部：总预算、实际发送、原始会话、压缩比例。
2. 中间：横向堆叠预算条，按来源着色。
3. 下方：来源表格，展示 Tokens、占比、相关性、重复率、截断和锁定状态。
4. 右侧：来源详情和具体文件/记忆/规则。
5. What-if 区：开关可选来源，立即计算预计变化。

### 8.5 可调与不可调来源

允许用户临时调整：

- Repo Map。
- 项目记忆。
- 用户记忆。
- Skills 目录摘要。
- 用户手动固定的文件。
- 可选历史摘要策略。

不可关闭：

- 核心系统安全提示。
- 当前模式和权限边界。
- 最新用户消息。
- Tool Call 与 Tool Result 协议完整组合。
- 强制项目规则和验证规则。

前端对不可关闭来源显示锁定图标和原因，不能仅禁用按钮而不给解释。

### 8.6 Repo Map 对比

V1 提供无模型调用的 Shadow Build：

```text
当前构建：Repo Map ON   24,000 Tokens   40 个文件
对照构建：Repo Map OFF  22,700 Tokens    0 个文件
差异：+1,300 Tokens，加入 8 个高相关符号
```

这只能说明上下文差异，不能证明回答质量提升。真实 A/B 需要用户主动运行两次模型任务，并明确显示额外 Token 成本。

### 8.7 重复与低价值检测

检测规则：

- 完全相同文本哈希。
- 规范化后重复。
- 最近消息与历史摘要高度重叠。
- 多条记忆内容相似。
- Repo Map 文件与用户已完整引用文件重复。
- 已失效文件路径或过期记忆。
- 长工具输出在最近消息中重复出现。

V1 使用确定性文本规则和 Jaccard 相似度；可选 Embedding 只作为辅助，并显示算法来源。

### 8.8 上下文质量评分

评分不代表模型回答正确率，只表示上下文工程质量。建议采用透明分项：

```text
相关性       30
新鲜度       20
协议完整性   20
预算平衡     15
来源可追溯性 15
- 重复惩罚
- 失效来源惩罚
- 严重截断惩罚
```

前端必须显示各分项，而不是只显示一个神秘的 82 分。

### 8.9 API 草案

```text
GET  /api/sessions/{id}/context/builds
GET  /api/sessions/{id}/context/builds/{build_id}
POST /api/sessions/{id}/context/shadow-build
GET  /api/sessions/{id}/context/preferences
PUT  /api/sessions/{id}/context/preferences
```

## 9. 第四优先级：Human-governed Agent Memory

### 9.1 目标

把“待确认记忆列表”升级成真正的记忆治理中心：减少重复、解释召回、暴露冲突、控制生命周期，同时保持“未经用户确认不进入长期记忆”的核心原则。

### 9.2 页面结构

顶部使用四个视图：

```text
候选收件箱｜长期记忆库｜冲突与过期｜召回审计
```

- 候选收件箱：按相似度聚类，不再平铺 40 条重复内容。
- 长期记忆库：筛选 fact、decision、preference、task。
- 冲突与过期：并排比较同一 subject 的不同结论。
- 召回审计：显示某次 Step 为什么召回、为什么未召回。

### 9.3 候选去重

分三个等级：

1. `exact_duplicate`：category 与规范化 content 完全一致。
2. `near_duplicate`：关键词和文本 Jaccard 相似度超过阈值。
3. `same_subject`：主题相同但结论不同，作为冲突候选处理。

候选聚类显示：

```text
“完成 book-recommendation 站点” × 6
来源：4 个 Turn
建议：合并为 1 条 task，或全部忽略
```

批量操作必须保留每条候选的来源记录。

### 9.4 生命周期字段

在现有 Memory 结构上增加：

```text
pinned
archived
expires_at
last_recalled_at
recall_count
last_verified_at
verification_status
duplicate_of
supersedes
```

所有操作继续使用追加式审计记录：

- pin / unpin。
- archive / restore。
- expire。
- merge。
- reweight。
- verify。

### 9.5 召回解释

现有检索已经有 lexical、semantic、recency、importance 和仓库证据。前端将其展示为可解释分解：

```text
总分 12.8
├─ 文本命中       +6.0
├─ 关键词命中     +2.0
├─ 重要程度       +1.4
├─ 新鲜度         +0.9
└─ 语义相似       +2.5
```

同时显示：

- 注入了哪个 Step。
- 占用多少 Token。
- 关联文件是否仍存在。
- 是否为同主题最新记录。
- 是否存在冲突记录。

### 9.6 记忆冲突对比

同一 `category + subject` 出现不同内容时，使用左右对比：

```text
旧记录                         新记录
使用 Python 3.11               使用 Python 3.12
来源 Turn A                    来源 Turn D
文件证据 missing               文件证据 verified
更新时间较早                    更新时间较新
```

用户可以：

- 保留两条并降低旧记录权重。
- 将新记录标记为 supersedes 旧记录。
- 合并为一条新记忆。
- 归档其中一条。

系统不得自动删除冲突中的旧记录。

### 9.7 从仓库重新验证

V1 验证范围保持保守：

- 检查关联文件是否存在。
- 检查关联符号是否仍能在 Repo Map 中找到。
- 检查保存时哈希与当前哈希是否变化。
- 对 task 检查关联文件和验证证据，不自动判断业务任务完成。

结果为 `verified / partial / missing / stale / not_applicable`，避免模型主观判断直接覆盖事实。

### 9.8 API 草案

```text
GET  /api/sessions/{id}/memory/governance
POST /api/sessions/{id}/memory/candidates/bulk-resolve
POST /api/sessions/{id}/memory/cluster/{cluster_id}/merge
PATCH /api/sessions/{id}/memory/{memory_id}
POST /api/sessions/{id}/memory/{memory_id}/revalidate
GET  /api/sessions/{id}/memory/recall-audit
```

删除、清空和合并继续视为高风险写操作，并保留确认。

## 10. 前端工程方案

### 10.1 保持现有技术栈

项目当前使用原生 HTML、CSS、JavaScript 和 FastAPI。V1 不需要引入 React 或图表库，但应避免继续扩大单个 `app.js` 和 `modern.css`。

建议拆分：

```text
src/coding_agent/web/static/
├─ app.js
├─ api.js
├─ state.js
├─ workbench-shell.js
├─ features/
│  ├─ algorithm-lab.js
│  ├─ time-travel.js
│  ├─ context-compiler.js
│  └─ memory-governance.js
├─ charts/
│  ├─ line-chart.js
│  ├─ budget-bar.js
│  └─ timeline.js
└─ styles/
   ├─ workbench.css
   ├─ algorithm-lab.css
   ├─ replay.css
   ├─ context.css
   └─ memory.css
```

使用 ES Modules，保持构建零依赖。公共状态、API、格式化和事件订阅只实现一次。

### 10.2 公共组件

- `EvidenceBadge`：证据等级。
- `MetricStrip`：紧凑关键指标。
- `Inspector`：右侧详情检查器。
- `Timeline`：Step 与事件序列。
- `SourceTable`：可排序来源表。
- `DiffPair`：冲突/前后对比。
- `EmptyState`：没有报告、回放或记忆时的下一步提示。
- `RunStatus`：运行、取消、失败和完成。
- `ExportButton`：导出 JSON/Markdown。

### 10.3 交互状态

每个工作台必须覆盖：

- 首次空状态。
- 正在加载。
- 后台运行中。
- 部分结果到达。
- 用户取消。
- 权限等待。
- 报告完成。
- 数据过期。
- 原始事件已被存储清理。
- API 错误与恢复重试。

### 10.4 响应式

- `≥ 1440px`：三列分析工作台 + 右侧对话。
- `1100–1439px`：检查器改为可折叠抽屉。
- `760–1099px`：分析工作台占主区域，对话切换为覆盖层。
- `< 760px`：提供只读报告和列表；复杂图表允许横向滚动，不强行堆成卡片。

### 10.5 无障碍与性能

- 所有图表提供文本表格替代。
- 状态不只依靠颜色表达。
- 时间线、标签和表格支持键盘操作。
- 动画遵守 `prefers-reduced-motion`。
- 事件列表虚拟化或分页，不能一次渲染数千事件。
- 高频 case 结果按 100～200 ms 合并刷新，避免页面抖动。

## 11. 后端代码组织建议

建议逐步将大型 Web 路由拆分：

```text
src/coding_agent/
├─ algorithm/
│  ├─ problem_spec.py
│  ├─ generators.py
│  ├─ oracle.py
│  ├─ benchmark.py
│  └─ report.py
├─ replay/
│  ├─ reducer.py
│  ├─ snapshot.py
│  ├─ compare.py
│  └─ fork.py
├─ context_compiler/
│  ├─ manifest.py
│  ├─ scoring.py
│  └─ preferences.py
├─ memory_governance.py
└─ web/routes/
   ├─ algorithm_lab.py
   ├─ replay.py
   ├─ context.py
   └─ memory.py
```

原则：

- Agent Loop 只发布事实事件，不负责拼装页面 ViewModel。
- Web API 从服务层读取派生报告。
- 前端不自行推断安全状态和验证结论。
- 报告生成失败不能破坏原始会话和任务结果。

## 12. 新增事件建议

```text
algorithm_spec_parsed
algorithm_run_started
algorithm_cases_generated
algorithm_case_batch_finished
algorithm_failure_minimized
algorithm_benchmark_point
algorithm_report_ready

context_manifest_built
context_preference_changed
context_shadow_build_ready

replay_snapshot_written
replay_bookmark_added
session_forked_from_step

memory_candidates_clustered
memory_merged
memory_archived
memory_revalidated
```

事件只存储必要元数据；大报告使用引用路径，避免 JSONL 无限膨胀。

## 13. 安全与可信边界

### 13.1 算法运行

- Candidate 和 Oracle 命令仍需经过权限系统。
- 延续超时、输出限制、取消和敏感环境变量过滤。
- 限制并发进程数和总测试时间。
- 页面明确显示运行命令和工作目录。

### 13.2 回放

- 不保存私有 reasoning_content。
- 上下文正文默认脱敏并延迟加载。
- 从 Step 分支不自动重放副作用。
- 工作区漂移必须在分支前展示。

### 13.3 上下文控制

- 安全提示、权限规则和 Tool 协议不可关闭。
- 用户关闭 Repo Map 或记忆只影响后续构建，不篡改历史回放。
- A/B 模型调用需要显示额外 Token 成本并单独确认。

### 13.4 记忆

- 候选不会自动提升为长期记忆。
- 合并、归档、删除保留审计事件。
- 项目记忆和用户记忆继续严格分目录。
- 仓库验证结果不得覆盖用户原始记录。

## 14. 测试方案

### 14.1 单元测试

- ProblemSpec 解析和警告。
- 固定 seed 用例生成可复现。
- Oracle 超时、崩溃和输出限制。
- Shrinker 保持失败性质。
- Benchmark 分位数计算。
- StepFrame 事件聚合。
- Context 分段 Token 对账。
- 重复检测与质量评分。
- 记忆聚类、过期、固定、归档和冲突。

### 14.2 API 测试

- 报告创建、查询、取消和导出。
- 回放缺失事件、旧版本事件和被清理引用。
- Fork 权限与工作区漂移。
- Context 不可关闭来源。
- 记忆批量操作的幂等性。
- 所有接口的路径越界和敏感数据清洗。

### 14.3 前端测试

- JavaScript 语法检查。
- API 错误、空状态和运行状态。
- 128 个 Case 的增量渲染。
- 1,000+ 事件的回放分页。
- 上下文开关与 Shadow Build。
- 候选聚类和冲突操作。
- 页面刷新后保持选中的工作台和运行。

### 14.4 浏览器验收

在桌面与窄屏分别验证：

- 算法报告首屏不滚动即可看到五个核心结论。
- 时间线能够跳到首个错误。
- Context 来源表可排序和展开。
- 记忆候选可批量忽略或合并。
- 对话栏折叠后不遮挡核心操作。
- 键盘焦点、对比度和减少动画模式正常。

## 15. 分阶段实施

### Phase 0：公共壳与数据口径（1～2 天）

- 增加研究工作台入口和主区域 Shell。
- 拆分公共 API、状态和基础组件。
- 统一 Step/Turn/Session Token 口径。
- 定义证据等级和报告版本。

验收：四个工作台均有真实空状态和路由，但没有伪造数据。

### Phase 1：算法实验室纵向切片（3～5 天）

- 复用现有 Complexity 和 Judge。
- 持久化算法报告。
- 实现 Case 分类、耗时、首个失败和最小反例页面。
- 增加简单数组/字符串边界生成。
- 实现 JSON/Markdown 导出。

验收：一个存在缺陷的排序程序能够被随机/边界测试发现，页面展示最小反例和报告。

### Phase 2：Oracle 与复杂度曲线（2～4 天）

- 暴力 Oracle 命令。
- 固定 seed 随机对拍。
- Benchmark Runner 和 P50/P95 曲线。
- 128+ 用例批次执行。

验收：Candidate 与 Oracle 对拍可复现；报告不会声称绝对正确。

### Phase 3：Time-Travel Debugger（3～5 天）

- StepFrame 聚合。
- 时间线、流图和事件检查器。
- 脱敏 Context Manifest。
- 首个错误定位和书签。
- Context Fork V1。

验收：可从一次真实失败任务定位首个错误，并从历史 Step 创建不会重放副作用的新会话。

### Phase 4：Context Compiler（3～4 天）

- 上下文分段和 Token 对账。
- 预算条、来源表和详情。
- 安全可调来源。
- Repo Map Shadow Build。
- 重复检测和透明质量评分。

验收：用户能解释最后一个 Step 的 Token 花在哪里，并预览关闭 Repo Map 后的变化。

### Phase 5：Memory Governance（3～5 天）

- 候选聚类与批量操作。
- pinned、archived、expires_at 等生命周期字段。
- 召回分数解释。
- 冲突对比和仓库重新验证。

验收：当前重复候选能够聚类收敛；用户可以看到某条记忆为什么被召回并处理冲突。

## 16. 需求验收矩阵

| 原始需求 | 验收证据 |
| --- | --- |
| 自动解析题目约束 | ProblemSpec 页面显示字段、置信度和原文来源 |
| 自动生成边界测试 | 报告中存在 boundary 分类及生成规则 |
| 随机对拍 | 固定 seed 重跑结果一致 |
| 暴力解法 Oracle | 报告记录 Oracle 哈希、命令和证据等级 |
| 复杂度实测曲线 | 至少 4 个规模、P50/P95 和超时点 |
| 最小失败反例 | 页面展示原始输入、缩减轨迹和最终反例 |
| 算法正确性报告 | JSON/Markdown 可导出且结论为有限证据表达 |
| 按 Step 回放 | 时间线可选择每个 Step 并查看节点 |
| 查看实际上下文 | 脱敏快照展示来源、正文和 Tool Schemas |
| 查看 Tool Calls | 参数、权限、结果和耗时可追溯 |
| 从 Step 分支 | 新会话记录来源 Step，且不自动重放副作用 |
| 比较推理强度 | 两次运行按阶段、成本和验证对齐 |
| 标记错误起点 | 自动规则与人工书签均可跳转 |
| 分来源 Token | 所有上下文段显示 Tokens 和占比 |
| 文件入选原因 | Repo Map 项显示分数和 reason |
| 手动禁用来源 | 可调来源影响下一次构建，锁定来源不可关闭 |
| Repo Map 对比 | Shadow Build 显示开关前后差异 |
| 重复/低价值检测 | 展示规则、命中项和惩罚分 |
| 上下文质量评分 | 分项透明，可复算，不暗示回答正确性 |
| 记忆相似合并 | 重复候选聚类并支持批量处理 |
| 记忆有效期 | 过期记录不默认注入并显示原因 |
| 记忆冲突对比 | 同 subject 不同结论并排展示 |
| 召回原因 | 显示 lexical/semantic/recency/importance 分项 |
| 固定、降权、归档 | 操作可恢复并写入审计记录 |
| 仓库重新验证 | 文件、符号和哈希状态可刷新 |

## 17. 推荐演示场景

使用一个“样例通过、随机测试失败”的数组算法：

1. Agent 在算法模式读取题目和错误代码。
2. 算法实验室解析 `n` 和数组范围。
3. 边界与随机对拍找到错误。
4. Shrinker 得到 `[0, 1, 0]`。
5. Agent 修复代码并重新验证。
6. 报告显示 128/128 通过和 P95。
7. Time-Travel 跳回首次失败 Step。
8. Context Compiler 展示算法 Profile 如何减少无关 Repo Map。
9. Memory Governance 将“本项目算法验证命令”确认为长期记忆。

这个场景能在一次任务中串联四个差异化页面，比演示普通网页生成更能体现项目价值。

## 18. 风险与限制

| 风险 | 应对 |
| --- | --- |
| 任意题目无法自动生成可信 Oracle | 要求用户/Agent提供暴力程序并显示可信等级 |
| 大量测试拖慢 UI | 批次事件、取消、分页和刷新节流 |
| 回放快照泄露敏感信息 | 复用 Redactor、正文延迟加载、私有推理永不持久化 |
| 从 Step 分支与历史文件不一致 | 显示哈希漂移，V1只做 Context Fork |
| Token 分段与总量不完全相加 | 显示序列化开销和估算器名称 |
| 质量评分被误解为答案正确率 | 显示分项和明确免责声明 |
| 候选去重错误合并不同事实 | 自动聚类但由用户确认合并 |
| 页面继续膨胀 | 独立工作台、模块拆分、虚拟化列表 |

## 19. 建议提交顺序

```text
feat(workbench): add differentiation workspace shell
feat(algorithm): persist reliability run reports
feat(algorithm): add deterministic generators and oracle comparison
feat(web): add algorithm reliability lab
feat(replay): derive step frames from session events
feat(web): add time-travel debugger
feat(context): emit source-level context manifests
feat(web): add context compiler controls
feat(memory): add candidate clustering and lifecycle metadata
feat(web): add memory governance center
test(e2e): cover differentiation workbench flows
docs: document evidence levels and demo workflow
```

每个阶段单独提交、单独运行 CI，避免把四个工作台合并成一个无法审查的大 Commit。

## 20. 最终完成定义

Differentiation Workbench V1 只有同时满足以下条件才算完成：

- 四个入口都使用真实后端数据，不存在不可点击的展示控件。
- 算法实验室能够发现并复现至少一个隐藏缺陷。
- 回放页面能从原始事件重建 Step，并安全创建 Context Fork。
- 上下文页面能解释最后一个 Step 的主要 Token 来源。
- 记忆页面能合并重复候选、展示冲突和解释召回。
- 所有结论都有 evidence level，不夸大模型生成内容的可信度。
- 敏感数据、私有思维内容和越界文件不会进入报告。
- 桌面和窄屏完成浏览器验收。
- 单元、API、Web 和端到端测试通过。
- README、答辩说明和演示视频使用同一套产品定位。

完成后，项目的差异化叙事不再是“拥有许多 Coding Agent 常见功能”，而是：

> Code Helper 将算法验证、Agent 回放、上下文编译和人类治理记忆整合为一个可观测、可验证、可控制的本地 Agent 实验工作台。
