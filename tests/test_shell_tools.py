from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from coding_agent.tool_executor import ToolExecutor
from coding_agent.cancellation import CancellationToken
from coding_agent.tools import ToolRegistry, Workspace, register_shell_tools


def test_command_does_not_inherit_api_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODE_HELPER_API_KEY", "must-not-reach-child")
    registry = ToolRegistry()
    register_shell_tools(registry, Workspace(tmp_path), default_timeout=10)
    executor = ToolExecutor(registry)
    command = (
        f'"{sys.executable}" -c "import os; '
        "print(os.getenv('CODE_HELPER_API_KEY', 'not-present'))\""
    )

    result = asyncio.run(
        executor.execute(
            "run_command", {"command": command, "purpose": "inspect"}
        )
    )

    assert result.ok is True
    assert result.data["stdout"].strip() == "not-present"
    assert "must-not-reach-child" not in result.data["stdout"]


def test_cancelled_command_terminates_child_process_tree(tmp_path: Path) -> None:
    (tmp_path / "child.py").write_text(
        "import time\nfrom pathlib import Path\ntime.sleep(1)\nPath('child-finished').write_text('unexpected')\n",
        encoding="utf-8",
    )
    (tmp_path / "parent.py").write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "subprocess.Popen([sys.executable, 'child.py'])\n"
        "Path('parent-started').write_text('ready')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    cancellation = CancellationToken()
    registry = ToolRegistry()
    register_shell_tools(
        registry,
        Workspace(tmp_path),
        default_timeout=10,
        cancellation=cancellation,
    )
    executor = ToolExecutor(registry)

    async def scenario():
        task = asyncio.create_task(
            executor.execute(
                "run_command",
                {
                    "command": f'"{sys.executable}" parent.py',
                    "purpose": "inspect",
                },
            )
        )
        for _ in range(100):
            if (tmp_path / "parent-started").exists():
                break
            await asyncio.sleep(0.02)
        assert (tmp_path / "parent-started").exists()
        cancellation.cancel("test_cancel")
        result = await asyncio.wait_for(task, timeout=5)
        await asyncio.sleep(1.2)
        return result

    result = asyncio.run(scenario())

    assert result.code == "COMMAND_CANCELLED"
    assert result.metadata["termination"] == "cancelled"
    assert result.metadata["process_tree_terminated"] is True
    assert not (tmp_path / "child-finished").exists()


def test_cancelled_long_output_keeps_result_reference(tmp_path: Path) -> None:
    cancellation = CancellationToken()
    registry = ToolRegistry()
    register_shell_tools(
        registry,
        Workspace(tmp_path),
        default_timeout=10,
        cancellation=cancellation,
    )
    result_store = tmp_path / "tool-results"
    executor = ToolExecutor(registry, result_store=result_store)
    # Keep the child alive well beyond cancellation, and publish a readiness
    # marker only after the large payload has been flushed.  A fixed sleep is
    # racy on a busy Windows event loop (especially under coverage): the test
    # can cancel before the reader has consumed any output and then incorrectly
    # conclude that cancellation lost the result reference.
    code = (
        "import time; from pathlib import Path; "
        "print('x'*20000, flush=True); Path('output-ready').write_text('ready'); "
        "time.sleep(120)"
    )

    async def scenario():
        task = asyncio.create_task(
            executor.execute(
                "run_command",
                {
                    "argv": [sys.executable, "-c", code],
                    "purpose": "inspect",
                },
            )
        )
        for _ in range(250):
            if (tmp_path / "output-ready").exists():
                break
            await asyncio.sleep(0.02)
        assert (tmp_path / "output-ready").exists()
        cancellation.cancel("test_cancel")
        return await asyncio.wait_for(task, timeout=5)

    result = asyncio.run(scenario())

    assert result.code == "COMMAND_CANCELLED"
    assert result.data.get("result_reference")
    references = list(result_store.glob("tool-result-*.json"))
    assert len(references) == 1
    assert len(references[0].read_text(encoding="utf-8")) > 12_000


def test_command_streams_stdout_and_stderr_deltas(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_shell_tools(registry, Workspace(tmp_path), default_timeout=10)
    executor = ToolExecutor(registry)
    deltas: list[tuple[str, str]] = []

    async def on_output(stream: str, content: str) -> None:
        deltas.append((stream, content))

    code = "import sys; print('out'); print('err', file=sys.stderr)"
    command = f'"{sys.executable}" -c "{code}"'
    result = asyncio.run(
        executor.execute(
            "run_command",
            {"command": command, "purpose": "inspect"},
            output_callback=on_output,
        )
    )

    assert result.ok is True
    assert result.metadata["output_streamed"] is True
    assert "out" in "".join(content for stream, content in deltas if stream == "stdout")
    assert "err" in "".join(content for stream, content in deltas if stream == "stderr")


def test_structured_argv_runs_without_shell_interpretation(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_shell_tools(registry, Workspace(tmp_path), default_timeout=10)
    result = asyncio.run(
        ToolExecutor(registry).execute(
            "run_command",
            {
                "argv": [sys.executable, "-c", "print('argv-ok')"],
                "purpose": "inspect",
            },
        )
    )

    assert result.ok is True
    assert result.data["stdout"].strip() == "argv-ok"
    assert result.metadata["execution_mode"] == "argv"


def test_structured_argv_does_not_expand_shell_metacharacters(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_shell_tools(registry, Workspace(tmp_path), default_timeout=10)
    result = asyncio.run(
        ToolExecutor(registry).execute(
            "run_command",
            {
                "argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", "a && echo injected"],
                "purpose": "inspect",
            },
        )
    )

    assert result.ok is True
    assert result.data["stdout"].strip() == "a && echo injected"


def test_command_requires_exactly_one_invocation_form(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_shell_tools(registry, Workspace(tmp_path), default_timeout=10)
    executor = ToolExecutor(registry)

    both = asyncio.run(
        executor.execute(
            "run_command",
            {"command": "echo no", "argv": ["echo", "no"], "purpose": "inspect"},
        )
    )
    neither = asyncio.run(executor.execute("run_command", {"purpose": "inspect"}))

    assert both.code == "INVALID_ARGUMENTS"
    assert neither.code == "INVALID_ARGUMENTS"
