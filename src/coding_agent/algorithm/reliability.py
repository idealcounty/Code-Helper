"""Persistable, explainable reports for algorithm judge runs.

The judge remains the source of truth for pass/fail decisions.  This module
only turns its bounded result into a versioned report that the Web workbench
can inspect and export; it never upgrades a model-generated answer into proof
of correctness.
"""

from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .complexity import analyze_file

REPORT_VERSION = 1
REPORT_DIRECTORY = Path(".code-helper") / "algorithm-runs"


def build_report(
    *,
    session_id: str,
    turn_id: str,
    step: int,
    event_sequence: int,
    arguments: dict[str, Any],
    result: dict[str, Any],
    complexity: dict[str, Any] | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any] | None:
    """Build a report from a serialized ``judge_algorithm`` ToolResult."""

    data = result.get("data") if isinstance(result, dict) else None
    judge = data.get("judge") if isinstance(data, dict) else None
    if not isinstance(judge, dict):
        return None
    raw_cases = judge.get("cases")
    cases = raw_cases if isinstance(raw_cases, list) else []
    inputs = arguments.get("cases")
    input_by_label = {
        str(item.get("label") or f"case-{index + 1}"): str(item.get("input") or "")
        for index, item in enumerate(inputs if isinstance(inputs, list) else [])
        if isinstance(item, dict)
    }
    normalized_cases: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        label = str(case.get("label") or f"case-{index + 1}")
        normalized_cases.append(
            {
                "label": label,
                "status": str(case.get("status") or "unknown"),
                "input": input_by_label.get(label, ""),
                "expected": str(case.get("expected") or ""),
                "actual": str(case.get("actual") or ""),
                "detail": str(case.get("detail") or ""),
                "duration_ms": _non_negative_number(case.get("duration_ms")),
                "oracle_duration_ms": _non_negative_number(case.get("oracle_duration_ms")),
                "input_size": _non_negative_int(case.get("input_size")),
                "case_source": str(case.get("case_source") or "explicit"),
                "oracle_source": str(case.get("oracle_source") or "expected_output"),
            }
        )
    report_id = uuid4().hex
    total = _non_negative_int(judge.get("total"))
    passed = _non_negative_int(judge.get("passed"))
    failed = _non_negative_int(judge.get("failed"))
    statuses = {str(item.get("status") or "unknown") for item in normalized_cases}
    if total > 0 and failed == 0:
        conclusion = "VERIFIED_FOR_CASES"
    elif statuses & {"wrong_answer", "runtime_error", "time_limit_exceeded", "output_limit", "output_limit_exceeded"}:
        conclusion = "FAILURE_FOUND"
    else:
        conclusion = "INCONCLUSIVE"
    source_path: Path | None = None
    requested_path = str(arguments.get("path") or "")
    if workspace_root is not None and requested_path:
        try:
            candidate_path = (workspace_root / requested_path).resolve()
            if candidate_path.is_file() and candidate_path.is_relative_to(workspace_root.resolve()):
                source_path = candidate_path
        except (OSError, ValueError):
            source_path = None
    if complexity is None and source_path is not None:
        measured = analyze_file(source_path)
        complexity = measured if measured.get("status") == "ok" else None
    report: dict[str, Any] = {
        "schema_version": REPORT_VERSION,
        "report_id": report_id,
        "created_at": datetime.now(UTC).isoformat(),
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "step": max(0, int(step)),
        "event_sequence": max(0, int(event_sequence)),
        "source": {
            "path": str(arguments.get("path") or ""),
            "command": str(arguments.get("command") or arguments.get("candidate_command") or ""),
            "seed": _non_negative_int(judge.get("seed")),
        },
        "oracle": {
            "type": str((judge.get("oracle") or {}).get("type") or "expected_output"),
            "command": str((judge.get("oracle") or {}).get("command") or ""),
        },
        "summary": {
            "status": conclusion,
            "total": total,
            "passed": passed,
            "failed": failed,
            "first_failure": judge.get("first_failure"),
            "minimized_input": judge.get("minimized_input"),
            "shrink_trace": judge.get("shrink_trace") if isinstance(judge.get("shrink_trace"), list) else [],
        },
        "cases": normalized_cases,
        "complexity": complexity if isinstance(complexity, dict) else None,
        "benchmark": judge.get("benchmark") if isinstance(judge.get("benchmark"), dict) else None,
        "evidence": {
            "level": "deterministic",
            "kind": "algorithm_experiment" if arguments.get("oracle_command") else "algorithm_judge",
            "tool_result_code": str(result.get("code") or ""),
            "message": str(result.get("message") or ""),
        },
    }
    for index, case in enumerate(report["cases"]):
        case["expected_hash"] = _digest(case.get("expected", ""))
        case["actual_hash"] = _digest(case.get("actual", ""))
    path = requested_path
    if source_path is not None:
        try:
            report["source"]["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            pass
    return report


def persist_report(workspace_root: Path, report: dict[str, Any]) -> Path:
    """Atomically persist one report below the reserved runtime directory."""

    report_id = _safe_id(report.get("report_id"))
    if not report_id:
        raise ValueError("Algorithm report requires a report_id")
    directory = workspace_root / REPORT_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{report_id}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def list_reports(workspace_root: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    directory = workspace_root / REPORT_DIRECTORY
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    paths = sorted(
        directory.glob("*.json"),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
        reverse=True,
    )
    for path in paths[: max(0, min(int(limit), 200))]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == REPORT_VERSION:
            found.append(payload)
    return found


def get_report(workspace_root: Path, report_id: str) -> dict[str, Any] | None:
    safe_id = _safe_id(report_id)
    if not safe_id:
        return None
    path = workspace_root / REPORT_DIRECTORY / f"{safe_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("schema_version") == REPORT_VERSION else None


def render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    source = report.get("source") or {}
    complexity = report.get("complexity") or {}
    lines = [
        "# Algorithm Reliability Report",
        "",
        f"- Status: `{summary.get('status', 'inconclusive')}`",
        f"- Cases: **{summary.get('passed', 0)} / {summary.get('total', 0)} passed**",
        f"- Failed: **{summary.get('failed', 0)}**",
        f"- Seed: `{source.get('seed', 0)}`",
        f"- Source: `{source.get('path') or 'not specified'}`",
        f"- Source SHA-256: `{source.get('sha256') or 'not recorded'}`",
        f"- Evidence: `{(report.get('evidence') or {}).get('level', 'unknown')}`",
    ]
    oracle = report.get("oracle") or {}
    if oracle:
        lines.extend(
            [
                f"- Oracle: `{oracle.get('type', 'unknown')}`",
                f"- Oracle command: `{oracle.get('command') or 'not specified'}`",
            ]
        )
    if complexity:
        lines.extend(
            [
                "",
                "## Complexity",
                "",
                f"- Estimated time: `{complexity.get('estimated_time_complexity', 'unknown')}`",
                f"- Parser: `{complexity.get('parser', 'unknown')}`",
            ]
        )
    benchmark = report.get("benchmark") or {}
    if benchmark:
        lines.extend(
            [
                "",
                "## Runtime benchmark",
                "",
                f"- Samples: `{benchmark.get('samples', 0)}`",
                f"- P50: `{benchmark.get('p50_ms', 0)} ms`",
                f"- P95: `{benchmark.get('p95_ms', 0)} ms`",
                f"- Max: `{benchmark.get('max_ms', 0)} ms`",
            ]
        )
        curve = benchmark.get("curve")
        if isinstance(curve, list) and curve:
            lines.extend(
                [
                    "",
                    "### Benchmark curve",
                    "",
                    "| Input bytes | Samples | P50 (ms) | P95 (ms) | Max (ms) |",
                    "| ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            lines.extend(
                f"| {item.get('input_size', 0)} | {item.get('samples', 0)} | {item.get('p50_ms', 0)} | {item.get('p95_ms', 0)} | {item.get('max_ms', 0)} |"
                for item in curve
                if isinstance(item, dict)
            )
    first_failure = summary.get("first_failure")
    if isinstance(first_failure, dict):
        lines.extend(
            [
                "",
                "## First failure",
                "",
                f"- Case: `{first_failure.get('label', '')}`",
                f"- Status: `{first_failure.get('status', '')}`",
                f"- Detail: {first_failure.get('detail', '')}",
            ]
        )
    minimized = summary.get("minimized_input")
    if minimized is not None:
        lines.extend(["", "## Minimized input", "", "```text", str(minimized), "```"])
    return "\n".join(lines) + "\n"


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text and all(char.isalnum() or char in "-_" for char in text) else ""


def _digest(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _non_negative_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(number, 3) if number >= 0 else 0.0
