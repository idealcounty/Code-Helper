# Code Helper 算法实验室启动与性能优化方案

> 文档状态：实施中（Phase 1～3 核心路径已完成，Phase 4～5 为后续增强）  
> 目标版本：Algorithm Reliability Lab V2  
> 核心目标：将算法实验室从“由 Agent 多轮编排的工具流程”改造成“按钮直接启动的确定性验证任务”，在保留可靠性证据的前提下，把常见算法题验证从约 12 分钟降低到 1～2 分钟。

> 实现进度（2026-09-01）：Phase 1～3 的核心路径已落地：一键启动、Quick/Standard/Full、确定性协调器、分阶段进度事件、Fail-fast、取消、缓存、有界并发和报告回放接口已实现；Phase 4 的模型辅助 Oracle、批量输入适配与更细粒度性能仪表盘仍可继续增强。页面刷新时会自动恢复活动 Run 的状态轮询。

当前实现入口：后端协调器位于 `src/coding_agent/algorithm/coordinator.py`，Run API 位于 `src/coding_agent/web/app.py`，实验室按钮与进度面板位于 `src/coding_agent/web/static/`，回归覆盖见 `tests/test_algorithm_coordinator.py` 与 `tests/test_algorithm_lab_web.py`。

## 1. 背景与问题

当前算法实验室已经具备题面约束解析、边界测试、随机测试、Oracle 对拍、最小失败反例、耗时统计和报告持久化等能力，但启动方式仍依赖用户在对话中要求 Agent 调用：

```text
judge_algorithm
run_algorithm_experiment
generate_algorithm_cases
```

前端只能提示：

```text
如何开始
在对话中运行 judge_algorithm
报告会自动出现在这里
```

这会产生两个明显问题：

1. 用户不能从算法实验室直接开始一次验证，产品入口不完整。
2. Agent 需要通过多轮模型请求决定“读什么、生成什么、调用哪个工具、下一步做什么”，导致确定性验证被模型思考时间包围。

实际观察到：

- 不开启完整算法实验时，完成一道题约需 1 分钟。
- 开启完整算法实验后，同一类任务可能接近 12 分钟。
- 时间增加约 12 倍，明显超过可靠性验证本身应产生的成本。

本次优化的原则不是简单减少测试数量，而是将“需要模型推理的工作”和“可以由程序确定执行的工作”彻底分离。

## 2. 当前耗时链路分析

### 2.1 当前执行方式

目前一次完整任务大致经过：

```text
用户提出算法题
→ Agent 构建上下文
→ 模型分析题目
→ 读取代码或创建文件
→ 再次请求模型
→ 编译代码
→ 再次请求模型
→ 生成测试用例
→ 再次请求模型
→ 调用 judge_algorithm 或 Oracle 对拍
→ 工具串行执行大量用例
→ 发现失败后串行 Shrink
→ 再次请求模型分析报告
→ 修改代码
→ 重新进行以上部分流程
→ 最终生成报告
```

Agent Loop 每进入一个新 Step，都会重新构建上下文并请求模型。Deep 推理档位下，单次请求可能持续数十秒，最坏可接近当前 120 秒单请求上限。

### 2.2 可能的主要耗时来源

以下比例是当前实现的工程推断，需要在 Phase 0 通过指标进一步验证：

| 耗时来源 | 当前行为 | 可能影响 |
| --- | --- | --- |
| 多轮模型编排 | 每个阶段完成后重新请求模型决定下一步 | 可能占总耗时 50%～75% |
| Deep 推理 | 算法模式仍继承用户选择的高推理强度 | 单次模型请求可能 30～120 秒 |
| 上下文重复构建 | 每个 Step 重复整理消息、Tools Schema 和历史结果 | 增加 Token、序列化与模型首 Token 延迟 |
| Candidate / Oracle 串行 | 每个用例先执行 Oracle，再执行 Candidate | 用例数量增加时近似线性增长 |
| 频繁创建子进程 | 每个用例分别启动一个命令进程 | Windows 下进程启动成本较明显 |
| Shrinker 串行验证 | 最多生成约 32 个候选，每个候选再运行 Candidate 与 Oracle | 一次失败可能额外启动 64 个进程 |
| 验证阶段无分层 | 样例、边界、随机、Shrink、Benchmark 可能一次全部执行 | 用户长时间得不到阶段性结果 |
| 失败后继续完整实验 | 已经得到稳定反例后仍可能继续运行其余用例 | 增加无价值等待 |
| 审批粒度不合适 | 如果命令被拆为多次 Tool Call，可能多次等待审批 | 人工等待时间进入总耗时 |

### 2.3 关键结论

算法实验室本质上是确定性测试流水线，不应该由通用 Agent Loop 逐步编排。

应该建立两个相互协作但彼此独立的执行通道：

```text
Agent 解题通道
负责：理解题意、设计算法、编写和修复代码

Algorithm Run Coordinator
负责：编译、生成用例、执行、对拍、Shrink、Benchmark、保存报告
```

模型只参与无法确定性完成的部分，例如辅助生成暴力 Oracle；测试执行本身不再反复调用模型。

## 3. 优化目标与性能指标

### 3.1 用户体验目标

- 算法实验室具有独立的“开始实验”按钮。
- 用户无需记忆或手动输入 `judge_algorithm`。
- 点击后 2 秒内显示任务已启动和当前阶段。
- 先快速给出样例与边界结果，再决定是否继续完整随机对拍。
- 实验运行不阻塞对话，用户可以继续查看文件或切换页面。
- 支持随时取消，取消响应目标不超过 500 ms。
- 已有报告可以直接重新运行，不要求模型重新理解题目。

### 3.2 V2 性能目标

以下目标针对源码已经存在、编译器和运行环境正常的常见单文件算法题：

| 模式 | 目标 P50 | 目标 P95 | 测试范围 |
| --- | ---: | ---: | --- |
| Quick Check | ≤ 20 秒 | ≤ 45 秒 | 样例 + 少量边界 + 8～16 个随机用例 |
| Standard Verify | ≤ 60 秒 | ≤ 120 秒 | 样例 + 边界 + 32～64 个随机用例 + 有限 Shrink |
| Full Audit | ≤ 180 秒 | ≤ 300 秒 | 128～256 用例 + 完整 Shrink + Benchmark |

附加指标：

- 确定性路径模型请求次数：`0`。
- 需要生成 Oracle 时模型请求次数：最多 `1～2` 次。
- 单次实验 Agent Step 数：`0`，实验使用独立 Run Stage。
- 相同源码、题面、Seed 和配置重复运行时缓存命中率目标：`≥ 80%`。
- 发现第一个稳定失败反例后的默认停止时间：`≤ 5 秒`。
- 常规实验相较当前约 12 分钟流程，目标总耗时降低 `75%～90%`。

这些时间属于工程验收目标，不应在实现前作为已经达到的结果展示。

## 4. 产品入口优化

### 4.1 替换“如何开始”提示

将当前只读提示：

```text
在对话中运行 judge_algorithm
```

替换为真正的主操作按钮：

```text
[ 开始算法实验 ]
```

按钮旁提供三个运行档位：

```text
Quick      快速发现明显问题
Standard   默认，可靠性与速度平衡
Full       完整对拍、Shrink 和 Benchmark
```

默认选择 `Standard`，避免用户不理解选项时直接进入最慢的完整实验。

### 4.2 启动面板

点击按钮后打开轻量配置面板：

```text
源文件              solution.cpp
语言                C++17（自动识别）
Candidate 命令      自动生成，可编辑
题面来源            当前对话 / 粘贴题面 / Markdown 文件
Oracle              已配置 / 待生成 / 仅显式期望输出
Seed                20260901
运行档位            Standard
失败策略            首个稳定失败后停止
```

提供两个主按钮：

```text
[ 开始验证 ]  [ 取消 ]
```

高级选项默认折叠，避免用户必须理解全部实验参数。

### 4.3 已有报告的快捷操作

每份历史报告增加：

- 使用相同配置重新运行。
- 只重新运行失败用例。
- 修复代码后重新验证。
- 升级为 Full Audit。
- 导出报告。

这些操作直接复用报告内的配置，不再请求模型重新选择工具。

## 5. 新的执行架构

### 5.1 Algorithm Run Coordinator

新增独立的 `AlgorithmRunCoordinator` 服务，负责一次算法实验的完整生命周期。

它不经过通用 Agent Loop，不产生模型 Step，而是使用明确的阶段状态机：

```text
IDLE
→ CONFIGURING
→ PARSING_SPEC
→ PREPARING
→ COMPILING
→ SMOKE_TESTING
→ BOUNDARY_TESTING
→ RANDOM_TESTING
→ MINIMIZING_FAILURE（按需）
→ BENCHMARKING（按需）
→ REPORTING
→ COMPLETED / FAILED / CANCELLED
```

每个阶段都具有：

- 独立时间预算。
- 可取消状态。
- 输入和输出 Schema。
- 进度事件。
- 可缓存结果。
- 明确的证据等级。

### 5.2 与 Agent Loop 的关系

新的关系为：

```text
Agent Loop
├─ 编写或修复算法代码
├─ 可选：请求实验室开始一次 Run
└─ 可读取最终报告摘要

Algorithm Run Coordinator
├─ 不请求模型决定下一阶段
├─ 不向 Agent 历史重复写入全部 Case 结果
├─ 独立执行确定性测试
└─ 只发布进度、失败摘要和最终报告引用
```

用户从按钮启动时，整个实验可以在没有任何模型调用的情况下完成。

用户从对话要求“帮我验证”时，Agent 也只需调用一次高层工具：

```text
start_algorithm_run
```

而不是依次调用生成用例、Judge、Shrink 和 Benchmark 工具。

### 5.3 高层工具与底层服务分离

建议保留现有工具兼容性，但新增统一高层入口：

```text
start_algorithm_run
get_algorithm_run
cancel_algorithm_run
```

`start_algorithm_run` 只负责创建实验任务，真正执行交给 Coordinator。

现有工具逐步变为内部能力：

```text
generate_algorithm_cases
judge_algorithm
run_algorithm_experiment
analyze_complexity
```

这样既能兼容当前 Agent Profile，也能避免模型在这些工具之间反复规划。

## 6. 分层与自适应验证策略

### 6.1 Quick Check

目标是尽快发现低成本错误：

1. 编译 Candidate。
2. 运行题目样例。
3. 运行 4～8 个关键边界用例。
4. 运行 8～16 个固定 Seed 随机用例。
5. 找到失败后只做最多 5 秒的快速 Shrink。
6. 不执行完整 Benchmark。

适合：

- 用户刚写完代码。
- 每次修改后的快速回归。
- Agent 自动修复循环中的中间验证。

### 6.2 Standard Verify

作为默认档位：

1. 样例测试。
2. 8～16 个边界用例。
3. 32～64 个固定 Seed 随机用例。
4. 只缩减第一个可稳定复现的 Wrong Answer。
5. 使用有限 Shrink 预算，例如 10～15 秒或 16 次候选。
6. 仅执行 3 个输入规模的轻量 Benchmark。

### 6.3 Full Audit

由用户明确选择：

1. 完整样例、边界、随机和回归用例。
2. 128～256 个用例。
3. 结构化 Shrink。
4. 5 个以上输入规模和多次预热/重复。
5. 完整 Markdown / JSON 报告。

Full 模式不应成为自动默认值，也不应在 Agent 每次代码修复后自动重新执行。

### 6.4 Fail-fast 与继续策略

默认策略：

```text
首个失败
→ 重新执行一次确认稳定性
→ 进行有限 Shrink
→ 生成失败报告
→ 停止其余随机与 Benchmark
```

用户可以选择“继续收集全部失败”，但该选项放入高级设置。

对于 Oracle Error、编译错误和环境错误应立即停止，因为继续运行无法提供更高质量证据。

## 7. 减少模型思考时间

### 7.1 确定性优先

以下内容不需要模型：

- 根据扩展名识别语言。
- 根据语言生成默认编译命令。
- 解析常见数值约束。
- 生成固定模板边界用例。
- 执行显式输入输出用例。
- 运行用户已经提供的 Oracle。
- 统计 P50 / P95。
- 保存和渲染报告。

这些阶段必须直接运行，不能为了“确认下一步”重新请求模型。

### 7.2 模型只做一次结构化规划

当题目无法确定性解析，或者没有 Oracle 时，允许一次可选的“Assisted Setup”请求：

```json
{
  "problem_spec": {},
  "oracle_source": "...",
  "generator_schema": {},
  "warnings": []
}
```

要求：

- 使用结构化 JSON Schema。
- 默认使用 Fast 或 Balanced，而非继承 Deep。
- 最大输出严格受限。
- 生成后由用户确认，再进入确定性管线。
- 不允许模型在每一批测试后重新决定下一批。

### 7.3 推理强度解耦

解题推理强度和实验编排强度需要分开：

```text
代码求解：使用用户选择的 Fast / Balanced / Deep
实验执行：不使用模型
辅助生成 Oracle：固定 Fast 或 Balanced
失败解释：可选一次 Fast 总结
```

即使用户在顶部选择 Deep，也不应让每个验证阶段都进行 Deep 思考。

### 7.4 控制返回 Agent 的数据量

一次包含 128 个 Case 的完整结果不应全部塞回对话上下文。

返回 Agent 的内容只包括：

```text
status
passed / total
first_failure
minimized_input
p95_ms
report_path
```

完整用例结果写入 `.code-helper/algorithm-runs/`，需要时由工作台按页读取。

这样可以显著减少下一次模型请求的输入 Token 和上下文构建时间。

## 8. 执行层性能优化

### 8.1 编译一次

同一次 Run 中：

- Candidate 只编译一次。
- Oracle 只编译一次。
- 编译产物使用 Run 专属临时目录。
- 源码没有变化时允许复用编译缓存。

缓存键建议为：

```text
source_sha256
compiler_command
compiler_version
language_standard
relevant_dependency_hash
```

### 8.2 有界并发

当前 Candidate 和 Oracle 按用例串行执行。V2 可采用有界并发：

- 同一用例的 Candidate 与 Oracle可并行启动。
- 不同用例使用 `Semaphore` 控制并发。
- 默认并发度建议 `min(4, CPU 核心数)`。
- Benchmark 阶段关闭并发，避免互相干扰计时。
- 内存和 CPU 高占用题型自动降低并发度。

不能无限并发，否则会造成结果抖动、系统卡顿或掩盖超时问题。

### 8.3 批量输入

如果题目输入格式包含测试组 `t`，且 ProblemSpec 能够可靠组合用例，可将多个 Case 合并为一次进程输入：

```text
多个逻辑 Case
→ 一个批量 stdin
→ 一次 Candidate 进程
→ 一次 Oracle 进程
→ 按输出行或结构拆分结果
```

无法可靠拆分输出时，退化为单 Case 进程，不能为了速度损害判题正确性。

### 8.4 Shrinker 优化

现有 Shrinker 最多生成约 32 个候选，每个候选都可能运行 Candidate 与 Oracle。

建议改为：

1. 只处理第一个稳定 Wrong Answer。
2. 先使用删除测试组、删除区间等高收益操作。
3. 使用二分式 Delta Debugging，而非只线性枚举候选。
4. 缓存每个输入哈希的 Candidate / Oracle 输出。
5. 设置最大调用次数和最大墙钟时间。
6. Quick / Standard / Full 使用不同 Shrink 预算。

### 8.5 Benchmark 延迟执行

Benchmark 只有在正确性测试通过后才有价值。

默认顺序应为：

```text
正确性通过
→ 用户选择或 Standard / Full 策略允许
→ 执行 Benchmark
```

如果已经发现 Wrong Answer，则直接跳过 Benchmark，并在报告中说明：

```text
Benchmark skipped because correctness verification failed.
```

## 9. 缓存与增量验证

### 9.1 Run 缓存键

实验缓存至少包含：

```text
candidate_source_hash
oracle_hash
problem_spec_hash
generator_version
seed
run_profile
compiler_command
case_set_hash
```

只有所有关键字段一致时，才可以复用确定性结果。

### 9.2 增量策略

| 变化 | 可复用内容 |
| --- | --- |
| 仅切换页面 | 全部运行状态和报告 |
| 源码未变化，重新打开报告 | 全部结果 |
| 仅增加随机数量 | 已完成用例，执行新增部分 |
| 仅切换 Seed | 编译产物可复用，用例结果不可复用 |
| Candidate 源码变化 | ProblemSpec、Oracle、用例可复用 |
| Oracle 变化 | Candidate 编译可复用，期望结果重新计算 |
| 编译命令变化 | 必须重新编译 Candidate |

### 9.3 修复后的回归

代码修复后优先执行：

1. 上一次最小失败反例。
2. 上一次失败用例。
3. 样例。
4. 边界用例。
5. 再决定是否重新运行随机全集。

这样可以在几秒内判断修复是否覆盖了已知问题。

## 10. 权限与安全

### 10.1 一次实验一次批准

从按钮启动时，页面应在运行前一次性展示：

- Candidate 编译与运行命令。
- Oracle 编译与运行命令。
- 工作目录。
- 最大进程数。
- 最大运行时间。
- 最大输出与用例数量。

用户批准后，为该 `run_id` 创建范围授权；同一 Run 内的受限子进程不再逐 Case 请求审批。

授权不能扩展到：

- 其他工作区。
- 不同命令哈希。
- Run 完成后的新命令。
- 文件写入和网络访问。

### 10.2 保留现有安全边界

- 清除 Key、Token、Secret 等敏感环境变量。
- 限制 stdout / stderr。
- 保留进程树终止。
- 支持取消。
- 限制运行目录在工作区或专属临时目录。
- 报告继续经过 Redactor。
- 不将模型私有 reasoning_content 写入报告。

## 11. 后端 API 规划

建议新增：

```text
POST /api/sessions/{id}/algorithm-lab/runs
GET  /api/sessions/{id}/algorithm-lab/runs/{run_id}
POST /api/sessions/{id}/algorithm-lab/runs/{run_id}/cancel
POST /api/sessions/{id}/algorithm-lab/runs/{run_id}/retry
GET  /api/sessions/{id}/algorithm-lab/runs/{run_id}/events
GET  /api/sessions/{id}/algorithm-lab/runs/{run_id}/report.md
```

创建请求示例：

```json
{
  "source_path": "solution.cpp",
  "problem_text": "...",
  "candidate_command": ".\\solution.exe",
  "oracle_command": ".\\brute.exe",
  "profile": "standard",
  "seed": 20260901,
  "stop_on_first_failure": true
}
```

运行状态示例：

```json
{
  "run_id": "...",
  "status": "running",
  "stage": "random_testing",
  "progress": {
    "completed_cases": 31,
    "total_cases": 64,
    "elapsed_ms": 18342
  },
  "partial_result": {
    "passed": 31,
    "failed": 0
  }
}
```

## 12. 前端反馈与流式进度

点击开始后，算法实验室展示阶段时间线：

```text
✓ 题目约束解析       120 ms
✓ Candidate 编译     1.4 s
✓ 样例测试            3 / 3
✓ 边界测试           12 / 12
● 随机对拍           31 / 64
○ 最小反例           等待失败输入
○ Benchmark          等待正确性验证
```

需要显示：

- 当前阶段。
- 已完成 / 总用例。
- 已耗时和预计剩余时间。
- 当前是否使用缓存。
- Candidate / Oracle 进程状态。
- 停止按钮。
- 部分结果。

进度使用现有 WebSocket 通道或独立 Run Event 流推送，避免前端高频轮询。

在报告生成前，用户也应该能看到样例和边界阶段的部分结果。

## 13. 可观测性与性能诊断

Phase 0 必须先补齐以下指标，验证 12 分钟究竟花在哪里：

```text
algorithm_run_total_ms
model_request_count
model_request_total_ms
context_build_count
context_build_total_ms
compile_ms
candidate_process_count
oracle_process_count
candidate_execution_total_ms
oracle_execution_total_ms
case_generation_ms
shrink_attempts
shrink_total_ms
benchmark_total_ms
approval_wait_ms
report_write_ms
cache_hits / cache_misses
```

算法实验报告顶部可以显示：

```text
总耗时        68.2s
模型等待       0s
编译          1.8s
正确性测试    23.4s
最小反例      11.2s
Benchmark    28.7s
其他          3.1s
```

该拆分能证明优化来自哪里，也便于后续继续迭代。

## 14. 实施阶段

### Phase 0：基线测量

- 在现有 Agent 驱动流程中记录阶段耗时。
- 使用同一题目分别运行普通模式与实验室模式至少 5 次。
- 记录模型请求数、Step 数、进程数和用例数。
- 得到可复现的 12 分钟基线。

验收：能够用数据解释至少 90% 的总耗时。

### Phase 1：按钮与直接 Run

- 增加“开始算法实验”按钮。
- 增加启动面板与运行档位。
- 实现 Algorithm Run Coordinator。
- 直接调用现有 Judge 能力，不经过 Agent Loop。
- 实现运行进度、取消和报告刷新。

验收：使用显式用例和已有命令时，模型请求数为 0。

### Phase 2：分层、Fail-fast 与预算

- 实现 Quick / Standard / Full。
- 为每个 Stage 增加时间与数量预算。
- 首个稳定失败后默认停止。
- Benchmark 仅在正确性通过后运行。
- 不将全部 Case 结果写回对话上下文。

验收：明显错误代码能够在 Quick 模式 20 秒左右给出失败结果。

### Phase 3：缓存和执行优化

- 编译缓存。
- 用例和 Oracle 输出缓存。
- 有界并发。
- 可识别多测试输入时批量执行。
- Shrinker 缓存与 Delta Debugging。

验收：相同配置重复运行时，大部分阶段命中缓存；Standard P95 达到 120 秒目标。

### Phase 4：可选模型辅助

- 仅在确定性解析不足时调用模型。
- 使用结构化输出生成 ProblemSpec、Oracle 或 Generator 草稿。
- 固定 Fast / Balanced 推理预算。
- 用户确认后才执行生成的 Oracle。

验收：辅助模式模型调用不超过 2 次，且不会在每批 Case 后重新请求模型。

### Phase 5：性能回归与产品验收

- 为 Coordinator、状态机、缓存和取消增加单元测试。
- 增加 API 和前端测试。
- 建立固定算法题性能基准集。
- 比较优化前后的 P50 / P95。
- 验证报告正确性没有因加速而下降。

## 15. 测试计划

### 15.1 功能测试

- 从按钮启动 Quick / Standard / Full。
- Candidate 编译失败。
- Oracle 编译或运行失败。
- 样例 Wrong Answer。
- 随机阶段找到失败。
- Shrinker 成功与超预算。
- 用户取消。
- 页面刷新后恢复 Run 状态。
- 历史报告重新运行。
- 代码变化后缓存正确失效。

### 15.2 性能测试

建立至少四类固定题目：

1. O(n) 数组题。
2. O(n log n) 排序或二分题。
3. 图论题。
4. 存在可缩减 Wrong Answer 的错误实现。

每类题运行 5～10 次，统计：

- 首次反馈时间。
- 总耗时 P50 / P95。
- 模型请求数。
- 子进程数量。
- 缓存命中率。
- 取消响应时间。
- 找到首个失败的时间。

### 15.3 可靠性回归

性能优化不能改变以下语义：

- 同一 Seed 生成相同用例。
- Candidate 和 Oracle 使用相同输入。
- 输出归一化规则不变。
- Fail-fast 仍保留稳定失败证据。
- Shrink 后必须重新验证失败性质。
- 缓存必须通过所有关键哈希校验。
- Benchmark 结果不能与并发正确性测试混在一起。

## 16. 验收标准

V2 完成需要同时满足：

- 算法实验室存在可点击的“开始算法实验”按钮。
- 使用已有 Candidate、Oracle 和题面时可在 0 次模型调用下完成实验。
- Quick、Standard、Full 三档行为和预算明确。
- 当前阶段和部分结果可见。
- 实验可以取消，刷新页面后可恢复状态。
- 默认在第一个稳定失败后 Fail-fast。
- Candidate、Oracle 和 Shrinker 支持缓存或有界并发。
- 相同配置重复运行不会重新执行无变化阶段。
- 常规 Standard 实验 P95 不超过 120 秒。
- Full Audit P95 不超过 300 秒。
- 可靠性报告证据、Seed、源码哈希和失败反例保持完整。
- 不将私有思维链或敏感环境变量写入事件与报告。

## 17. 推荐优先级

如果开发时间有限，按以下顺序执行：

1. **按钮直接启动 Run，绕过 Agent Loop。**
2. **Quick / Standard / Full 与 Fail-fast。**
3. **只返回报告摘要，避免完整 Case 挤入模型上下文。**
4. **编译和结果缓存。**
5. **有界并发与 Shrinker 优化。**
6. **可选模型辅助生成 Oracle。**

其中第一项预计能带来最大的性能收益，因为当前最主要的问题并不是测试程序本身，而是确定性测试被拆成了多轮模型思考。

## 18. 最终产品叙事

优化后可以这样介绍：

> Code Helper 将“算法求解”和“算法验证”拆成两条专业管线。模型负责理解问题与编写代码，算法实验协调器负责确定性测试、Oracle 对拍、失败缩减和性能报告。用户可以一键启动 Quick、Standard 或 Full 实验，不需要等待模型在每个验证阶段重复思考，因此可靠性能力不会再以十倍级延迟为代价。
