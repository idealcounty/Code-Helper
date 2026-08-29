# Agent Eval 指南

Code Helper 的 Eval 用于回答“这次优化是否破坏了 Agent 契约”，而不是用一次模型输出证明模型智能。评测分为默认的确定性契约层和显式启用的真实模型层。

## 确定性契约层

运行：

```powershell
python -m evals.runner --compare evals/reports/baseline.json
```

`evals/tasks/` 中的十个 JSON 任务分别覆盖项目问答、单文件缺陷、跨文件功能、外部并发修改、审批拒绝、检查点恢复、卡死终止、长输出取消、Session 中断和敏感环境。任务使用固定 fixture 与 `ScriptedModel`，但执行时复用产品的 Runtime、AgentLoop、权限、工具、检查点和事件管线。

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

确定性层要求所有契约通过、完成率不低于 70%、安全拦截率为 100%、需要验证的修改任务新鲜验证率为 100%。与基线比较时，任一核心比率下降或基线任务消失也会失败。CI 在 Python 3.11 上执行该门禁并上传 JSON/Markdown 报告。

仓库基线位于 `evals/reports/baseline.json` 和 `evals/reports/baseline.md`。只有任务契约或评分口径经过审阅后才应更新基线；不能用更新基线掩盖回归。

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

这条命令默认使用确定性模型，只验证开关、契约和报告链路。可用 `--repetitions 3` 做三次成对重复，报告会给出两组均值和指标差值。需要真实 DeepSeek 质量证据时，必须明确执行 `--mode real --allow-paid`；脚本会在同一任务集上先后运行启用与禁用 Repo Map 的隔离实验，并不会把确定性结果表述为模型智能提升。

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

真实层当前只运行标记为 `real_enabled` 的项目问答、单文件缺陷和跨文件功能，其他任务会明确记为 skipped。报告会保存供应商、模型、推理档位、供应商默认 temperature、Token 预算、Prompt 哈希与代码提交。由于模型输出存在波动，真实层不会成为唯一 CI 阻塞条件。

## 已知边界

- 脚本化 Token 是固定的度量样本，只用于发现调用次数或上下文行为回退，不能估算真实账单。
- 确定性耗时受机器影响，因此当前不作为基线阻塞比率。
- `sensitive_environment` 只验证命令环境不继承 API Key；事件与完整工具输出脱敏属于 R7。
- `session_interruption` 验证中断后不自动重放写副作用；完整待审批恢复属于 R2。
