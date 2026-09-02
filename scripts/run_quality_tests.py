"""Run the deterministic quality gate and keep reproducible test evidence.

The default run is intentionally local and deterministic.  It does not call a
paid model.  Optional Eval commands can be enabled with ``--include-evals``.
Every command writes a bounded log plus a machine-readable manifest and a
human-readable summary, so a test result can be tied to a Git commit.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
_SNAPSHOT_ROOTS = ("src/", "scripts/", "tests/", "evals/", ".github/")
_SNAPSHOT_FILES = {"pyproject.toml", "pytest.ini", "requirements.txt"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_text(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=False,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    output = result.stdout or result.stderr or b""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    return result.returncode, str(output).strip()


def _git_metadata() -> dict[str, Any]:
    code, commit = _run_text(["git", "rev-parse", "HEAD"])
    status_code, status = _run_text(["git", "status", "--porcelain"])
    snapshot_hash = hashlib.sha256()
    if status_code == 0:
        snapshot_hash.update(status.encode("utf-8", "replace"))
        _, diff = _run_text(["git", "diff", "--no-ext-diff", "--binary"])
        snapshot_hash.update(diff.encode("utf-8", "replace"))
        _, untracked = _run_text(["git", "ls-files", "--others", "--exclude-standard", "-z"])
        for name in [item for item in untracked.split("\0") if item]:
            normalized_name = name.replace("\\", "/")
            if normalized_name not in _SNAPSHOT_FILES and not normalized_name.startswith(_SNAPSHOT_ROOTS):
                continue
            path = ROOT / name
            snapshot_hash.update(name.encode("utf-8", "replace"))
            try:
                if path.is_file() and path.stat().st_size <= 20_000_000:
                    snapshot_hash.update(path.read_bytes())
            except OSError:
                snapshot_hash.update(b"<unreadable>")
    return {
        "git_commit": commit if code == 0 else "unknown",
        "git_dirty": bool(status.strip()) if status_code == 0 else None,
        "git_status_entries": len(status.splitlines()) if status_code == 0 and status else None,
        "git_snapshot_sha256": snapshot_hash.hexdigest() if status_code == 0 else None,
    }


def _safe_environment() -> dict[str, Any]:
    """Collect non-secret configuration only."""
    provider = os.getenv("CODE_HELPER_PROVIDER", "deepseek").strip() or "deepseek"
    model = os.getenv("CODE_HELPER_MODEL", "").strip() or None
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "provider": provider,
        "model": model,
        "paid_model_request": False,
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def run_command(
    name: str,
    command: list[str],
    output_dir: Path,
    *,
    timeout_seconds: float = 900,
    required: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    log_path = output_dir / f"{name}.log"
    display_command = [_display_path(Path(item)) if Path(item).is_absolute() else item for item in command]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
        status = "passed" if result.returncode == 0 else "failed"
        return_code = result.returncode
    except FileNotFoundError as exc:
        output = f"Command unavailable: {exc}"
        status = "failed" if required else "skipped"
        return_code = None
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        output = f"Timed out after {timeout_seconds:.0f}s\n{stdout}\n{stderr}".strip()
        status = "failed"
        return_code = None
    except OSError as exc:
        output = f"Could not start command: {exc}"
        status = "failed" if required else "skipped"
        return_code = None
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    log_path.write_text(output + "\n", encoding="utf-8")
    return {
        "name": name,
        "command": display_command,
        "status": status,
        "return_code": return_code,
        "required": required,
        "duration_ms": duration_ms,
        "log": _display_path(log_path),
    }


def _default_commands(output_dir: Path) -> list[tuple[str, list[str], bool]]:
    commands: list[tuple[str, list[str], bool]] = [
        ("compile-python", [sys.executable, "-m", "compileall", "-q", "src", "evals", "scripts"], True),
        ("javascript-syntax", ["node", "--check", "src/coding_agent/web/static/app.js"], False),
        ("git-diff-check", ["git", "diff", "--check"], True),
        ("security-audit", [sys.executable, "scripts/security_audit.py", "--output-dir", str(output_dir / "security")], True),
        (
            "fault-injection",
            [
                sys.executable,
                "scripts/fault_injection_smoke.py",
                "--output-dir",
                str(output_dir / "fault-injection"),
            ],
            True,
        ),
        (
            "context-stress",
            [
                sys.executable,
                "scripts/context_stress_smoke.py",
                "--files",
                "250",
                "--messages",
                "80",
                "--output-dir",
                str(output_dir / "context-stress"),
            ],
            True,
        ),
        (
            "api-contract",
            [
                sys.executable,
                "scripts/api_contract_smoke.py",
                "--output-dir",
                str(output_dir / "api-contract"),
            ],
            True,
        ),
    ]
    pytest_command = [sys.executable, "-m", "pytest", "-q", "--junitxml", str(output_dir / "junit.xml")]
    if importlib.util.find_spec("pytest_cov") is not None:
        pytest_command.extend([
            "--cov=src/coding_agent",
            "--cov-branch",
            f"--cov-report=xml:{output_dir / 'coverage.xml'}",
            f"--cov-report=html:{output_dir / 'coverage-html'}",
            "--cov-report=term-missing",
        ])
    commands.append(("pytest", pytest_command, True))
    return commands


def _eval_commands(output_dir: Path) -> list[tuple[str, list[str], bool]]:
    return [
        (
            "e2e-deterministic",
            [
                sys.executable,
                "scripts/e2e_deterministic_smoke.py",
                "--output-dir",
                str(output_dir / "e2e"),
            ],
            True,
        ),
        (
            "agent-eval",
            [sys.executable, "-m", "evals.runner", "--output-dir", str(output_dir / "eval"), "--compare", "evals/reports/baseline.json"],
            True,
        ),
        (
            "retrieval-benchmark",
            [sys.executable, "-m", "evals.retrieval_benchmark", "--output-dir", str(output_dir / "retrieval")],
            True,
        ),
        (
            "algorithm-benchmark",
            [sys.executable, "-m", "evals.algorithm_benchmark", "--output-dir", str(output_dir / "algorithm")],
            True,
        ),
        (
            "profile-comparison",
            [sys.executable, "-m", "evals.profile_comparison", "--output-dir", str(output_dir / "profile")],
            True,
        ),
        (
            "superpowers-comparison",
            [
                sys.executable,
                "-m",
                "evals.superpowers_comparison",
                "--mode",
                "deterministic",
                "--output-dir",
                str(output_dir / "superpowers-comparison"),
            ],
            True,
        ),
    ]


def _write_summary(manifest: dict[str, Any], path: Path) -> None:
    results = manifest["commands"]
    passed = sum(item["status"] == "passed" for item in results)
    failed = sum(item["status"] == "failed" for item in results)
    skipped = sum(item["status"] == "skipped" for item in results)
    lines = [
        "# Code Helper 测试运行摘要",
        "",
        "> 此文件由 `scripts/run_quality_tests.py` 生成。数据只描述本次实际执行结果。",
        "",
        f"- Run ID：`{manifest['run_id']}`",
        f"- 开始：`{manifest['started_at']}`",
        f"- 结束：`{manifest['finished_at']}`",
        f"- Git Commit：`{manifest['git_commit']}`",
        f"- 工作区修改：`{'是' if manifest.get('git_dirty') else '否' if manifest.get('git_dirty') is False else '未知'}`",
        f"- 工作区快照 SHA-256：`{manifest.get('git_snapshot_sha256') or '未知'}`",
        f"- 环境：`{manifest['environment']['os']}` / Python `{manifest['environment']['python']}`",
        f"- Provider：`{manifest['environment']['provider']}` / Model：`{manifest['environment']['model'] or '未指定'}`",
        "",
        "## 结果",
        "",
        "| 检查 | 状态 | 耗时 | 日志 |",
        "| --- | --- | ---: | --- |",
    ]
    for item in results:
        lines.append(f"| `{item['name']}` | {item['status']} | {item['duration_ms']:.0f} ms | `{item['log']}` |")
    lines.extend([
        "",
        f"通过：**{passed}** · 失败：**{failed}** · 跳过：**{skipped}**",
        "",
        f"总体结论：**{'Passed' if failed == 0 else 'Failed'}**",
        "",
        "## 证据文件",
        "",
        "- `manifest.json`：机器可读的环境、提交和命令记录。",
        "- `junit.xml`：pytest 测试明细（如果 pytest 已启动）。",
        "- `coverage.xml`：安装 pytest-cov 时生成。",
        "- 各命令 `.log`：完整 stdout/stderr，不应包含 API Key。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="结果目录；默认写入 test-results/<run-id>")
    parser.add_argument("--include-evals", action="store_true", help="额外运行确定性 Agent Eval 和三个基准")
    parser.add_argument(
        "--include-coverage",
        action="store_true",
        help="使用标准库 trace 额外生成本机行覆盖率基线（比普通门禁更慢）",
    )
    parser.add_argument(
        "--include-mutation",
        action="store_true",
        help="在临时副本中运行关键不变量变异测试（比普通门禁更慢）",
    )
    parser.add_argument("--timeout", type=float, default=900, help="单个命令最大秒数，默认 900")
    args = parser.parse_args(argv)

    metadata = _git_metadata()
    short_commit = str(metadata.get("git_commit") or "unknown")[:8]
    run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_" + short_commit
    output_dir = (args.output_dir or ROOT / "test-results" / run_id).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    commands = _default_commands(output_dir)
    if args.include_evals:
        commands.extend(_eval_commands(output_dir))
    if args.include_coverage:
        commands.append(
            (
                "coverage-baseline",
                [
                    sys.executable,
                    "scripts/coverage_baseline.py",
                    "--output-dir",
                    str(output_dir / "coverage-baseline"),
                ],
                True,
            )
        )
    if args.include_mutation:
        commands.append(
            (
                "mutation-smoke",
                [
                    sys.executable,
                    "scripts/mutation_smoke.py",
                    "--output-dir",
                    str(output_dir / "mutation"),
                ],
                True,
            )
        )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": _now(),
        **metadata,
        "environment": _safe_environment(),
        "include_evals": bool(args.include_evals),
        "include_coverage": bool(args.include_coverage),
        "include_mutation": bool(args.include_mutation),
        "commands": [],
    }
    for name, command, required in commands:
        result = run_command(name, command, output_dir, timeout_seconds=args.timeout, required=required)
        manifest["commands"].append(result)
        if result["status"] == "failed" and required:
            # Continue running remaining checks so a single report explains all
            # failures instead of hiding later diagnostics.
            continue
    manifest["finished_at"] = _now()
    required_failures = [item for item in manifest["commands"] if item["required"] and item["status"] == "failed"]
    manifest["result"] = "failed" if required_failures else "passed"
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_summary(manifest, output_dir / "summary.md")
    print(json.dumps({"run_id": run_id, "result": manifest["result"], "output_dir": _display_path(output_dir)}, ensure_ascii=False))
    return 1 if required_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
