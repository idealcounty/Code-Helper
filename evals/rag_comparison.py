"""Compare project-task quality with Repo Map enabled and disabled."""

from __future__ import annotations

import argparse
import asyncio
import json
from statistics import mean
from pathlib import Path
from typing import Any

from coding_agent.config import AppConfig

from .runner import run_suite


TASK_IDS = {"project_qa", "single_file_bug", "cross_file_feature"}


async def run_comparison(
    *,
    mode: str = "deterministic",
    real_config: AppConfig | None = None,
    task_ids: set[str] | None = None,
    repetitions: int = 1,
) -> dict[str, Any]:
    if not 1 <= repetitions <= 20:
        raise ValueError("repetitions must be between 1 and 20")
    selected = task_ids or TASK_IDS
    samples: list[dict[str, Any]] = []
    for index in range(repetitions):
        with_retrieval = await run_suite(
            mode=mode,
            real_config=real_config,
            task_ids=selected,
            profile_override="project",
            retrieval_enabled=True,
        )
        without_retrieval = await run_suite(
            mode=mode,
            real_config=real_config,
            task_ids=selected,
            profile_override="project",
            retrieval_enabled=False,
        )
        samples.append(
            {
                "index": index + 1,
                "repo_map": _compact(with_retrieval),
                "no_rag": _compact(without_retrieval),
            }
        )
    repo_runs = [sample["repo_map"] for sample in samples]
    no_rag_runs = [sample["no_rag"] for sample in samples]
    repo_summary = _aggregate(repo_runs)
    no_rag_summary = _aggregate(no_rag_runs)
    cross_file_delta = _delta(
        repo_summary["cross_file"]["metrics"],
        no_rag_summary["cross_file"]["metrics"],
        keys=("contract_pass_rate", "completion_rate", "verification_rate"),
    )
    return {
        "schema_version": 1,
        "mode": mode,
        "task_ids": sorted(selected),
        "repetitions": repetitions,
        "runs": {
            "repo_map": repo_summary,
            "no_rag": no_rag_summary,
        },
        "samples": samples,
        "delta": _delta(repo_summary["metrics"], no_rag_summary["metrics"]),
        "cross_file_delta": cross_file_delta,
        "cross_file_repetitions": _cross_file_repetition_stats(samples),
        "quality_evidence": {
            "status": "real_model_required" if mode != "real" else "real_model_observed",
            "cross_file_completion_improved": (
                cross_file_delta["completion_rate"] > 0
            ),
        },
        "interpretation": (
            "This is a controlled A/B report on the same task set. Deterministic "
            "runs validate the switch and contracts; only real-model runs can "
            "provide evidence of quality improvement."
        ),
    }


def _compact(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "retrieval_enabled": report["retrieval_enabled"],
        "metrics": report["metrics"],
        "tasks": report["tasks"],
    }


def _aggregate(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if len(reports) == 1:
        report = reports[0]
        report["cross_file"] = _cross_file_summary(report)
        return report
    metric_keys = (
        "contract_pass_rate",
        "completion_rate",
        "safety_pass_rate",
        "verification_rate",
        "recall_at_5",
        "first_relevant_file_rate",
        "average_steps",
        "average_duration_ms",
        "average_tool_calls",
    )
    metrics = {
        key: round(mean(float(item["metrics"].get(key, 0.0)) for item in reports), 4)
        for key in metric_keys
    }
    metrics["task_count"] = sum(int(item["metrics"].get("task_count", 0)) for item in reports)
    metrics["repetitions"] = len(reports)
    tasks = [task for item in reports for task in item.get("tasks", [])]
    aggregate = {
        "retrieval_enabled": reports[0]["retrieval_enabled"],
        "metrics": metrics,
        "tasks": tasks,
    }
    aggregate["cross_file"] = _cross_file_summary(aggregate)
    return aggregate


def _cross_file_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Summarize only cross-file project tasks for the roadmap gate."""
    tasks = [
        task
        for task in report.get("tasks", [])
        if str(task.get("category") or "") == "cross_file_feature"
    ]
    eligible = [task for task in tasks if task.get("completion_eligible")]
    completed = [
        task
        for task in eligible
        if task.get("contract_passed") and task.get("status") == "completed"
    ]
    return {
        "metrics": {
            "task_count": len(tasks),
            "contract_pass_rate": _task_rate(tasks, "contract_passed"),
            "completion_rate": len(completed) / len(eligible) if eligible else 0.0,
            "verification_rate": _task_rate(
                [task for task in tasks if task.get("verification_required")],
                "verification_fresh",
            ),
        },
        "task_ids": [str(task.get("task_id") or "") for task in tasks],
    }


def _cross_file_repetition_stats(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize paired cross-file deltas across repetitions."""
    deltas: list[float] = []
    for sample in samples:
        repo = _cross_file_summary(sample["repo_map"])["metrics"]["completion_rate"]
        no_rag = _cross_file_summary(sample["no_rag"])["metrics"]["completion_rate"]
        deltas.append(round(float(repo) - float(no_rag), 4))
    return {
        "count": len(deltas),
        "positive_count": sum(delta > 0 for delta in deltas),
        "non_negative_count": sum(delta >= 0 for delta in deltas),
        "mean_delta": round(sum(deltas) / len(deltas), 4) if deltas else 0.0,
        "deltas": deltas,
    }


def _task_rate(tasks: list[dict[str, Any]], field: str) -> float:
    if not tasks:
        return 0.0
    return sum(bool(task.get(field)) for task in tasks) / len(tasks)


def _delta(
    repo_metrics: dict[str, Any],
    no_rag_metrics: dict[str, Any],
    *,
    keys: tuple[str, ...] = (
        "contract_pass_rate",
        "completion_rate",
        "safety_pass_rate",
        "verification_rate",
        "recall_at_5",
        "first_relevant_file_rate",
    ),
):
    return {
        key: round(float(repo_metrics.get(key, 0.0)) - float(no_rag_metrics.get(key, 0.0)), 4)
        for key in keys
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Repo Map A/B 对照报告",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Repetitions: `{report['repetitions']}` (paired runs)",
        f"- Tasks: {', '.join(f'`{item}`' for item in report['task_ids'])}",
        "",
        "| Run | Contract | Completion | Cross-file completion | Verification | Recall@5 | First file |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("repo_map", "no_rag"):
        metrics = report["runs"][name]["metrics"]
        lines.append(
            f"| `{name}` | {metrics['contract_pass_rate']:.1%} | "
            f"{metrics['completion_rate']:.1%} | "
            f"{report['runs'][name]['cross_file']['metrics']['completion_rate']:.1%} | "
            f"{metrics['verification_rate']:.1%} | "
            f"{metrics['recall_at_5']:.1%} | "
            f"{metrics['first_relevant_file_rate']:.1%} |"
        )
    lines.extend(
        [
            "",
            "Cross-file delta (Repo Map - no RAG): "
            + ", ".join(
                f"{key}={value:+.1%}"
                for key, value in report["cross_file_delta"].items()
            ),
            f"> {report['interpretation']}",
            "Paired repetitions: "
            f"{report['cross_file_repetitions']['positive_count']}/"
            f"{report['cross_file_repetitions']['count']} positive, "
            f"mean completion delta={report['cross_file_repetitions']['mean_delta']:+.1%}",
            "",
        ]
    )
    lines.insert(-1, "Delta (Repo Map - no RAG): " + ", ".join(
        f"{key}={value:+.1%}" for key, value in report["delta"].items()
    ))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("deterministic", "real"), default="deterministic")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--repetitions", type=int, default=1, help="Paired A/B repetitions (1-20).")
    parser.add_argument(
        "--require-cross-file-improvement",
        action="store_true",
        help="In real mode, return failure unless cross-file completion improves.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results/rag"))
    args = parser.parse_args()
    if args.mode == "real" and not args.allow_paid:
        raise SystemExit("Real RAG comparison requires --allow-paid and consumes API credits")
    config = AppConfig.from_env() if args.mode == "real" else None
    if config is not None and not config.api_key:
        raise SystemExit("Real RAG comparison requires a configured model API key")
    report = asyncio.run(
        run_comparison(mode=args.mode, real_config=config, repetitions=args.repetitions)
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "rag-comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (args.output_dir / "rag-comparison.md").write_text(
        render_markdown(report), encoding="utf-8", newline="\n"
    )
    print(render_markdown(report))
    if args.require_cross_file_improvement:
        if args.mode != "real":
            raise SystemExit("--require-cross-file-improvement requires --mode real")
        if not report["quality_evidence"]["cross_file_completion_improved"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
