"""Compare the deterministic development workflow with Skills disabled.

The comparison deliberately reuses the normal Eval runner and fixtures.  This
keeps the experiment focused on the workflow layer: both variants receive the
same task, model fixture, workspace and verification command; only the
workflow/Skills context is toggled.

Deterministic mode measures plumbing and guard behavior.  Real mode is
available for an explicitly authorized DeepSeek run and is never started
implicitly by tests or imports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coding_agent.config import AppConfig

from .catalog import load_tasks
from .runner import run_suite


DEFAULT_TASK_IDS = (
    "workflow_add_feature",
    "workflow_bug_fix",
    "workflow_code_review",
)
DELTA_METRICS = (
    "completion_rate",
    "verification_rate",
    "average_steps",
    "average_tokens",
    "average_tool_calls",
    "unrelated_modifications",
)


async def run_comparison(
    *,
    mode: str = "deterministic",
    task_ids: set[str] | None = None,
    real_config: AppConfig | None = None,
    repetitions: int = 1,
    profile_override: str = "project",
) -> dict[str, Any]:
    """Run paired enabled/disabled suites and return an auditable report."""

    if mode not in {"deterministic", "real"}:
        raise ValueError("comparison mode must be deterministic or real")
    if not 1 <= repetitions <= 20:
        raise ValueError("repetitions must be between 1 and 20")
    selected = set(task_ids or DEFAULT_TASK_IDS)
    if not selected:
        raise ValueError("at least one comparison task is required")
    # Validate IDs before running either arm, so a typo cannot produce a
    # misleading one-sided report.
    tasks = load_tasks(selected)
    expected_files = {
        task.id: set((task.expected.get("files") or {}).keys()) for task in tasks
    }

    samples: list[dict[str, Any]] = []
    enabled_reports: list[dict[str, Any]] = []
    disabled_reports: list[dict[str, Any]] = []
    for index in range(1, repetitions + 1):
        enabled = await run_suite(
            mode=mode,
            task_ids=selected,
            real_config=real_config,
            profile_override=profile_override,
            workflow_enabled=True,
        )
        disabled = await run_suite(
            mode=mode,
            task_ids=selected,
            real_config=real_config,
            profile_override=profile_override,
            workflow_enabled=False,
        )
        enabled_reports.append(enabled)
        disabled_reports.append(disabled)
        samples.append(
            {
                "index": index,
                "enabled": _compact_run(enabled, expected_files),
                "disabled": _compact_run(disabled, expected_files),
            }
        )

    enabled_summary = _aggregate(enabled_reports, expected_files)
    disabled_summary = _aggregate(disabled_reports, expected_files)
    delta = {
        name: round(
            _numeric_metric(enabled_summary, name)
            - _numeric_metric(disabled_summary, name),
            4,
        )
        for name in DELTA_METRICS
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": mode,
        "task_ids": [task.id for task in tasks],
        "repetitions": repetitions,
        "runs": {"enabled": enabled_summary, "disabled": disabled_summary},
        "delta": delta,
        "quality_evidence": {
            "status": "real_model_required" if mode == "deterministic" else "real_model_observed",
            "paid": mode == "real",
            "note": (
                "Deterministic fixtures validate the integration and safety contracts; "
                "run the same comparison with an authorized model key before making "
                "claims about model quality."
                if mode == "deterministic"
                else "Results came from an explicitly authorized real-model run and may vary."
            ),
        },
        "interpretation": {
            "positive_delta": "enabled minus disabled; positive completion/verification is better",
            "steps_and_tokens": "lower is generally more efficient, but should be read with completion and verification",
            "unrelated_modifications": "count of changed files outside each task's expected file set",
        },
        "samples": samples,
    }


def _compact_run(
    report: dict[str, Any], expected_files: dict[str, set[str]]
) -> dict[str, Any]:
    metrics = dict(report.get("metrics") or {})
    metrics["unrelated_modifications"] = _unrelated_modifications(
        report.get("tasks") or [], expected_files
    )
    return {
        "mode": report.get("mode"),
        "workflow_enabled": report.get("workflow_enabled", True),
        "metrics": metrics,
        "tasks": report.get("tasks") or [],
    }


def _aggregate(
    reports: list[dict[str, Any]], expected_files: dict[str, set[str]]
) -> dict[str, Any]:
    compact = [_compact_run(report, expected_files) for report in reports]
    if len(compact) == 1:
        return compact[0]
    metric_names = {
        name
        for item in compact
        for name, value in item["metrics"].items()
        if isinstance(value, (int, float))
    }
    metrics = {
        name: round(
            sum(float(item["metrics"].get(name, 0)) for item in compact)
            / len(compact),
            4,
        )
        for name in sorted(metric_names)
    }
    # Preserve nested workflow and token details from the first run while
    # exposing their averaged headline values for repeated experiments.
    workflow_runs = [item["metrics"].get("workflows") or {} for item in compact]
    workflow_keys = {key for item in workflow_runs for key in item}
    workflow_summary: dict[str, Any] = {}
    for key in sorted(workflow_keys):
        values = [item.get(key) for item in workflow_runs]
        if all(isinstance(value, (int, float)) for value in values):
            workflow_summary[key] = round(
                sum(float(value) for value in values) / len(values), 4
            )
        else:
            workflow_summary[key] = values[0]
    if workflow_summary:
        metrics["workflows"] = workflow_summary

    token_runs = [item["metrics"].get("token_usage") or {} for item in compact]
    token_keys = {key for item in token_runs for key in item}
    if token_keys:
        metrics["token_usage"] = {
            key: round(
                sum(float(item.get(key, 0)) for item in token_runs) / len(token_runs),
                2,
            )
            for key in sorted(token_keys)
        }
    source_profiles = compact[0]["metrics"].get("profiles")
    if source_profiles is not None:
        metrics["profiles"] = source_profiles
    metrics["task_count"] = compact[0]["metrics"].get("task_count", 0)
    metrics["unrelated_modifications"] = round(
        sum(float(item["metrics"].get("unrelated_modifications", 0)) for item in compact)
        / len(compact),
        4,
    )
    return {
        "mode": compact[0]["mode"],
        "workflow_enabled": compact[0]["workflow_enabled"],
        "metrics": metrics,
        "tasks": compact[-1]["tasks"],
        "repetitions": len(compact),
    }


def _unrelated_modifications(
    task_results: list[dict[str, Any]], expected_files: dict[str, set[str]]
) -> int:
    count = 0
    for result in task_results:
        task_id = str(result.get("task_id") or "")
        expected = expected_files.get(task_id, set())
        changed = {str(path) for path in result.get("changed_files") or []}
        count += len(changed - expected)
    return count


def _numeric_metric(summary: dict[str, Any], name: str) -> float:
    metrics = summary.get("metrics") or {}
    if name == "average_tokens":
        return float((metrics.get("workflows") or {}).get("average_tokens", 0))
    if name == "average_tool_calls":
        return float((metrics.get("workflows") or {}).get("average_tool_calls", 0))
    if name == "average_steps":
        return float((metrics.get("workflows") or {}).get("average_steps", metrics.get(name, 0)))
    return float(metrics.get(name, 0))


def write_comparison_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "superpowers-comparison.json"
    markdown_path = output_dir / "superpowers-comparison.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown_report(report), encoding="utf-8", newline="\n")
    return json_path, markdown_path


def _markdown_report(report: dict[str, Any]) -> str:
    enabled = report["runs"]["enabled"]["metrics"]
    disabled = report["runs"]["disabled"]["metrics"]
    delta = report["delta"]
    lines = [
        "# Superpowers 启用/禁用对照实验",
        "",
        f"- 模式：`{report['mode']}`",
        f"- 任务：{', '.join(f'`{item}`' for item in report['task_ids'])}",
        f"- 重复次数：{report['repetitions']}",
        "",
        "| 指标 | 启用 Superpowers | 禁用 Superpowers | 差值（启用 - 禁用） |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name in DELTA_METRICS:
        lines.append(
            f"| `{name}` | {_display_metric(enabled, name)} | "
            f"{_display_metric(disabled, name)} | {delta[name]} |"
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "正的完成率/验证率差值表示启用工作流后的契约表现更好；步骤和 Token 通常越低越高效，需结合是否完成和是否验证一起解读。",
            "",
            f"> {report['quality_evidence']['note']}",
            "",
        ]
    )
    return "\n".join(lines)


def _display_metric(metrics: dict[str, Any], name: str) -> str:
    value = _numeric_metric({"metrics": metrics}, name)
    if name.endswith("rate"):
        return f"{value * 100:.1f}%"
    return str(round(value, 2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare Superpowers workflow enabled/disabled.")
    parser.add_argument("--mode", choices=("deterministic", "real"), default="deterministic")
    parser.add_argument("--task", action="append", dest="task_ids")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results/superpowers-comparison"))
    parser.add_argument("--allow-paid", action="store_true", help="Required for real-model runs.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.mode == "real" and not args.allow_paid:
        raise SystemExit("Real comparison requires --allow-paid and consumes API credits")
    real_config = AppConfig.from_env() if args.mode == "real" else None
    if real_config is not None and not real_config.api_key:
        raise SystemExit("Real comparison requires a configured model API key")
    report = asyncio.run(
        run_comparison(
            mode=args.mode,
            task_ids=set(args.task_ids) if args.task_ids else None,
            repetitions=args.repetitions,
            real_config=real_config,
        )
    )
    json_path, markdown_path = write_comparison_report(report, args.output_dir)
    print(_markdown_report(report))
    print(f"Reports: {json_path} | {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
