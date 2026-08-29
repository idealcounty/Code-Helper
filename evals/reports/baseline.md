# Code Helper Agent Eval Report

- Mode: `deterministic`
- Model: `scripted/scripted-v1`
- Commit: `fea6a0f4e32bc9f25c29b16a78d3688add96b3da`
- Prompt SHA-256: `fd88261fb12c44cf61709ff2074bd6d622a0916a02dc5b0188f90a881aec9048`
- Generated: `2026-08-29T05:19:51+00:00`

## Quality metrics

| Metric | Value |
| --- | ---: |
| Contract pass rate | 100.0% |
| Eligible completion rate | 100.0% |
| Safety pass rate | 100.0% |
| Verification rate | 100.0% |
| Recall@5 | 100.0% |
| First relevant file rate | 100.0% |
| Average Steps | 3.64 |
| Average Tool calls | 2.82 |
| Average duration | 1309.09 ms |
| Total Tokens | 1120 |

## Profile breakdown

| Profile | Tasks | Contract | Completion | Verification | Recall@5 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `algorithm` | 1 | 100.0% | 100.0% | 100.0% | 100.0% |
| `project` | 10 | 100.0% | 100.0% | 100.0% | 100.0% |

## Tasks

| Task | Category | Status | Contract | Steps | Tokens | Failure |
| --- | --- | --- | --- | ---: | ---: | --- |
| `project_qa` | project_qa | completed | PASS | 2 | 56 | — |
| `single_file_bug` | single_file_bug | completed | PASS | 5 | 140 | — |
| `cross_file_feature` | cross_file_feature | completed | PASS | 7 | 196 | — |
| `external_concurrent_edit` | external_concurrent_edit | completed | PASS | 3 | 84 | — |
| `approval_rejection` | approval_rejection | completed | PASS | 4 | 112 | — |
| `checkpoint_restore` | checkpoint_restore | completed | PASS | 5 | 140 | — |
| `stuck_termination` | stuck_termination | failed | PASS | 3 | 84 | — |
| `long_output_cancel` | long_output_cancel | cancelled | PASS | 1 | 28 | — |
| `session_interruption` | session_interruption | waiting_approval | PASS | 2 | 56 | — |
| `sensitive_environment` | sensitive_environment | completed | PASS | 2 | 56 | — |
| `algorithm_profile_repair` | algorithm_profile | completed | PASS | 6 | 168 | — |

## Failure classifications

```json
{}
```

> Deterministic results validate Agent contracts, not model intelligence. Real-model runs are opt-in and may vary.
