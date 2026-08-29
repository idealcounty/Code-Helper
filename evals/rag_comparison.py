"""Compare project-task quality with Repo Map enabled and disabled."""

from __future__ import annotations

import argparse
import asyncio
import json
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
) -> dict[str, Any]:
    selected = task_ids or TASK_IDS
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
    return {
        "schema_version": 1,
        "mode": mode,
        "task_ids": sorted(selected),
        "runs": {
            "repo_map": _compact(with_retrieval),
            "no_rag": _compact(without_retrieval),
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


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Repo Map A/B 对照报告",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Tasks: {', '.join(f'`{item}`' for item in report['task_ids'])}",
        "",
        "| Run | Contract | Completion | Verification | Recall@5 | First file |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("repo_map", "no_rag"):
        metrics = report["runs"][name]["metrics"]
        lines.append(
            f"| `{name}` | {metrics['contract_pass_rate']:.1%} | "
            f"{metrics['completion_rate']:.1%} | {metrics['verification_rate']:.1%} | "
            f"{metrics['recall_at_5']:.1%} | "
            f"{metrics['first_relevant_file_rate']:.1%} |"
        )
    lines.extend(["", f"> {report['interpretation']}", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("deterministic", "real"), default="deterministic")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results/rag"))
    args = parser.parse_args()
    if args.mode == "real" and not args.allow_paid:
        raise SystemExit("Real RAG comparison requires --allow-paid and consumes API credits")
    config = AppConfig.from_env() if args.mode == "real" else None
    if config is not None and not config.api_key:
        raise SystemExit("Real RAG comparison requires a configured model API key")
    report = asyncio.run(run_comparison(mode=args.mode, real_config=config))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "rag-comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (args.output_dir / "rag-comparison.md").write_text(
        render_markdown(report), encoding="utf-8", newline="\n"
    )
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
