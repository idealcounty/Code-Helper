"""Run a small, dependency-free mutation test suite for critical invariants.

This is deliberately narrower than a full mutmut run: it copies the package and
tests to a temporary directory, applies one controlled source mutation at a
time, and checks that the selected regression tests kill that mutation.  The
original working tree is never modified.  It is useful on machines where the
optional ``mutmut`` package is unavailable and produces the same auditable
``json``/Markdown evidence shape as the other quality probes.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.evidence_metadata import collect_metadata
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    from evidence_metadata import collect_metadata


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Mutation:
    mutation_id: str
    target: str
    needle: str
    replacement: str
    tests: tuple[str, ...]
    rationale: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "approval-full-access-scope",
        "src/coding_agent/permissions.py",
        'if self.approval_mode is ApprovalMode.FULL and mode == "act":',
        "if self.approval_mode is ApprovalMode.FULL:",
        ("tests/test_permissions.py",),
        "Full access must not bypass the Ask/Plan mode boundary.",
    ),
    Mutation(
        "workspace-boundary-direction",
        "src/coding_agent/permissions.py",
        "if not candidate.resolve().is_relative_to(self.workspace_root):",
        "if candidate.resolve().is_relative_to(self.workspace_root):",
        ("tests/test_permissions.py",),
        "Outside-workspace paths must remain hard denied.",
    ),
    Mutation(
        "step-budget-off-by-one",
        "src/coding_agent/budget.py",
        "if self.max_steps is not None and next_step > self.max_steps:",
        "if self.max_steps is not None and next_step >= self.max_steps:",
        ("tests/test_budget.py",),
        "The configured maximum step itself is legal; only the next step is rejected.",
    ),
    Mutation(
        "token-budget-boundary",
        "src/coding_agent/budget.py",
        "if self.token_limit is not None and self.consumed_tokens >= self.token_limit:",
        "if self.token_limit is not None and self.consumed_tokens > self.token_limit:",
        ("tests/test_budget.py",),
        "A request at the token limit must be rejected before another model call.",
    ),
    Mutation(
        "tool-result-pairing-equality",
        "src/coding_agent/context.py",
        "and set(result_ids) == set(call_ids)",
        "and set(result_ids) != set(call_ids)",
        ("tests/test_context.py",),
        "Provider history must retain only complete assistant/tool exchanges.",
    ),
)


def _copy_fixture(destination: Path) -> None:
    shutil.copytree(ROOT / "src", destination / "src")
    shutil.copytree(ROOT / "tests", destination / "tests")
    shutil.copy2(ROOT / "pyproject.toml", destination / "pyproject.toml")


def _run_one(mutation: Mutation, *, timeout_seconds: float) -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="code-helper-mutation-") as raw_dir:
        fixture = Path(raw_dir)
        _copy_fixture(fixture)
        target = fixture / mutation.target
        source = target.read_text(encoding="utf-8")
        occurrences = source.count(mutation.needle)
        if occurrences != 1:
            return {
                "id": mutation.mutation_id,
                "status": "error",
                "message": f"expected one mutation site, found {occurrences}",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        target.write_text(source.replace(mutation.needle, mutation.replacement, 1), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(fixture / "src")
        command = [sys.executable, "-m", "pytest", "-q", "--disable-warnings", "--maxfail=1", *mutation.tests]
        try:
            completed = subprocess.run(
                command,
                cwd=fixture,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "id": mutation.mutation_id,
                "status": "error",
                "message": f"mutation test timed out after {timeout_seconds:g}s",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        except OSError as exc:
            return {
                "id": mutation.mutation_id,
                "status": "error",
                "message": str(exc),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        # A failing regression suite means the mutation was killed, which is
        # the desired result.  Keep only a bounded diagnostic for audit logs.
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        return {
            "id": mutation.mutation_id,
            "status": "killed" if completed.returncode else "survived",
            "return_code": completed.returncode,
            "tests": list(mutation.tests),
            "diagnostic": output[-1200:],
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def _write_report(output_dir: Path, payload: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "mutation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    results = payload["mutations"]
    killed = sum(item["status"] == "killed" for item in results)
    total = len(results)
    lines = [
        "# Code Helper 关键不变量变异测试",
        "",
        "> 这是不依赖 mutmut 的最小变异测试；每个变异在临时副本中运行，原工作区不会被修改。",
        "",
        f"- Git Commit：`{payload['git_commit']}`",
        f"- 工作区快照 SHA-256：`{payload['git_snapshot_sha256']}`",
        f"- 环境：`{payload['environment']['os']}` / Python `{payload['environment']['python']}`",
        f"- 结果：**{killed}/{total} mutations killed**；Mutation Score **{(killed / total * 100) if total else 0:.1f}%**",
        "",
        "| 变异 | 状态 | 目标 |",
        "| --- | --- | --- |",
    ]
    descriptions = {item.mutation_id: item.rationale for item in MUTATIONS}
    for item in results:
        lines.append(f"| `{item['id']}` | **{item['status']}** | {descriptions.get(item['id'], '')} |")
    lines.extend(
        [
            "",
            "`killed` 表示对应回归测试按预期失败；`survived` 或 `error` 必须在发布前解释并补测。",
        ]
    )
    (output_dir / "mutation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "test-results" / "mutation-smoke")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    metadata = collect_metadata()
    started_at = datetime.now(UTC).isoformat()
    results = [_run_one(mutation, timeout_seconds=args.timeout) for mutation in MUTATIONS]
    finished_at = datetime.now(UTC).isoformat()
    payload = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "mutations": results,
        "git_commit": metadata.get("git_commit"),
        "git_dirty": metadata.get("git_dirty"),
        "git_snapshot_sha256": metadata.get("git_snapshot_sha256"),
        "environment": metadata.get("environment", {"os": platform.platform(), "python": platform.python_version()}),
    }
    _write_report(args.output_dir, payload)
    killed = sum(item["status"] == "killed" for item in results)
    errors = sum(item["status"] == "error" for item in results)
    print(json.dumps({"killed": killed, "total": len(results), "errors": errors, "output_dir": str(args.output_dir)}, ensure_ascii=False))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
