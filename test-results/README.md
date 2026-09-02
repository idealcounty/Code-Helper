# 测试运行产物

本目录保存 `scripts/run_quality_tests.py`、`scripts/api_contract_smoke.py`、
`scripts/performance_smoke.py`、`scripts/agent_concurrency_smoke.py`、
`scripts/soak_test.py`、`scripts/security_audit.py`、
`scripts/e2e_deterministic_smoke.py`、`scripts/coverage_baseline.py`、
`scripts/desktop_package_check.py`、`scripts/fault_injection_smoke.py`、
`scripts/context_stress_smoke.py`、`scripts/mutation_smoke.py` 以及浏览器 UI
烟测生成的本地测试数据。每个运行目录包含
JSON/Markdown 汇总、JUnit、日志、性能数据或发行包检查结果。

单次产物默认不提交到 Git，面试或发布时请将经过脱敏的 `summary.md`、
`security.md`、性能报告和最终结论复制到 `docs/test-reports/`。所有产物都
禁止包含 API Key、完整环境变量和用户私有文件内容。
