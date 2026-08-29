"""Run the two deterministic interview demonstrations end to end."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .runner import run_suite


DEMOS = {
    "algorithm": ("algorithm_profile_repair", "algorithm"),
    "project": ("cross_file_feature", "project"),
}


async def run_demos(selected: set[str] | None = None) -> dict[str, Any]:
    names = selected or set(DEMOS)
    demos: dict[str, Any] = {}
    for name in sorted(names):
        task_id, profile = DEMOS[name]
        report = await run_suite(
            task_ids={task_id}, profile_override=profile, retrieval_enabled=True
        )
        demos[name] = {
            "task_id": task_id,
            "profile": profile,
            "metrics": report["metrics"],
            "tasks": report["tasks"],
        }
    return {
        "schema_version": 1,
        "demos": demos,
        "interpretation": (
            "Deterministic demos exercise the same Runtime, permissions, events, "
            "tools and verification pipeline used by the product."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 面试演示运行报告",
        "",
        "| Demo | Task | Profile | Contract | Completion | Verification |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for name, demo in report["demos"].items():
        metrics = demo["metrics"]
        lines.append(
            f"| `{name}` | `{demo['task_id']}` | `{demo['profile']}` | "
            f"{metrics['contract_pass_rate']:.1%} | "
            f"{metrics['completion_rate']:.1%} | "
            f"{metrics['verification_rate']:.1%} |"
        )
    lines.extend(["", f"> {report['interpretation']}", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", choices=sorted(DEMOS), action="append")
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results/demos"))
    args = parser.parse_args()
    report = asyncio.run(run_demos(set(args.demo) if args.demo else None))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "interview-demos.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (args.output_dir / "interview-demos.md").write_text(
        render_markdown(report), encoding="utf-8", newline="\n"
    )
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
