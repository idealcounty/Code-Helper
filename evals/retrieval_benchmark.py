"""Reproducible lexical-vs-dependency Repo Map benchmark."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from coding_agent.repo_map import RepoMapBuilder
from coding_agent.tools.workspace import Workspace

from .catalog import load_tasks
from .types import write_fixture


_DISCRIMINATING_CASE = {
    "id": "dependency_centrality_hidden",
    "query": "workflow entrypoint",
    "gold_files": ("src/core.py",),
    "files": {
        "src/core.py": "def canonicalize(value):\n    return value.strip().lower()\n",
        "src/workflow_entry.py": "from src.core import canonicalize\n\ndef workflow_entry(value):\n    return canonicalize(value)\n",
        **{
            f"src/worker_{index}.py": "from src.core import canonicalize\n\n"
            f"def worker_{index}(value):\n    return canonicalize(value)\n"
            for index in range(4)
        },
        **{
            f"src/noise_{index}.py": f"VALUE_{index} = {index}\n"
            for index in range(8)
        },
    },
}


def run_benchmark() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    tasks = [task for task in load_tasks() if task.gold_files]
    with tempfile.TemporaryDirectory(prefix="code-helper-retrieval-") as temporary:
        root = Path(temporary)
        for task in tasks:
            workspace = root / task.id
            workspace.mkdir()
            write_fixture(workspace, task.fixture_files)
            builder = RepoMapBuilder(Workspace(workspace))
            lexical = builder.build(query=task.task, max_files=5, include_dependency_graph=False)
            graph = builder.build(query=task.task, max_files=5, include_dependency_graph=True)
            rows.append(
                {
                    "task_id": task.id,
                    "gold_files": list(task.gold_files),
                    "lexical": _metrics(lexical["files"], task.gold_files),
                    "dependency_graph": _metrics(graph["files"], task.gold_files),
                }
            )
        case = _DISCRIMINATING_CASE
        workspace = root / case["id"]
        workspace.mkdir()
        write_fixture(workspace, case["files"])
        builder = RepoMapBuilder(Workspace(workspace))
        lexical = builder.build(query=case["query"], max_files=1, include_dependency_graph=False)
        graph = builder.build(query=case["query"], max_files=1, include_dependency_graph=True)
        rows.append(
            {
                "task_id": case["id"],
                "gold_files": list(case["gold_files"]),
                "lexical": _metrics(lexical["files"], case["gold_files"]),
                "dependency_graph": _metrics(graph["files"], case["gold_files"]),
            }
        )
    lexical = _aggregate(rows, "lexical")
    graph = _aggregate(rows, "dependency_graph")
    return {
        "schema_version": 1,
        "task_count": len(rows),
        "metrics": {"lexical": lexical, "dependency_graph": graph},
        "tasks": rows,
    }


def _metrics(files: list[dict[str, Any]], gold: tuple[str, ...]) -> dict[str, Any]:
    ranked = [str(item.get("path") or "") for item in files[:5]]
    gold_set = set(gold)
    hits = [index for index, path in enumerate(ranked, start=1) if path in gold_set]
    recall = len(hits) / len(gold_set) if gold_set else None
    return {
        "top_files": ranked,
        "recall_at_5": round(recall, 4) if recall is not None else None,
        "first_relevant": bool(hits and hits[0] == 1),
        "mrr": round(1 / hits[0], 4) if hits else 0.0,
    }


def _aggregate(rows: list[dict[str, Any]], key: str) -> dict[str, float | int]:
    values = [row[key] for row in rows]
    return {
        "task_count": len(values),
        "recall_at_5": round(sum(float(item["recall_at_5"]) for item in values) / len(values), 4) if values else 0.0,
        "first_relevant_rate": round(sum(bool(item["first_relevant"]) for item in values) / len(values), 4) if values else 0.0,
        "mrr": round(sum(float(item["mrr"]) for item in values) / len(values), 4) if values else 0.0,
    }


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# Repo Map 检索基线",
        "",
        "该报告使用固定 Eval fixtures，对比词法排序与词法 + Python 导入依赖图排序。",
        "",
        "| 策略 | 任务数 | Recall@5 | 首个相关文件 | MRR |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, label in (("lexical", "仅词法"), ("dependency_graph", "词法+依赖图")):
        item = metrics[name]
        lines.append(
            f"| {label} | {item['task_count']} | {item['recall_at_5']:.1%} | {item['first_relevant_rate']:.1%} | {item['mrr']:.3f} |"
        )
    lines.extend(["", "## 任务明细", "", "| 任务 | 词法 Recall@5 | 依赖图 Recall@5 |", "| --- | ---: | ---: |"])
    for row in report["tasks"]:
        lines.append(
            f"| `{row['task_id']}` | {row['lexical']['recall_at_5']:.1%} | {row['dependency_graph']['recall_at_5']:.1%} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results"))
    args = parser.parse_args()
    report = run_benchmark()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "retrieval.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "retrieval.md").write_text(render_markdown(report), encoding="utf-8")
    lexical = report["metrics"]["lexical"]
    graph = report["metrics"]["dependency_graph"]
    if graph["recall_at_5"] < lexical["recall_at_5"] or graph["first_relevant_rate"] < lexical["first_relevant_rate"]:
        return 1
    print(render_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
