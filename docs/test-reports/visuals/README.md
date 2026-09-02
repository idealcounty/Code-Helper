# 测试可视化索引

这些 SVG 由 `scripts/generate_test_visuals.py` 从脱敏 JSON/XML 证据自动生成，适合直接嵌入 Markdown、面试 PPT 或投影展示。图表不手工录入指标；重新运行脚本即可从同一批证据得到相同结果。

| 图表 | 面试中说明的重点 |
| --- | --- |
| [test-dashboard.svg](test-dashboard.svg) | 一页总览：门禁、测试数量、覆盖率、并发完成率和 EXE 启动 |
| [quality-gates.svg](quality-gates.svg) | 15 项质量命令的通过/失败状态 |
| [coverage.svg](coverage.svg) | pytest-cov 与标准库 trace 的行/分支覆盖率对比 |
| [latency-percentiles.svg](latency-percentiles.svg) | P50/P95/P99/最大延迟，以及尾延迟风险 |
| [stability-context.svg](stability-context.svg) | 并发、浸泡、Repo Map 和历史上下文预算 |

机器可读输入快照：[visual-data.json](visual-data.json)。

```powershell
python scripts/generate_test_visuals.py --output-dir docs/test-reports/visuals
```
