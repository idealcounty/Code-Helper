"""Exercise isolated Agent sessions concurrently with a deterministic model.

This is a safe local load test: each worker receives a temporary workspace and
an in-process ScriptedModel that immediately returns a final answer.  It does
not call DeepSeek, write to a user workspace, or require a paid API key.
"""

from __future__ import annotations

import argparse
import concurrent.futures
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

from coding_agent.config import AppConfig
from coding_agent.model import ModelResponse
from coding_agent.web.app import create_app


class ScriptedModel:
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        del messages, tools, reasoning_effort
        return ModelResponse(content="concurrency smoke completed")


def run_one(index: int, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="code-helper-concurrency-") as directory:
        app = create_app(
            AppConfig(api_key="test-key", base_url="https://example.invalid/v1"),
            model_client_factory=ScriptedModel,
        )
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            created = client.post("/api/sessions", json={"workspace": directory, "mode": "ask"})
            if created.status_code != 200:
                return {"index": index, "ok": False, "error": f"create:{created.status_code}"}
            session_id = created.json()["session_id"]
            sent = client.post(f"/api/sessions/{session_id}/messages", json={"content": "smoke"})
            if sent.status_code != 202:
                return {"index": index, "ok": False, "error": f"message:{sent.status_code}"}
            deadline = time.monotonic() + timeout
            details = client.get(f"/api/sessions/{session_id}").json()
            while details.get("running") and time.monotonic() < deadline:
                time.sleep(0.01)
                details = client.get(f"/api/sessions/{session_id}").json()
            events = client.get(f"/api/sessions/{session_id}/events").json()
            event_session_mismatch = any(
                str(event.get("session_id") or session_id) != session_id
                for event in events
                if isinstance(event, dict)
            )
            ok = not details.get("running") and details.get("status") == "completed" and not event_session_mismatch
            error = None if ok else f"status={details.get('status')} running={details.get('running')}"
            return {
                "index": index,
                "ok": ok,
                "session_id": session_id,
                "event_count": len(events),
                "event_session_mismatch": event_session_mismatch,
                "error": error,
            }


def run_probe(sessions: int, concurrency: int, timeout: float) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(lambda index: run_one(index, timeout), range(max(0, sessions))))
    wall_duration_ms = round((time.perf_counter() - started) * 1000, 3)
    successes = sum(bool(item.get("ok")) for item in results)
    mismatches = sum(bool(item.get("event_session_mismatch")) for item in results)
    report = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "sessions": len(results),
        "concurrency": max(1, concurrency),
        "successes": successes,
        "failures": len(results) - successes,
        "completion_rate": round(successes / len(results), 6) if results else 0.0,
        "event_session_mismatches": mismatches,
        "wall_duration_ms": wall_duration_ms,
        "throughput_per_second": round(len(results) / max(wall_duration_ms / 1000, 0.001), 3),
        "results": results,
    }
    report.update(collect_metadata())
    return report


def render_markdown(report: dict[str, Any]) -> str:
    return "\n".join([
        "# Agent 并发会话探针",
        "",
        "> 每个会话使用临时工作区和 ScriptedModel，不访问真实模型。",
        "",
        f"- Git Commit：`{report.get('git_commit') or 'unknown'}` · 工作区修改：`{'是' if report.get('git_dirty') else '否'}`",
        f"- 工作区快照 SHA-256：`{report.get('git_snapshot_sha256') or 'unknown'}`",
        f"- 环境：`{report.get('environment', {}).get('os', 'unknown')}` · Python `{report.get('environment', {}).get('python', 'unknown')}`",
        "",
        f"- 会话：`{report['sessions']}` · 并发：`{report['concurrency']}`",
        f"- 完成：`{report['successes']}` · 失败：`{report['failures']}` · 完成率：`{report['completion_rate']:.2%}`",
        f"- 事件串扰：`{report['event_session_mismatches']}`",
        f"- 总耗时：`{report['wall_duration_ms']}` ms · 吞吐：`{report['throughput_per_second']}` sessions/s",
        "",
        "| 会话 | 状态 | 事件数 | 错误 |",
        "| ---: | --- | ---: | --- |",
        *[
            f"| {item['index']} | {'PASS' if item['ok'] else 'FAIL'} | {item.get('event_count', '—')} | {item.get('error') or '—'} |"
            for item in report["results"]
        ],
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.sessions < 1 or args.concurrency < 1:
        parser.error("--sessions and --concurrency must be positive")
    report = run_probe(args.sessions, args.concurrency, args.timeout)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "agent-concurrency.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "agent-concurrency.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"completion_rate": report["completion_rate"], "mismatches": report["event_session_mismatches"]}, ensure_ascii=False))
    return 0 if report["failures"] == 0 and report["event_session_mismatches"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
