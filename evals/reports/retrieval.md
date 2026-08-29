# Repo Map 检索基线

该报告使用固定 Eval fixtures，对比无 RAG 文件顺序、词法排序与词法 + Python 导入依赖图排序。

| 策略 | 任务数 | Recall@5 | 首个相关文件 | MRR |
| --- | ---: | ---: | ---: | ---: |
| 无 RAG | 10 | 100.0% | 90.0% | 0.950 |
| 仅词法 | 10 | 90.0% | 90.0% | 0.900 |
| 词法+依赖图 | 10 | 100.0% | 100.0% | 1.000 |

## 任务明细

| 任务 | 无 RAG Recall@5 | 词法 Recall@5 | 依赖图 Recall@5 |
| --- | ---: | ---: | ---: |
| `project_qa` | 100.0% | 100.0% | 100.0% |
| `single_file_bug` | 100.0% | 100.0% | 100.0% |
| `cross_file_feature` | 100.0% | 100.0% | 100.0% |
| `external_concurrent_edit` | 100.0% | 100.0% | 100.0% |
| `approval_rejection` | 100.0% | 100.0% | 100.0% |
| `checkpoint_restore` | 100.0% | 100.0% | 100.0% |
| `stuck_termination` | 100.0% | 100.0% | 100.0% |
| `session_interruption` | 100.0% | 100.0% | 100.0% |
| `algorithm_profile_repair` | 100.0% | 100.0% | 100.0% |
| `dependency_centrality_hidden` | 100.0% | 0.0% | 100.0% |
