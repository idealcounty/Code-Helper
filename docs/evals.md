# Agent Eval 指南

Code Helper 的 Eval 用于回答“这次优化是否破坏了 Agent 契约”，而不是用一次模型输出证明模型智能。评测分为默认的确定性契约层和显式启用的真实模型层。

## 确定性契约层

运行：

```powershell
python -m evals.runner --compare evals/reports/baseline.json
```

`evals/tasks/` 中的 14 个 JSON 任务覆盖项目问答、单文件缺陷、跨文件功能、外部并发修改、审批拒绝、检查点恢复、卡死终止、长输出取消、Session 中断、敏感环境，以及 `add-feature`、`bug-fix`、`code-review` 三种轻量开发工作流。任务使用固定 fixture 与 `ScriptedModel`，但执行时复用产品的 Runtime、AgentLoop、权限、工具、检查点和事件管线。

默认报告写入 `.eval-results/report.json` 与 `.eval-results/report.md`。可用 `--task TASK_ID` 重复选择任务，用 `--output-dir` 改变目录，用 `--report-name` 设置安全的文件名。例如：

```powershell
python -m evals.runner --task single_file_bug --output-dir .eval-results/single
```

## 指标和门禁

报告记录：

- 契约通过率与可完成任务完成率。
- 安全用例拦截率与修改后新鲜验证率。
- 标注任务的 Recall@5 和首次相关文件命中率。
- 平均 Step、工具调用、耗时、Token 与失败分类。
- 代码提交、Python/平台、系统 Prompt 哈希和模型配置。
- 工作流任务还记录 Skill 加载、工作流选择/阶段变更、计划与验收标准、修改后验证新鲜度，以及 Code Review 的只读约束。

确定性层要求所有契约通过、完成率不低于 70%、安全拦截率为 100%、需要验证的修改任务新鲜验证率为 100%。与基线比较时，任一核心比率下降或基线任务消失也会失败。CI 在 Python 3.11 上执行该门禁并上传 JSON/Markdown 报告。

### Superpowers Lite 验证快照

2026-09-01 的完整质量流水线运行 `test-results/quality-run-superpowers-lite-final8`（Run ID：`2026-09-01_172229_e3d2e699`）：16 个检查全部通过（0 失败、0 跳过），全量 pytest 为 397 passed；14 个确定性 Eval 的契约、完成、安全和验证指标均为 100%，三个工作流 Eval（Add Feature、Bug Fix、Code Review）的 Skill 加载、计划、只读和恢复阶段指标均为 100%。这组数据是一次可复现的本地验证快照，不等同于真实模型质量承诺。新增的 `superpowers-comparison` 检查同时验证了启用/禁用工作流对照报告链路。

仓库基线位于 `evals/reports/baseline.json` 和 `evals/reports/baseline.md`。新增工作流任务可用 `python -m evals.runner --task workflow_add_feature --task workflow_bug_fix --task workflow_code_review` 单独复核；只有任务契约或评分口径经过审阅后才应更新基线，不能用更新基线掩盖回归。

## 真实模型层

真实模型会消耗 API 额度，因此必须显式确认：

```powershell
python -m evals.runner `
  --mode real `
  --allow-paid `
  --output-dir .eval-results/real
```

如需费用估算，可同时传入 `--input-price-per-million` 和 `--output-price-per-million`。价格由运行者提供，报告不会假设供应商当前价格。

### Repo Map A/B 对照

项目模式可以对同一组任务执行 Repo Map 开关对照，报告会分别保存两次运行的指标：

```powershell
python -m evals.rag_comparison --output-dir .eval-results/rag
```

这条命令默认使用确定性模型，只验证开关、契约和报告链路。可用 `--repetitions 3` 做三次成对重复，报告会给出两组均值和指标差值。`--require-cross-file-improvement` 质量门禁要求真实模式至少两次成对重复、每次跨文件完成率不低于无 RAG 基线且平均提升为正。需要真实 DeepSeek 质量证据时，必须明确执行 `--mode real --allow-paid`；脚本会在同一任务集上先后运行启用与禁用 Repo Map 的隔离实验，并不会把确定性结果表述为模型智能提升。

### Superpowers Lite 启用/禁用对照

可用同一组工作流任务比较是否加载 Superpowers Lite。脚本复用正常的
Agent Loop、权限、工具和验证管线，只切换工作流 Skill 是否可见：

```powershell
python -m evals.superpowers_comparison `
  --mode deterministic `
  --output-dir .eval-results/superpowers-comparison
```

报告会并列记录完成率、验证率、平均 Step、平均 Token、工具调用数和无关修改数，
并给出“启用 - 禁用”的差值。确定性结果只证明集成契约和安全门禁；要研究真实
模型是否因流程约束受益，必须在明确授权后执行：

当前确定性快照中，两组完成率和验证率均为 100%，无关修改数均为 0；启用组平均
5.67 Step / 158.67 Token，禁用组平均 4.67 Step / 130.67 Token。这些 Token 是
固定脚本模型的度量样本，不代表真实供应商账单或模型智能提升。

`--repetitions 3` 的本地复验报告位于
`test-results/superpowers-comparison-repetitions3/superpowers-comparison.md`，三次运行
得到相同均值。

```powershell
python -m evals.superpowers_comparison `
  --mode real `
  --allow-paid `
  --repetitions 3 `
  --output-dir .eval-results/superpowers-comparison-real
```

真实运行会消耗 API 额度，且结果受模型随机性、供应商限流和工作区环境影响，不能
在没有报告的情况下宣称 Superpowers Lite 提升了模型能力。

2026-09-01 已在获得明确授权后完成一次真实 DeepSeek 对照（3 次重复），机器可读报告
为 `test-results/superpowers-comparison-real/superpowers-comparison.json`，摘要为
`test-results/superpowers-comparison-real/superpowers-comparison.md`。聚合结果如下：

| 指标 | 启用 Superpowers | 禁用 Superpowers | 启用 - 禁用 |
| --- | ---: | ---: | ---: |
| completion_rate | 22.22% | 44.45% | -22.23 个百分点 |
| verification_rate | 0% | 33.33% | -33.33 个百分点 |
| average_steps | 4.553 | 4.443 | +0.110 |
| average_tokens | 20,453 | 17,983 | +2,470 |
| average_tool_calls | 6.33 | 5.55 | +0.78 |
| unrelated_modifications | 0 | 0 | 0 |

这组结果的结论应保持克制：真实模型确实执行并记录了工作流事件、Skill 加载、审批和
验证链路，但在当前三项小型夹具与三次重复中，启用组的完成率和验证率反而较低，说明
工作流提示、运行预算或验证闭环仍有优化空间。它是可复现的观测样本，不是统计显著性
结论，也不应替代后续更大任务集和更多重复次数的评估。

## 面试演示

两个纵向演示也可以一条命令重复运行并保存报告：

```powershell
python -m evals.interview_demos --output-dir .eval-results/demos
```

其中 `algorithm` 演示固定种子 Judge 的缺陷发现与修复，并可通过只读 `analyze_complexity` 工具展示循环嵌套/递归的复杂度估计；`project` 演示跨文件修改和验证。两者都复用产品 Runtime、权限、事件、工具和验证管线。

有预算时可显式切换为 DeepSeek：

```powershell
python -m evals.rag_comparison --mode real --allow-paid --output-dir .eval-results/rag-real
python -m evals.profile_comparison --mode real --allow-paid --output-dir .eval-results/profile-real
python -m evals.interview_demos --mode real --allow-paid --output-dir .eval-results/demos-real
```

三条命令都要求已配置 API Key，并将模型、提交、Profile/检索开关和任务指标写入报告；不带 `--allow-paid` 时不会发起真实请求。

真实层当前运行标记为 `real_enabled` 的项目问答、缺陷、跨文件功能和三种工作流任务，其他任务会明确记为 skipped。报告会保存供应商、模型、推理档位、供应商默认 temperature、Token 预算、Prompt 哈希与代码提交。由于模型输出存在波动，真实层不会成为唯一 CI 阻塞条件。

## 已知边界

- 脚本化 Token 是固定的度量样本，只用于发现调用次数或上下文行为回退，不能估算真实账单。
- 确定性耗时受机器影响，因此当前不作为基线阻塞比率。
- `sensitive_environment` 只验证命令环境不继承 API Key；事件与完整工具输出脱敏属于 R7。
- `session_interruption` 验证中断后不自动重放写副作用；完整待审批恢复属于 R2。
- 工作流仍是单 Agent 的轻量门禁：没有引入多 Agent、任务 DAG 或第二套执行循环；真实模型层仍需显式付费开关，且结果可能受模型波动影响。
