# Code Helper 最终测试报告（确定性门禁版）

> 报告日期：2026-09-01（Asia/Shanghai）  
> 结论：**Conditional Pass**  
> 说明：本报告记录当前工作区的可复现确定性测试；真实 DeepSeek、干净 Windows 环境和完整浏览器用户旅程仍需按“未执行项”补验。

## 1. 被测版本

| 项目 | 值 |
| --- | --- |
| Git Commit | `e3d2e69963577a1ee875d22a66d2c338c6d926c1` |
| 工作区状态 | 有未提交改动；以下快照绑定到该工作区 |
| 工作区快照 SHA-256 | `b968c94616e832ed9f359cf67ae0752d3f7745b6f4c6e3f7167916118954cb36` |
| 环境 | Windows 11 / Python 3.13 / 16 logical CPUs |
| 模型 | Scripted/Fake Model；未调用付费 DeepSeek |
| 统一 Run ID | `2026-09-01_125804_e3d2e699` |

## 2. 门禁结果

命令：

```powershell
python scripts/run_quality_tests.py --include-evals --include-coverage --include-mutation --output-dir test-results/quality-run-2026-09-01-final39
```

**15/15 通过，0 失败，0 跳过。**

| 层级 | 实测结果 |
| --- | --- |
| 静态检查 | Python 编译、JavaScript 语法、`git diff --check` 全部通过 |
| 单元/集成 | **366 passed**；pytest 261.14s（含 pytest-cov 分支覆盖率） |
| API/WebSocket 契约 | **32/32 checks**；包含状态码、响应形状、路径边界、会话隔离、审批错误契约、实时事件和断线重连历史恢复 |
| 确定性用户旅程 | Ask→Act、审批、验证、归档/恢复、重启历史恢复通过 |
| Agent Eval | 11 个任务；合同、完成、安全、验证率均 **100%** |
| Repo Map/算法/Profile | 检索、算法可靠性和推理 Profile 基准全部通过 |
| 覆盖率 | pytest-cov 行 **86.46%**、分支 **75.00%**；标准库 trace 行基线 **58.06%**；XML/HTML 已生成 |
| 安全审计 | 0 个高危发现；未发现跟踪 `.env` 或密钥模式 |
| 故障注入 | **5/5**；协议错误、429、越界、超时和 Hook 异常均安全收敛 |
| 关键不变量变异 | **5/5 killed**；Mutation Score 100.0% |
| 大上下文 | 252 文件与 80 条历史消息；另有 1,002 文件/200 条历史的大仓库探针；预算、压缩和缓存失效通过 |

## 3. 非功能测试数据

| 测试 | 数据与结果 |
| --- | --- |
| HTTP 负载（最新快照） | `/api/health` 300 请求/25 并发，错误率 0%，吞吐 96.195 req/s，P50 27.550ms，P95 2758.599ms，P99 2777.927ms |
| Agent 并发（最新快照） | 20 会话/5 并发，完成率 100%，事件串扰 0，7.224 sessions/s |
| 60 秒浸泡（历史基线） | 101 轮、505 会话，完成率 100%，事件串扰 0，RSS 峰值约 152.5MB，CPU 54.73s |
| 2 小时浸泡 | 9107 轮、45535 会话，完成率 100%，事件串扰 0，RSS 峰值约 460.2MB，CPU 6561.98s |
| EXE 冷启动（最新快照） | 1806 文件；健康接口约 1489.680ms 返回 200；探针结束后进程清理完成 |

P99 显著高于 P95，最新快照的冷启动和 HTTP 尾延迟也高于历史基线；2 小时浸泡的 RSS 从约 58.8MB 最低值升至约 460.2MB 峰值。当前探针使用本地 TestClient/临时 App，不能据此判定生产内存泄漏；尾延迟、启动时间和资源曲线均作为后续风险记录，而不是隐藏在“全绿”结论中。

## 4. 缺陷与回归

API 契约探针首次执行暴露了检查点接口的变量遮蔽缺陷：`get_checkpoint` 在读取会话前错误引用了同名局部变量，导致 `UnboundLocalError`。修复为使用 `checkpoint_manager` 后，随后又收紧审批字段为严格布尔类型；最终 32 项契约检查、366 个 pytest 和完整质量门禁均重新通过。覆盖率插桩期间又暴露了长输出取消竞态：进程管道未及时关闭时旧实现会丢弃已读取输出，导致完整结果引用缺失；现已保留部分输出并新增回归测试。故障注入探针还验证了流式参数错误会触发一次非流式恢复，而不是直接终止任务。新增 Step 边界、ApprovalBroker、文件系统、Hook 配置、记忆治理、CLI/入口和外部 Hook 边界回归后，5 个关键变异均被测试捕获。final35/36 暴露了 Windows 高负载下长输出取消和并发耗时阈值的测试竞态，分别通过延长子进程寿命、直接断言并发重叠修复；final39 重新通过。修复前失败产物不作为成功证据使用，但根因和修复后的回归结果保留在当前工作区历史中。

## 5. 未执行或需外部环境补验

- 干净 Windows 虚拟机中的 EXE 安装/升级和残留进程验收。
- 文件夹选择器打开中文、空格及多级子目录，并检查 Markdown/代码高亮、复制和设置持久化。
- 浏览器/WebView2 完整多轮对话、审批、取消、刷新恢复和大消息渲染。
- 真实 DeepSeek 小规模重复测试（首 Token、P95、429/超时/取消、费用）。
- pytest-cov 分支/HTML 已执行（行 86.46%、分支 75.00%）；2 小时浸泡已完成且无失败，峰值 RSS 约 460.2MB，仍需用真实长生命周期服务复测资源曲线；项目内置变异探针已通过，但完整 mutmut 仍未安装执行。

## 6. 证据索引

- 统一摘要：`test-results/quality-run-2026-09-01-final39/summary.md`
- 机器清单：`test-results/quality-run-2026-09-01-final39/manifest.json`
- API 契约：`test-results/quality-run-2026-09-01-final39/api-contract/api-contract.md`
- Agent Eval：`test-results/quality-run-2026-09-01-final39/eval/report.md`
- 故障注入：`test-results/quality-run-2026-09-01-final39/fault-injection/fault-injection.md`
- 上下文压力：`test-results/quality-run-2026-09-01-final39/context-stress/context-stress.md`
- HTTP 性能：`test-results/performance-2026-09-01-final39-current/api-load.md`
- 并发：`test-results/agent-concurrency-2026-09-01-final39-current/agent-concurrency.md`
- 浸泡（历史 60 秒基线）：`test-results/soak-2026-09-01-final30-60s/soak.md`
- 2 小时浸泡：`test-results/soak-2026-09-01-2h/soak.md`
- EXE：`test-results/desktop-package-2026-09-01-final39-current/desktop-package.md`
- 变异测试：`test-results/quality-run-2026-09-01-final39/mutation/mutation.md`
- pytest-cov XML/HTML：`test-results/quality-run-2026-09-01-final39/coverage.xml`、`test-results/quality-run-2026-09-01-final39/coverage-html/index.html`
- 浏览器 UI 烟测：`test-results/ui-browser-2026-09-01-final31/ui-browser.md`、`test-results/ui-browser-2026-09-01-final31/ui-browser.json`
- 已知限制：`docs/test-reports/known-limitations.md`
