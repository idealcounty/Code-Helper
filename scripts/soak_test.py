"""Run repeated deterministic Agent batches for stability/soak evidence."""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on Windows
    resource = None  # type: ignore[assignment]

try:
    from scripts.evidence_metadata import collect_metadata
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from evidence_metadata import collect_metadata

try:
    from scripts.agent_concurrency_smoke import run_probe
except ModuleNotFoundError:  # pragma: no cover - direct ``python scripts/...`` execution
    from agent_concurrency_smoke import run_probe


def _process_metrics() -> dict[str, int | float | None]:
    """Return RSS and CPU time without making psutil a hard dependency.

    ``psutil`` is preferred when installed.  Release/CI environments may not
    have it, so Windows uses the native process APIs and POSIX falls back to
    ``getrusage``.  Missing platform APIs are represented as ``None`` rather
    than making the stability probe fail.
    """

    try:
        import psutil  # type: ignore[import-not-found]

        process = psutil.Process()
        memory = process.memory_info()
        cpu = process.cpu_times()
        return {
            "rss_bytes": int(memory.rss),
            "cpu_time_seconds": round(float(cpu.user + cpu.system), 6),
        }
    except (ImportError, OSError, AttributeError):
        pass

    if os.name == "nt":
        try:
            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("page_fault_count", ctypes.c_ulong),
                    ("peak_working_set_size", ctypes.c_size_t),
                    ("working_set_size", ctypes.c_size_t),
                    ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                    ("quota_paged_pool_usage", ctypes.c_size_t),
                    ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                    ("quota_non_paged_pool_usage", ctypes.c_size_t),
                    ("pagefile_usage", ctypes.c_size_t),
                    ("peak_pagefile_usage", ctypes.c_size_t),
                ]

            class _FileTime(ctypes.Structure):
                _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.GetProcessTimes.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
            ]
            kernel32.GetProcessTimes.restype = ctypes.c_int
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCounters),
                ctypes.c_ulong,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int

            process_handle = kernel32.GetCurrentProcess()
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            memory_ok = psapi.GetProcessMemoryInfo(
                process_handle, ctypes.byref(counters), ctypes.sizeof(counters)
            )
            created = _FileTime()
            exited = _FileTime()
            kernel = _FileTime()
            user = _FileTime()
            cpu_ok = kernel32.GetProcessTimes(
                process_handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )

            def _filetime_seconds(value: _FileTime) -> float:
                ticks = (int(value.high) << 32) | int(value.low)
                return ticks / 10_000_000.0

            return {
                "rss_bytes": int(counters.working_set_size) if memory_ok else None,
                "cpu_time_seconds": round(
                    _filetime_seconds(kernel) + _filetime_seconds(user), 6
                )
                if cpu_ok
                else None,
            }
        except (AttributeError, OSError, TypeError):
            return {"rss_bytes": None, "cpu_time_seconds": None}

    if resource is None:
        return {"rss_bytes": None, "cpu_time_seconds": None}
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux reports KiB; macOS reports bytes.  The probe only needs a
        # stable approximation, and the platform check avoids importing psutil.
        rss = int(usage.ru_maxrss * (1024 if os.uname().sysname == "Linux" else 1))
        return {
            "rss_bytes": rss,
            "cpu_time_seconds": round(float(usage.ru_utime + usage.ru_stime), 6),
        }
    except (AttributeError, OSError, ValueError):
        return {"rss_bytes": None, "cpu_time_seconds": None}


def _memory_rss_bytes() -> int | None:
    """Backward-compatible helper retained for callers of older scripts."""

    return _process_metrics()["rss_bytes"]  # type: ignore[return-value]


def run_soak(*, duration_seconds: float, round_sessions: int, concurrency: int, timeout: float) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    rounds: list[dict[str, Any]] = []
    round_number = 0
    while time.perf_counter() - started < max(0.1, duration_seconds):
        round_number += 1
        before_metrics = _process_metrics()
        report = run_probe(round_sessions, concurrency, timeout)
        after_metrics = _process_metrics()
        before = before_metrics["rss_bytes"]
        after = after_metrics["rss_bytes"]
        cpu_before = before_metrics["cpu_time_seconds"]
        cpu_after = after_metrics["cpu_time_seconds"]
        rounds.append({
            "round": round_number,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "completion_rate": report["completion_rate"],
            "failures": report["failures"],
            "event_session_mismatches": report["event_session_mismatches"],
            "rss_before_bytes": before,
            "rss_after_bytes": after,
            "cpu_time_before_seconds": cpu_before,
            "cpu_time_after_seconds": cpu_after,
            "cpu_time_delta_seconds": round(max(float(cpu_after or 0) - float(cpu_before or 0), 0), 6)
            if cpu_before is not None and cpu_after is not None
            else None,
        })
    total_sessions = sum(round_sessions for _ in rounds)
    failures = sum(int(item["failures"]) for item in rounds)
    rss_values = [
        int(value)
        for item in rounds
        for value in (item["rss_before_bytes"], item["rss_after_bytes"])
        if value is not None
    ]
    cpu_deltas = [
        float(item["cpu_time_delta_seconds"])
        for item in rounds
        if item["cpu_time_delta_seconds"] is not None
    ]
    report = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "rounds": len(rounds),
        "sessions": total_sessions,
        "failures": failures,
        "completion_rate": round((total_sessions - failures) / total_sessions, 6) if total_sessions else 0.0,
        "rss_peak_bytes": max(rss_values) if rss_values else None,
        "rss_min_bytes": min(rss_values) if rss_values else None,
        "cpu_time_seconds": round(sum(cpu_deltas), 6) if cpu_deltas else None,
        "round_results": rounds,
    }
    report.update(collect_metadata())
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Agent 稳定性浸泡测试",
        "",
        "> 使用临时工作区和 ScriptedModel；优先使用 psutil，Windows/POSIX 均有原生指标回退。",
        "",
        f"- Git Commit：`{report.get('git_commit') or 'unknown'}` · 工作区修改：`{'是' if report.get('git_dirty') else '否'}`",
        f"- 工作区快照 SHA-256：`{report.get('git_snapshot_sha256') or 'unknown'}`",
        f"- 环境：`{report.get('environment', {}).get('os', 'unknown')}` · Python `{report.get('environment', {}).get('python', 'unknown')}`",
        "",
        f"- 时长：`{report['duration_seconds']}` 秒 · 轮次：`{report['rounds']}`",
        f"- 会话：`{report['sessions']}` · 失败：`{report['failures']}` · 完成率：`{report['completion_rate']:.2%}`",
        f"- RSS 峰值：`{report.get('rss_peak_bytes') or '—'}` bytes · CPU 时间：`{report.get('cpu_time_seconds') or '—'}` 秒",
        "",
        "| 轮次 | 完成率 | 失败 | 事件串扰 | RSS 前/后 | CPU 增量 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["round_results"]:
        before = item["rss_before_bytes"] or "—"
        after = item["rss_after_bytes"] or "—"
        cpu_delta = item.get("cpu_time_delta_seconds")
        lines.append(f"| {item['round']} | {item['completion_rate']:.2%} | {item['failures']} | {item['event_session_mismatches']} | {before} / {after} | {cpu_delta if cpu_delta is not None else '—'} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--round-sessions", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if min(args.duration_seconds, args.round_sessions, args.concurrency, args.timeout) <= 0:
        parser.error("all numeric arguments must be positive")
    report = run_soak(duration_seconds=args.duration_seconds, round_sessions=args.round_sessions, concurrency=args.concurrency, timeout=args.timeout)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "soak.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "soak.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=report["round_results"][0].keys() if report["round_results"] else ["round"])
        writer.writeheader()
        writer.writerows(report["round_results"])
    (output_dir / "soak.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"rounds": report["rounds"], "completion_rate": report["completion_rate"]}, ensure_ascii=False))
    return 0 if report["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
