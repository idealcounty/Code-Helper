"""Exercise Repo Map, context compaction and file-summary caching locally.

The probe creates a synthetic repository in a temporary directory.  It never
touches a user workspace or calls a model.  The output is intentionally a
bounded summary so it can be committed as interview evidence without leaking
the temporary absolute path or file contents.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.evidence_metadata import collect_metadata
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from evidence_metadata import collect_metadata

from coding_agent.context import ContextManager, _messages_chars
from coding_agent.repo_map import RepoMapBuilder
from coding_agent.session import AgentState
from coding_agent.tools import Workspace


def _write_fixture(root: Path, file_count: int) -> None:
    """Create a small but structurally rich source tree."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    for index in range(file_count):
        path = root / "src" / f"module_{index:05d}.py"
        dependency = f"module_{(index - 1) % max(1, file_count):05d}"
        path.write_text(
            f"from .{dependency} import helper_{(index - 1) % max(1, file_count)}\n\n"
            f"def helper_{index}(value: int) -> int:\n"
            f"    return value + {index}\n\n"
            f"class Service{index}:\n"
            "    def run(self, value: int) -> int:\n"
            f"        return helper_{index}(value)\n",
            encoding="utf-8",
        )
    (root / "src" / "entry.py").write_text(
        "from .module_00000 import helper_0\n\n"
        "def main() -> int:\n"
        "    return helper_0(0)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_entry.py").write_text(
        "def test_entry():\n    assert True\n",
        encoding="utf-8",
    )


def _history(message_count: int, message_chars: int) -> list[dict[str, Any]]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message-{index} " + ("x" * max(0, message_chars - 12)),
        }
        for index in range(message_count)
    ]


def run_probe(
    *,
    file_count: int = 1_000,
    message_count: int = 200,
    message_chars: int = 1_000,
    repo_map_chars: int = 12_000,
    context_chars: int = 8_000,
) -> dict[str, Any]:
    if min(file_count, message_count, message_chars, repo_map_chars, context_chars) < 1:
        raise ValueError("fixture and budget values must be positive")
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="code-helper-context-") as directory:
        root = Path(directory)
        _write_fixture(root, file_count)
        workspace = Workspace(root)
        builder = RepoMapBuilder(workspace)

        cold_started = time.perf_counter()
        cold = builder.build(
            query="module service helper",
            max_files=40,
            max_chars=repo_map_chars,
        )
        cold_ms = round((time.perf_counter() - cold_started) * 1000, 3)

        warm_started = time.perf_counter()
        warm = builder.build(
            query="module service helper",
            max_files=40,
            max_chars=repo_map_chars,
        )
        warm_ms = round((time.perf_counter() - warm_started) * 1000, 3)

        summary_path = root / "src" / "module_00000.py"
        first_summary = workspace.file_summary(summary_path)
        second_summary = workspace.file_summary(summary_path)
        summary_cache_hit = first_summary["sha256"] == second_summary["sha256"]
        summary_path.write_text(
            summary_path.read_text(encoding="utf-8") + "\n# external edit\n",
            encoding="utf-8",
        )
        changed_summary = workspace.file_summary(summary_path)
        summary_cache_invalidated = changed_summary["sha256"] != first_summary["sha256"]

        state = AgentState.create(session_id="context-stress", task_profile="project")
        state.messages = _history(message_count, message_chars)
        context = ContextManager(
            workspace=workspace,
            max_messages=48,
            max_message_chars=2_000,
            max_context_chars=context_chars,
            max_repo_map_chars=repo_map_chars,
        ).build(state, [])
        # ``max_context_chars`` bounds the retained history (including the
        # generated summary), while ``estimated_chars`` also includes the
        # system prompt, Repo Map and tool schemas.  Report both explicitly
        # so a larger full request is not mistaken for a budget violation.
        bounded_history_chars = _messages_chars(context.messages[1:])
        summary_meta = context.context_summary_meta
        result = {
            "schema_version": 1,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "fixture": {
                "source_files": file_count + 2,
                "history_messages": message_count,
                "message_chars": message_chars,
            },
            "repo_map": {
                "cold_ms": cold_ms,
                "warm_ms": warm_ms,
                "files_seen": cold["totals"]["files_seen"],
                "selected": len(cold["files"]),
                "selected_chars": cold["selected_chars"],
                "budget_chars": repo_map_chars,
                "truncated": bool(cold["truncated"]),
                "cold_cache_misses": cold["totals"]["summary_cache_misses"],
                "warm_cache_hits": warm["totals"]["summary_cache_hits"],
            },
            "file_summary": {
                "cache_hit": summary_cache_hit,
                "invalidated_after_edit": summary_cache_invalidated,
            },
            "context": {
                "history_chars_after_compaction": bounded_history_chars,
                "history_budget_respected": bounded_history_chars <= context_chars,
                "full_request_chars": context.estimated_chars,
                "estimated_chars": context.estimated_chars,
                "estimated_tokens": context.estimated_tokens,
                "budget_chars": context_chars,
                "truncated": context.truncated,
                "summary_present": bool(state.context_summary),
                "dropped_messages": int(summary_meta.get("covered_message_count") or 0),
                "protocol_removed": int(summary_meta.get("protocol_removed_message_count") or 0),
            },
        }
    result["wall_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    result.update(collect_metadata())
    return result


def render_markdown(report: dict[str, Any]) -> str:
    repo = report["repo_map"]
    context = report["context"]
    cache = report["file_summary"]
    return "\n".join(
        [
            "# 大仓库与大上下文探针",
            "",
            "> 临时合成仓库；不调用模型、不写入用户工作区，报告不包含临时绝对路径和文件内容。",
            "",
            f"- Git Commit：`{report.get('git_commit') or 'unknown'}`",
            f"- 工作区快照 SHA-256：`{report.get('git_snapshot_sha256') or 'unknown'}`",
            f"- 总耗时：`{report['wall_duration_ms']}` ms",
            "",
            "| 检查 | 实测 |",
            "| --- | ---: |",
            f"| 合成源文件 | {report['fixture']['source_files']} |",
            f"| Repo Map 冷启动 / 热启动 | {repo['cold_ms']}ms / {repo['warm_ms']}ms |",
            f"| Repo Map 文件扫描 / 入选 | {repo['files_seen']} / {repo['selected']} |",
            f"| Repo Map 预算 / 实际 | {repo['budget_chars']} / {repo['selected_chars']} chars |",
            f"| Repo Map 缓存命中 | {repo['warm_cache_hits']} |",
            f"| 文件摘要修改后失效 | {'PASS' if cache['invalidated_after_edit'] else 'FAIL'} |",
            f"| 历史消息 / 压缩后摘要 | {report['fixture']['history_messages']} / {'PASS' if context['summary_present'] else 'FAIL'} |",
            f"| 历史上下文预算 / 实际 | {context['budget_chars']} / {context['history_chars_after_compaction']} chars ({'PASS' if context['history_budget_respected'] else 'FAIL'}) |",
            f"| 完整请求（含系统提示/Repo Map） | {context['full_request_chars']} chars |",
            f"| 丢弃历史消息 | {context['dropped_messages']} |",
            "",
            "结论：" + (
                "PASS（Repo Map 受预算约束、历史发生可追踪压缩、文件摘要可失效）"
                if (
                    repo["selected_chars"] <= repo["budget_chars"]
        and context["summary_present"]
        and context["history_budget_respected"]
                    and cache["cache_hit"]
                    and cache["invalidated_after_edit"]
                )
                else "FAIL（请查看 JSON 结果）"
            ),
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1_000)
    parser.add_argument("--messages", type=int, default=200)
    parser.add_argument("--message-chars", type=int, default=1_000)
    parser.add_argument("--repo-map-chars", type=int, default=12_000)
    parser.add_argument("--context-chars", type=int, default=8_000)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_probe(
        file_count=args.files,
        message_count=args.messages,
        message_chars=args.message_chars,
        repo_map_chars=args.repo_map_chars,
        context_chars=args.context_chars,
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "context-stress.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "context-stress.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    passed = (
        report["repo_map"]["selected_chars"] <= report["repo_map"]["budget_chars"]
        and report["context"]["summary_present"]
        and report["context"]["history_budget_respected"]
        and report["file_summary"]["cache_hit"]
        and report["file_summary"]["invalidated_after_edit"]
    )
    print(
        json.dumps(
            {
                "passed": passed,
                "files_seen": report["repo_map"]["files_seen"],
                "dropped_messages": report["context"]["dropped_messages"],
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
