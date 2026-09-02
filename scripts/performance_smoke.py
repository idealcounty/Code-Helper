"""Run a bounded read-only HTTP load probe and record latency evidence.

The probe never sends a chat message or mutating request.  Start the Web
server separately, then run for example::

    python scripts/performance_smoke.py --url http://127.0.0.1:8765 \
        --path /api/health --requests 500 --concurrency 25 \
        --output-dir test-results/performance

It uses only the Python standard library, so it is useful in a clean release
environment as well as during development.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import platform
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


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, quantile))
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def request_once(url: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
            status = int(response.status)
        result = {"ok": 200 <= status < 400, "status": status, "bytes": len(body), "error": None}
    except urllib.error.HTTPError as exc:
        result = {"ok": False, "status": int(exc.code), "bytes": 0, "error": f"HTTP {exc.code}"}
    except (OSError, TimeoutError) as exc:
        result = {"ok": False, "status": None, "bytes": 0, "error": type(exc).__name__}
    result["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


def run_probe(
    url: str,
    *,
    requests: int,
    concurrency: int,
    timeout: float,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    samples: list[dict[str, Any]] = []

    def worker(_: int) -> dict[str, Any]:
        return request_once(url, timeout)

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for result in pool.map(worker, range(max(0, requests))):
            samples.append(result)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    latencies = [float(item["duration_ms"]) for item in samples]
    successes = sum(bool(item["ok"]) for item in samples)
    errors = len(samples) - successes
    report = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "url": url,
        "requests": len(samples),
        "concurrency": max(1, concurrency),
        "wall_duration_ms": duration_ms,
        "throughput_per_second": round(len(samples) / max(duration_ms / 1000, 0.001), 3),
        "successes": successes,
        "errors": errors,
        "error_rate": round(errors / len(samples), 6) if samples else 0.0,
        "latency_ms": {
            "min": round(min(latencies), 3) if latencies else 0.0,
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "p99": round(percentile(latencies, 0.99), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "status_codes": {
            str(code): sum(item["status"] == code for item in samples)
            for code in sorted({item["status"] for item in samples if item["status"] is not None})
        },
        "environment": {"os": platform.platform(), "python": platform.python_version()},
    }
    report.update(collect_metadata())
    return report


def render_markdown(report: dict[str, Any]) -> str:
    latency = report["latency_ms"]
    return "\n".join([
        "# HTTP 性能探针报告",
        "",
        "> 只访问只读接口，不代表 Agent 模型任务质量。",
        "",
        f"- Git Commit：`{report.get('git_commit') or 'unknown'}` · 工作区修改：`{'是' if report.get('git_dirty') else '否'}`",
        f"- 工作区快照 SHA-256：`{report.get('git_snapshot_sha256') or 'unknown'}`",
        f"- 环境：`{report.get('environment', {}).get('os', 'unknown')}` · Python `{report.get('environment', {}).get('python', 'unknown')}`",
        "",
        f"- URL：`{report['url']}`",
        f"- 请求：`{report['requests']}` · 并发：`{report['concurrency']}`",
        f"- 成功：`{report['successes']}` · 失败：`{report['errors']}` · 错误率：`{report['error_rate']:.2%}`",
        f"- 吞吐：`{report['throughput_per_second']}` req/s",
        "",
        "| 指标 | 毫秒 |",
        "| --- | ---: |",
        f"| P50 | {latency['p50']} |",
        f"| P95 | {latency['p95']} |",
        f"| P99 | {latency['p99']} |",
        f"| Max | {latency['max']} |",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--path", default="/api/health")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--max-p95-ms", type=float, default=0.0, help="0 表示不设置延迟门禁")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")
    url = args.url.rstrip("/") + "/" + args.path.lstrip("/")
    report = run_probe(url, requests=args.requests, concurrency=args.concurrency, timeout=args.timeout)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "api-load.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "api-load.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"error_rate": report["error_rate"], "p95_ms": report["latency_ms"]["p95"], "output_dir": str(output_dir)}, ensure_ascii=False))
    if report["error_rate"] > args.max_error_rate:
        return 1
    if args.max_p95_ms and report["latency_ms"]["p95"] > args.max_p95_ms:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
