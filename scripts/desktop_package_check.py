"""Check a PyInstaller onedir package without launching a GUI.

This is the portable half of the desktop acceptance test.  It verifies the
files that must be present in a release directory and writes a report.  The
interactive cold-start, folder picker and WebView checks still require a
Windows desktop/VM and are deliberately reported as a separate manual step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.evidence_metadata import collect_metadata
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from evidence_metadata import collect_metadata


REQUIRED_FILES = ("code-helper.exe", ".env.example")
RESOURCE_MARKERS = (
    "_internal",
    "coding_agent",
)


def _listening_ports_for_pid(pid: int) -> list[int]:
    """Return local TCP listening ports owned by a process on Windows."""

    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    ports: list[int] = []
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.split()
        if len(fields) < 5 or fields[0].upper() not in {"TCP", "TCP6"}:
            continue
        if fields[-2].upper() != "LISTENING":
            continue
        try:
            owner = int(fields[-1])
            endpoint = fields[1].rsplit(":", 1)
            port = int(endpoint[-1])
        except (ValueError, IndexError):
            continue
        if owner == pid and port not in ports:
            ports.append(port)
    return ports


def launch_smoke(package_dir: Path, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Start the packaged app and verify its local health endpoint.

    This intentionally checks only the local process boundary.  It does not
    send a model request or touch a user workspace.  The child process is
    always terminated before returning so a smoke run cannot leave a server
    behind.
    """

    executable = package_dir.resolve() / "code-helper.exe"
    result: dict[str, Any] = {
        "requested": True,
        "supported": os.name == "nt",
        "passed": False,
        "startup_ms": None,
        "port": None,
        "health_status": None,
        "exit_code": None,
        "cleanup_completed": False,
        "error": None,
    }
    if os.name != "nt":
        result["error"] = "launch smoke is only supported on Windows"
        return result
    if not executable.is_file():
        result["error"] = "code-helper.exe is missing"
        return result

    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process: subprocess.Popen[str] | None = None
    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            [str(executable)],
            cwd=str(package_dir.resolve()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        deadline = started + max(0.1, timeout_seconds)
        while time.perf_counter() < deadline:
            if process.poll() is not None:
                result["exit_code"] = process.returncode
                result["error"] = "process exited before health endpoint became ready"
                break
            for port in _listening_ports_for_pid(process.pid):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.0) as response:
                        body = json.loads(response.read().decode("utf-8", errors="replace"))
                    result["port"] = port
                    result["health_status"] = int(response.status)
                    result["startup_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    result["passed"] = response.status == 200 and body.get("ok") is True
                    if not result["passed"]:
                        result["error"] = "health endpoint returned an unexpected payload"
                    return result
                except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
                    continue
            time.sleep(0.1)
        if result["error"] is None:
            result["error"] = "health endpoint did not become ready before timeout"
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = type(exc).__name__
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process is not None:
            result["exit_code"] = process.returncode
            result["cleanup_completed"] = process.poll() is not None
    return result


def inspect_package(package_dir: Path) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    findings: list[dict[str, str]] = []
    for name in REQUIRED_FILES:
        if not (package_dir / name).is_file():
            findings.append({"code": "MISSING_FILE", "path": name, "detail": "required release file is missing"})
    for marker in RESOURCE_MARKERS:
        locations = (package_dir / marker, package_dir / "_internal" / marker)
        if not any(location.exists() for location in locations):
            findings.append({"code": "MISSING_RESOURCE", "path": marker, "detail": "PyInstaller resource directory is missing"})
    executable = package_dir / "code-helper.exe"
    sha256 = None
    if executable.is_file():
        digest = hashlib.sha256()
        with executable.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()
    file_count = sum(1 for path in package_dir.rglob("*") if path.is_file()) if package_dir.is_dir() else 0
    report = {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "package": str(package_dir),
        "platform": platform.platform(),
        "passed": not findings,
        "findings": findings,
        "file_count": file_count,
        "executable_bytes": executable.stat().st_size if executable.is_file() else None,
        "executable_sha256": sha256,
        "manual_checks_required": [
            "通过文件夹选择器打开含中文/空格/子目录的工作区",
            "验证 Markdown、代码高亮、复制和设置持久化",
            "关闭后确认无残留子进程",
        ],
    }
    report.update(collect_metadata())
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Windows EXE 包检查",
        "",
        f"结论：**{'Passed' if report['passed'] else 'Failed'}**",
        f"- Git Commit：`{report.get('git_commit') or 'unknown'}` · 工作区修改：`{'是' if report.get('git_dirty') else '否'}`",
        f"- 工作区快照 SHA-256：`{report.get('git_snapshot_sha256') or 'unknown'}`",
        f"- 检查环境：`{report.get('environment', {}).get('os', report.get('platform', 'unknown'))}`",
        f"- 包目录：`{report['package']}`",
        f"- 文件数：`{report['file_count']}`",
        f"- EXE 大小：`{report['executable_bytes'] or '—'}` bytes",
        f"- EXE SHA-256：`{report['executable_sha256'] or '—'}`",
        "",
        "## 自动检查",
        "",
        "| 代码 | 路径 | 说明 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| `{item['code']}` | `{item['path']}` | {item['detail']} |"
        for item in report["findings"]
    )
    if not report["findings"]:
        lines.append("| — | — | 所有必需文件和资源目录存在 |")
    if report.get("launch_smoke") is not None:
        launch = report["launch_smoke"]
        lines.extend([
            "",
            "## 自动冷启动探针",
            "",
            f"- 结果：**{'Passed' if launch.get('passed') else 'Failed'}**",
            f"- 平台支持：`{launch.get('supported')}` · 端口：`{launch.get('port') or '—'}` · HTTP：`{launch.get('health_status') or '—'}`",
            f"- 就绪耗时：`{launch.get('startup_ms') or '—'}` ms",
            f"- 探针结束后进程已清理：`{launch.get('cleanup_completed')}`",
            f"- 错误：`{launch.get('error') or '—'}`",
        ])
    lines.extend(["", "## 仍需人工验证", ""])
    lines.extend(f"- [ ] {item}" for item in report["manual_checks_required"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--launch-smoke-seconds",
        type=float,
        default=0.0,
        help="在 Windows 上启动 EXE 并探测 /api/health；0 表示只检查包结构",
    )
    args = parser.parse_args(argv)
    report = inspect_package(args.package)
    if args.launch_smoke_seconds > 0 and report["passed"]:
        report["launch_smoke"] = launch_smoke(args.package, timeout_seconds=args.launch_smoke_seconds)
        report["passed"] = bool(report["passed"] and report["launch_smoke"].get("passed"))
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "desktop-package.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "desktop-package.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "file_count": report["file_count"], "launch_smoke": report.get("launch_smoke")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
