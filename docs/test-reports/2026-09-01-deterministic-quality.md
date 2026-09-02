# Code Helper 确定性质量测试报告

> 测试日期：2026-09-01（Asia/Shanghai）
> 目的：为面试和发布前验收提供可复现、可审计的测试数据。
> 范围：本地确定性测试、API/WebSocket 契约、只读 HTTP 探针、并发会话、短时浸泡、EXE 包结构/冷启动和安全审计。
> 说明：本报告不包含 API Key；原始日志和机器可读结果保存在被 Git 忽略的 `test-results/` 目录。

## 1. 运行环境与版本

| 项目 | 值 |
| --- | --- |
| 统一门禁 Run ID | `2026-09-01_125804_e3d2e699` |
| 操作系统 | Windows 11（`Windows-11-10.0.26200-SP0`） |
| Python | 3.13.0 |
| CPU 逻辑核 | 16 |
| Git Commit | `e3d2e69963577a1ee875d22a66d2c338c6d926c1` |
| 工作区状态 | 有未提交改动；报告记录的是该工作区快照 |
| 工作区快照 SHA-256 | `b968c94616e832ed9f359cf67ae0752d3f7745b6f4c6e3f7167916118954cb36` |
| 模型 | 默认质量门禁使用 Scripted/Fake Model；未调用付费 DeepSeek |

## 2. 统一质量门禁

命令：

```powershell
python scripts/run_quality_tests.py --include-evals --include-coverage --include-mutation --output-dir test-results/quality-run-2026-09-01-final39
```

结果：**15/15 通过，0 失败，0 跳过**。

| 检查 | 结果 | 耗时/数据 |
| --- | --- | ---: |
| Python 编译（`src evals scripts`） | PASS | 93ms |
| JavaScript 语法 | PASS | 60ms |
| `git diff --check` | PASS | 60ms |
| 密钥与隐私审计 | PASS | 0 个高危发现；1.269s |
| 故障注入探针 | PASS | **5/5 场景**；0.736s |
| 上下文压力探针 | PASS | 252 文件、80 条历史；3.038s |
| API/WebSocket 契约烟测 | PASS | **32/32 checks，1.458s** |
| pytest | PASS | **366 passed，261.14s**（含 pytest-cov 分支覆盖率） |
| 确定性 E2E | PASS | 1.693s |
| 确定性 Agent Eval | PASS | 20.850s |
| Repo Map 检索基准 | PASS | 535ms |
| 算法基准 | PASS | 2.721s |
| 推理 Profile 对比 | PASS | 6.015s |
| 标准库行覆盖率基线 | PASS | 154.958s；58.06% |
| pytest-cov 分支/HTML 覆盖率 | PASS | 行 86.46%，分支 75.00%；已生成 XML/HTML |
| 关键不变量变异测试 | PASS | **5/5 killed；100.0%**；13.563s |

原始汇总：`test-results/quality-run-2026-09-01-final39/summary.md`。
 pytest 明细：`test-results/quality-run-2026-09-01-final39/junit.xml`。

## 3. API 与 WebSocket 契约

契约探针使用临时工作区、FastAPI TestClient 和 ScriptedModel，不调用 DeepSeek，也不把响应正文写入报告。
共验证 **32/32** 项：健康与设置、会话创建/详情/列表/隔离、嵌套文件与读取、工作区边界、模式/推理/审批切换、非法审批载荷与无待审批状态、智能/上下文/记忆/轨迹/回放/报告/权限/检查点/算法接口、缺失资源与非法请求状态码，以及 WebSocket 历史、实时事件、断线重连历史恢复和事件序列。

原始报告：`test-results/quality-run-2026-09-01-final39/api-contract/api-contract.md`。

### 3.1 故障注入与大上下文

- 故障注入：5/5 场景通过，覆盖流式 Tool Call 参数错误后的非流式恢复、HTTP 429、工作区越界、命令超时和 Post-Tool Hook 异常。
- 上下文压力：统一门禁使用 252 个合成文件和 80 条历史消息；独立大仓库探针使用 1,002 个文件和 200 条历史消息，最新快照 Repo Map 冷启动 8.993s、热启动 2.352s，预算/历史摘要/文件摘要缓存失效均通过。
- 原始报告：`test-results/quality-run-2026-09-01-final39/fault-injection/fault-injection.md`、`test-results/quality-run-2026-09-01-final39/context-stress/context-stress.md`、`test-results/context-stress-2026-09-01-final39-1000-current/context-stress.md`。

## 4. Agent 能力 Eval

固定任务集共 11 个，使用 `scripted/scripted-v1`，用于验证 Agent Loop、工具协议、权限和验证合同，而不是评价外部模型的开放域智能。

| 指标 | 实测 |
| --- | ---: |
| 合同通过率 | 100.0% |
| 可完成任务率 | 100.0% |
| 安全通过率 | 100.0% |
| 验证率 | 100.0% |
| Recall@5 | 100.0% |
| 首个相关文件命中率 | 100.0% |
| 平均 Step 数 | 3.64 |
| 平均 Tool Call 数 | 2.82 |
| 平均耗时 | 1443.18ms |
| 总 Tokens | 1120 |

覆盖场景包括项目问答、单文件 Bug、跨文件功能、外部并发编辑、审批拒绝、检查点恢复、卡死终止、长输出取消、会话中断、敏感环境变量和算法 Profile 修复。原始报告：`test-results/quality-run-2026-09-01-final39/eval/report.md`。

## 5. HTTP 只读性能探针

命令（需要本地 Web 服务已启动）：

```powershell
python scripts/performance_smoke.py --url http://127.0.0.1:8765 --path /api/health --requests 300 --concurrency 25 --output-dir test-results/performance-2026-09-01-final39-current
```

| 指标 | 实测 |
| --- | ---: |
| 请求数 / 并发 | 300 / 25 |
| 成功 / 失败 | 300 / 0 |
| 错误率 | 0.00% |
| 吞吐 | 96.195 req/s |
| P50 | 27.550ms |
| P95 | 2758.599ms |
| P99 | 2777.927ms |
| Max | 2787.470ms |

P99 明显高于 P95，说明仍需在正式发布前分析偶发抖动；本次探针只访问健康检查接口，不代表模型任务吞吐。

## 6. Agent 并发与稳定性

### 6.1 隔离会话并发

命令：

```powershell
python scripts/agent_concurrency_smoke.py --sessions 20 --concurrency 5 --output-dir test-results/agent-concurrency-2026-09-01-final39-current
```

- 20 个隔离会话，5 并发。
- 完成 20，失败 0，完成率 **100%**。
- 事件串扰 **0**；每个会话均产生 21 个事件。
- 总耗时 2768.538ms，吞吐 7.224 sessions/s。
- 使用真实 Runtime + ScriptedModel，不产生外部 API 费用。

### 6.2 短时浸泡

以下 60 秒结果是 final30 的历史基线；当前快照的 2 小时浸泡已完成并追加最终报告。

命令：

```powershell
python scripts/soak_test.py --duration-seconds 60 --round-sessions 5 --concurrency 3 --output-dir test-results/soak-2026-09-01-final30-60s
```

- 实际运行 60.149 秒，共 101 轮、505 个会话。
- 失败 0，完成率 **100%**，事件串扰 0。
- Windows 原生进程 API 记录 RSS 峰值 **152,469,504 bytes（约 152.5MB）**，最低 RSS **61,419,520 bytes（约 61.4MB）**，CPU 时间 **54.73 秒**。

这是一轮中时烟测，不能替代 2 小时浸泡；RSS 从约 61.4MB 上升到约 152.5MB，说明长时资源曲线需要重点关注，不能据此直接判定泄漏。完整 2 小时结果见下方独立报告。

2 小时最终结果：7200.111 秒、9107 轮、45535 个会话，失败 0，完成率 100%，事件串扰 0；RSS 最低 61,616,128 bytes、峰值 482,582,528 bytes（约 460.2MB），CPU 时间 6561.984375 秒。该探针使用临时 App/TestClient，峰值不能直接等同于生产泄漏，但已作为资源风险保留。原始报告：`test-results/soak-2026-09-01-2h/soak.md`。

## 7. 安全与发行包检查

### 7.1 安全审计

扫描 Git 跟踪文件、`.env` 跟踪状态、示例密钥占位符、日志/路径中的敏感信息。本轮结果：**0 个高危发现，PASS**。审计结果：`test-results/quality-run-2026-09-01-final39/security/`。

### 7.2 Windows EXE 包结构

检查 `dist/code-helper` 的 PyInstaller onedir 结构：

- 结论：**PASS**。
- 文件数：1806。
- EXE 大小：14,827,092 bytes（约 14.8MB）。
- EXE SHA-256：`8e981cf6155cf83742735646ffc95f85b5b5b4ba595ffd2b1b32f90083a2572c`。
- 已确认 EXE、`.env.example`、`_internal/coding_agent` 资源存在。
- 自动冷启动探针：**PASS**；约 `1489.680ms` 后 `/api/health` 返回 HTTP `200`，端口 `57645`，探针结束后进程已清理。

原始报告：`test-results/desktop-package-2026-09-01-final39-current/desktop-package.md`。

## 8. 确定性用户旅程 E2E

命令：

```powershell
python scripts/e2e_deterministic_smoke.py --timeout 10 --output-dir test-results/quality-run-2026-09-01-final39/e2e
```

结果：**PASS**，耗时 749.526ms，模型调用 4 次。使用临时工作区和 ScriptedModel，
不调用 DeepSeek、不写入用户工作区。已验证：

- 创建工作区会话与 Ask 对话完成。
- 同一会话从 Ask 切换到 Act。
- 文件写入审批、验证命令审批及最终完成状态。
- 生命周期事件记录（81 个事件）。
- 会话归档、恢复，以及关闭应用后的历史重新加载。

原始报告：`test-results/quality-run-2026-09-01-final39/e2e/e2e.md`。

## 9. 覆盖率基线

本轮同时运行了标准库 `trace` 基线和已安装的 `pytest-cov` 分支覆盖率；两者都运行同一套 pytest。标准库结果用于无可选依赖环境，pytest-cov 结果用于 CI/开发环境的 XML、HTML 和分支分析：

```powershell
python scripts/coverage_baseline.py --output-dir test-results/quality-run-2026-09-01-final39/coverage-baseline
```

- 标准库 `trace`：**366 passed**；耗时约 147.78 秒；可执行行 12,751，覆盖行 7,403，行覆盖率 **58.06%**。
- `pytest-cov`：**366 passed**；可执行行 8,444，覆盖行 7,301，行覆盖率 **86.46%**；分支 2,784 条、覆盖 2,088 条，分支覆盖率 **75.00%**。
- 已生成 `coverage.xml` 和 `coverage-html/index.html`，可直接上传 CI artifact 或在浏览器查看。
- 关键核心模块（Agent Loop、权限、上下文、Repo Map、验证器）多数已达到约 80% 以上；Web UI、协调器和入口适配层仍有较多未覆盖路径。
- 当前行覆盖率超过 85% 目标，分支覆盖率达到 75% 目标；新增 CLI、入口适配、Web 辅助函数、取消、脱敏、Skills、Git 和回放边界测试后，覆盖率门禁通过。Web/桌面和算法协调器仍保留可继续提升的路径，不通过调整统计范围掩盖缺口。

原始报告：`test-results/quality-run-2026-09-01-final39/coverage-baseline/coverage-baseline.md`，机器可读数据：
`test-results/quality-run-2026-09-01-final39/coverage-baseline/coverage-baseline.json`；pytest-cov XML/HTML：
`test-results/quality-run-2026-09-01-final39/coverage.xml`、`test-results/quality-run-2026-09-01-final39/coverage-html/index.html`。

## 10. 浏览器 UI 烟测

本轮使用本地浏览器自动化访问 `http://127.0.0.1:8765/`，未发送真实模型消息，也未修改用户工作区文件。

- 成功恢复 `examples/demo_project`，工作区名称和会话列表正常显示。
- 顶部模式、推理档位和任务类型控件均可交互并正确更新 DOM 状态。
- 审批策略 `ask ↔ auto` 可切换；`full` 按设计出现原生二次确认，本轮未自动接受。
- 会话归档/恢复：对话数量从 `9/0` 变为 `8/1`，恢复后回到 `9/0`。
- 对话、轨迹、计划、智能四个标签均可切换；输入框从 42px 增长并封顶 150px。
- 文件浏览器、Markdown 预览、Python 代码高亮和刷新恢复均通过。

这是启动、关键控件、文件预览和状态恢复烟测，不替代完整多轮对话、审批执行、取消和 WebView2 E2E。原始记录：`test-results/ui-browser-2026-09-01-final31/ui-browser.md`、`test-results/ui-browser-2026-09-01-final31/ui-browser.json`。620px 窄屏无横向溢出的证据仍保留在 `test-results/ui-browser-2026-09-01-final22/ui-browser.md`。

## 11. 关键不变量变异测试

统一门禁通过 `scripts/mutation_smoke.py` 在临时副本中注入 5 个关键变异，验证回归测试能够捕获权限边界、工作区越界、Step/Token 预算边界和 Tool Result 配对错误。结果为 **5/5 mutations killed，Mutation Score 100.0%**。原始报告：`test-results/quality-run-2026-09-01-final39/mutation/mutation.md`。

## 12. 尚未由本机自动化替代的验收项

以下项目不能用本轮确定性脚本冒充完成，发布前应补充结果并把报告中的 `[ ]` 改为 `[x]`：

- [ ] 干净 Windows 环境冷启动 EXE，并确认无残留进程。
- [ ] 用文件夹选择器打开含中文、空格和多级子目录的工作区。
- [ ] 浏览器/桌面 WebView2 端到端流程：多轮对话、审批、取消、刷新恢复、会话切换、Markdown/代码高亮和复制。
- [ ] 真实 DeepSeek 小规模重复测试：记录模型、首 Token、P95、429/超时/取消和费用；不得把供应商延迟当作本项目性能。
- [x] 2 小时浸泡测试，并记录 CPU、RSS、错误率和事件积压（7200.111 秒、45,535 会话、0 失败、事件串扰 0；RSS 峰值约 460.2MB）。
- [x] 达到覆盖率目标：pytest-cov 已生成行 86.46% / 分支 75.00% 的 XML/HTML。
- [x] 关键不变量变异目标：内置变异探针 5/5 killed，Mutation Score 100.0%；完整 mutmut 仍未安装执行。

已知风险的复现步骤和关闭规则见：`docs/test-reports/known-limitations.md`。

## 13. 复现与证据规则

1. 在目标提交上运行统一质量门禁，保留 `manifest.json`、JUnit、日志和子报告。
2. 性能、并发和浸泡测试使用单独带日期的目录，避免覆盖历史结果。
3. 报告只提交脱敏摘要；`test-results/` 原始产物默认本地保存，CI 通过构建产物上传。
4. 任何失败都保留失败日志、最小反例和修复后的新运行 ID，不删除失败证据。
5. 真实 API 测试前确认 Key 不会进入命令行、日志、截图或 Git 历史。
