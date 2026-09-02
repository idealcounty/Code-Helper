"""Measure a local line-coverage baseline without requiring pytest-cov.

The CI pipeline uses pytest-cov for richer XML/HTML output.  This small
standard-library fallback keeps a useful, reproducible baseline available on a
clean Windows checkout where optional coverage packages are not installed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from trace import Trace
from types import CodeType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "coding_agent"


def _code_line_numbers(code: CodeType, result: set[int] | None = None) -> set[int]:
    lines = result if result is not None else set()
    for _, _, line in code.co_lines():
        if line is not None and line > 0:
            lines.add(int(line))
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            _code_line_numbers(constant, lines)
    return lines


def executable_lines(path: Path) -> set[int]:
    """Return line numbers represented by Python bytecode for ``path``."""

    try:
        source = path.read_text(encoding="utf-8")
        code = compile(source, str(path), "exec")
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    return _code_line_numbers(code)


def _normal_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _run_pytest(pytest_args: list[str]) -> tuple[int, dict[tuple[str, int], int], float]:
    import pytest

    tracer = Trace(
        count=True,
        trace=False,
        ignoredirs=(str(Path(sys.prefix).resolve()),),
    )
    started = time.perf_counter()
    exit_code = tracer.runfunc(pytest.main, pytest_args)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    counts = getattr(tracer.results(), "counts", {})
    return int(exit_code), counts, duration_ms


def build_report(*, pytest_args: list[str]) -> dict[str, Any]:
    exit_code, counts, duration_ms = _run_pytest(pytest_args)
    normalized_counts: dict[tuple[Path, int], int] = {
        (_normal_path(path), int(line)): int(count)
        for (path, line), count in counts.items()
    }
    files: list[dict[str, Any]] = []
    total_executable = 0
    total_covered = 0
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        executable = executable_lines(path)
        covered = {line for line in executable if normalized_counts.get((path.resolve(), line), 0) > 0}
        total_executable += len(executable)
        total_covered += len(covered)
        files.append({
            "path": str(path.resolve().relative_to(ROOT)).replace("\\", "/"),
            "executable_lines": len(executable),
            "covered_lines": len(covered),
            "missing_lines": sorted(executable - covered),
            "line_rate": round(len(covered) / len(executable), 6) if executable else 1.0,
        })
    return {
        "schema_version": 1,
        "tool": "python.trace",
        "pytest_args": pytest_args,
        "pytest_exit_code": exit_code,
        "passed": exit_code == 0,
        "duration_ms": duration_ms,
        "executable_lines": total_executable,
        "covered_lines": total_covered,
        "line_rate": round(total_covered / total_executable, 6) if total_executable else 1.0,
        "files": files,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Code Helper 本机覆盖率基线",
        "",
        "> 使用 Python 标准库 `trace` 运行 pytest；该报告是行覆盖率基线，不替代 pytest-cov 的分支覆盖率报告。",
        "",
        f"- pytest 状态：**{'PASS' if report['passed'] else 'FAIL'}**（退出码 `{report['pytest_exit_code']}`）",
        f"- 总耗时：`{report['duration_ms']}`ms",
        f"- 可执行行：`{report['executable_lines']}` · 覆盖行：`{report['covered_lines']}` · 行覆盖率：**{report['line_rate']:.2%}**",
        "",
        "| 文件 | 可执行行 | 覆盖行 | 行覆盖率 | 未覆盖行（最多 20 个） |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for item in report["files"]:
        missing = ", ".join(str(line) for line in item["missing_lines"][:20]) or "—"
        lines.append(
            f"| `{item['path']}` | {item['executable_lines']} | {item['covered_lines']} | "
            f"{item['line_rate']:.2%} | {missing} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="pytest 参数；可在 -- 后传入，例如 -- tests/test_agent_loop.py",
    )
    args = parser.parse_args(argv)
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if not pytest_args:
        pytest_args = ["-q"]
    report = build_report(pytest_args=pytest_args)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "coverage-baseline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "coverage-baseline.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    print(json.dumps({"passed": report["passed"], "line_rate": report["line_rate"], "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
