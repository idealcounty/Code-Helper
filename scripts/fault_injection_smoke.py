"""Run deterministic failure-injection checks at the model, tool and boundary layers.

All inputs are synthetic and local.  The probe deliberately injects failures
that have previously caused agent hangs or protocol errors, then verifies a
stable error/repair outcome and writes only redacted metadata.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

try:
    from scripts.evidence_metadata import collect_metadata
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from evidence_metadata import collect_metadata

from coding_agent.hooks import HookManager
from coding_agent.model import ModelError, OpenAICompatibleModelClient
from coding_agent.tool_executor import ToolExecutor
from coding_agent.tools import ToolRegistry, Workspace, register_shell_tools
from coding_agent.tools.base import ToolResult, ToolRisk, ToolSpec, ToolError


def _stream_argument_recovery() -> dict[str, Any]:
    requests: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(bool(body.get("stream")))
        if body.get("stream"):
            payload = (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_bad",'
                '"function":{"name":"read_file","arguments":"{bad-json"}}]},'
                '"finish_reason":"tool_calls"}]}\n'
                "data: [DONE]\n"
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload.encode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-retry",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"app.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    client = OpenAICompatibleModelClient(
        api_key="synthetic-fault-key",
        base_url="https://example.invalid/v1",
        model="scripted",
        transport=httpx.MockTransport(handler),
    )
    response = asyncio.run(
        client.complete_stream(messages=[], tools=[], on_delta=lambda _: None)
    )
    passed = requests == [True, False] and response.tool_calls[0].id == "call-retry"
    return {"name": "malformed_stream_tool_call", "passed": passed, "requests": requests}


def _model_429() -> dict[str, Any]:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "synthetic rate limit"}})

    client = OpenAICompatibleModelClient(
        api_key="synthetic-fault-key",
        base_url="https://example.invalid/v1",
        model="scripted",
        transport=httpx.MockTransport(handler),
    )
    error = ""
    try:
        asyncio.run(client.complete(messages=[], tools=[]))
    except ModelError as exc:
        error = str(exc)
    passed = "HTTP 429" in error and "synthetic-fault-key" not in error
    return {"name": "model_http_429", "passed": passed, "error_code": "HTTP_429" if passed else "unexpected"}


def _path_boundary(root: Path) -> dict[str, Any]:
    workspace = Workspace(root)
    code = ""
    try:
        workspace.resolve("../outside.txt", must_exist=False)
    except ToolError as exc:
        code = exc.code
    return {"name": "workspace_boundary", "passed": code == "PATH_OUTSIDE_WORKSPACE", "error_code": code}


def _command_timeout(root: Path) -> dict[str, Any]:
    registry = ToolRegistry()
    register_shell_tools(registry, Workspace(root), default_timeout=2.0)
    executor = ToolExecutor(registry)
    result = asyncio.run(
        executor.execute(
            "run_command",
            {
                "command": "python -c \"import time; time.sleep(0.5)\"",
                "purpose": "verify",
                "timeout": 0.08,
            },
        )
    )
    return {
        "name": "command_timeout",
        "passed": result.code == "COMMAND_TIMEOUT",
        "error_code": result.code,
        "termination": result.metadata.get("termination"),
    }


def _post_hook_failure(root: Path) -> dict[str, Any]:
    async def handler(_: dict[str, Any]) -> ToolResult:
        return ToolResult.success("ok")

    async def exploding_hook(_: str, __: dict[str, Any], ___: ToolResult) -> ToolResult:
        raise RuntimeError("synthetic post-hook fault")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "probe",
            "synthetic probe",
            {"type": "object", "properties": {}, "additionalProperties": False},
            ToolRisk.READ,
            handler,
        )
    )
    executor = ToolExecutor(registry, hooks=HookManager(post=[exploding_hook]))
    result = asyncio.run(executor.execute("probe", {}))
    return {"name": "post_hook_failure", "passed": result.code == "HOOK_FAILED", "error_code": result.code}


def run_probe() -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="code-helper-faults-") as directory:
        root = Path(directory)
        checks = [
            _stream_argument_recovery(),
            _model_429(),
            _path_boundary(root),
            _command_timeout(root),
            _post_hook_failure(root),
        ]
    report = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "wall_duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "checks": checks,
        "passed": sum(bool(item["passed"]) for item in checks),
        "failed": sum(not bool(item["passed"]) for item in checks),
    }
    report.update(collect_metadata())
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 故障注入与恢复探针",
        "",
        "> 仅使用合成输入；验证协议错误、限流、路径越界、命令超时和 Hook 异常均能稳定收敛。",
        "",
        f"- Git Commit：`{report.get('git_commit') or 'unknown'}`",
        f"- 工作区快照 SHA-256：`{report.get('git_snapshot_sha256') or 'unknown'}`",
        f"- 耗时：`{report['wall_duration_ms']}` ms",
        "",
        "| 故障场景 | 结果 | 错误码/附加证据 |",
        "| --- | --- | --- |",
    ]
    for item in report["checks"]:
        evidence = item.get("error_code") or str(item.get("requests") or item.get("termination") or "—")
        lines.append(f"| `{item['name']}` | {'PASS' if item['passed'] else 'FAIL'} | `{evidence}` |")
    lines.extend(["", f"结论：**{'PASS' if report['failed'] == 0 else 'FAIL'}**（{report['passed']}/{len(report['checks'])}）", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_probe()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "fault-injection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "fault-injection.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "failed": report["failed"], "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
