"""Compare explicit project and algorithm Profiles on the same deterministic tasks."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from coding_agent.config import AppConfig

from .runner import run_suite


TASK_IDS = {"cross_file_feature", "algorithm_profile_repair"}


async def run_comparison(
    *, mode: str = "deterministic", real_config: AppConfig | None = None
) -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = {}
    for profile in ("project", "algorithm"):
        report = await run_suite(
            mode=mode,
            real_config=real_config,
            task_ids=TASK_IDS,
            profile_override=profile,
        )
        runs[profile] = {
            "metrics": report["metrics"],
            "tasks": report["tasks"],
        }
    return {
        "schema_version": 1,
        "mode": mode,
        "task_ids": sorted(TASK_IDS),
        "profiles": runs,
        "interpretation": (
            "Deterministic comparison isolates Profile plumbing and safety contracts; "
            "it does not establish real-model quality or statistical superiority."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# TaskProfile 对照基准",
        "",
        "同一组跨文件与算法任务分别显式运行 `project` 和 `algorithm` Profile。",
        "",
        "| Profile | Tasks | Contract | Completion | Verification | Recall@5 | Steps |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile in ("project", "algorithm"):
        metrics = report["profiles"][profile]["metrics"]
        lines.append(
            f"| `{profile}` | {metrics['task_count']} | "
            f"{metrics['contract_pass_rate']:.1%} | {metrics['completion_rate']:.1%} | "
            f"{metrics['verification_rate']:.1%} | {metrics['recall_at_5']:.1%} | "
            f"{metrics['average_steps']} |"
        )
    lines.extend(["", f"> {report['interpretation']}", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("deterministic", "real"), default="deterministic")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results"))
    args = parser.parse_args()
    if args.mode == "real" and not args.allow_paid:
        raise SystemExit("Real Profile comparison requires --allow-paid and consumes API credits")
    config = AppConfig.from_env() if args.mode == "real" else None
    if config is not None and not config.api_key:
        raise SystemExit("Real Profile comparison requires a configured model API key")
    report = asyncio.run(run_comparison(mode=args.mode, real_config=config))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "profile-comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (args.output_dir / "profile-comparison.md").write_text(
        render_markdown(report), encoding="utf-8", newline="\n"
    )
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
