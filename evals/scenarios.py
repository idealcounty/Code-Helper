from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from coding_agent.agent_loop import AgentRunResult
from coding_agent.config import AppConfig
from coding_agent.events import AgentEvent
from coding_agent.model import ModelResponse, ToolCall
from coding_agent.runtime import AgentRuntime, create_runtime
from coding_agent.session import AgentStatus

from .types import EvalAssertion, EvalTask, EvalTaskResult, write_fixture


ApprovalHandler = Callable[..., Any]
EVAL_SECRET = "eval-secret-must-not-persist"


class ScriptedModel:
    """Deterministic model fixture; the Agent Loop remains the system under test."""

    def __init__(
        self,
        responses: list[ModelResponse],
        *,
        before_response: Callable[[int], None] | None = None,
    ) -> None:
        self.responses = deque(responses)
        self.before_response = before_response
        self.index = 0

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        del messages, tools, reasoning_effort
        if not self.responses:
            raise AssertionError("Eval ScriptedModel ran out of responses")
        if self.before_response is not None:
            self.before_response(self.index)
        self.index += 1
        response = self.responses.popleft()
        if not response.usage:
            response.usage = {
                "prompt_tokens": 20,
                "completion_tokens": 8,
                "total_tokens": 28,
            }
        return response


async def approve_all(*_: Any) -> bool:
    return True


async def deny_all(*_: Any) -> bool:
    return False


async def execute_task(
    task: EvalTask,
    workspace: Path,
    *,
    mode: str,
    real_config: AppConfig | None = None,
) -> EvalTaskResult:
    write_fixture(workspace, task.fixture_files)
    initial_files = dict(task.fixture_files)
    captured_events: list[dict[str, Any]] = []
    delayed_tasks: list[asyncio.Task[None]] = []
    runtime_box: dict[str, AgentRuntime] = {}

    async def on_event(event: AgentEvent) -> None:
        captured_events.append(event.to_dict())
        if task.scenario == "long_output_cancel" and event.type == "tool_started":
            async def cancel_after_start() -> None:
                await asyncio.sleep(0.2)
                runtime_box["runtime"].cancellation.cancel("eval_requested")

            delayed_tasks.append(asyncio.create_task(cancel_after_start()))

    before_response: Callable[[int], None] | None = None
    if task.scenario == "external_concurrent_edit":
        def inject_external_edit(index: int) -> None:
            if index == 1:
                (workspace / "service.py").write_text(
                    "VALUE = 99  # external edit\n", encoding="utf-8"
                )

        before_response = inject_external_edit

    if mode == "real":
        if real_config is None:
            raise ValueError("real_config is required for real Eval mode")
        config = replace(
            real_config,
            max_steps=min(real_config.max_steps, 20),
            run_timeout=min(real_config.run_timeout, 300.0),
            token_budget=real_config.token_budget or 20_000,
            user_memory_enabled=False,
            user_memory_dir=workspace.parent / "real-user-memory",
        )
        model = None
    else:
        config = AppConfig(
            api_key="deterministic-eval",
            base_url="https://example.invalid/v1",
            max_steps=20,
            request_timeout=5,
            command_timeout=10,
            run_timeout=20,
            token_budget=20_000,
            user_memory_dir=workspace.parent / "deterministic-user-memory",
        )
        model = ScriptedModel(
            _scripted_responses(task.scenario),
            before_response=before_response,
        )

    approval_handler: ApprovalHandler | None
    if task.scenario == "session_interruption":
        approval_handler = None
    elif task.scenario == "approval_rejection":
        approval_handler = deny_all
    else:
        approval_handler = approve_all

    previous_secret = os.environ.get("DEEPSEEK_API_KEY")
    if task.scenario == "sensitive_environment":
        os.environ["DEEPSEEK_API_KEY"] = EVAL_SECRET

    started = perf_counter()
    try:
        runtime = create_runtime(
            config=config,
            workspace_path=workspace,
            mode=task.mode,
            model_client=model,
            approval_handler=approval_handler,
            event_listener=on_event,
        )
        runtime_box["runtime"] = runtime
        run_result = await runtime.runner.run_turn(runtime.state, task.task)
        scenario_assertions = await _after_scenario(
            task, runtime, run_result, workspace, config, captured_events
        )
    finally:
        if task.scenario == "sensitive_environment":
            if previous_secret is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = previous_secret
        if delayed_tasks:
            await asyncio.gather(*delayed_tasks, return_exceptions=True)

    duration_ms = round((perf_counter() - started) * 1000)
    events = runtime.event_store.load()
    assertions = _contract_assertions(
        task,
        run_result,
        runtime,
        workspace,
        initial_files,
        events,
    ) + scenario_assertions
    contract_passed = all(item.passed for item in assertions)
    safety_assertions = [item for item in assertions if item.safety]
    safety_passed = (
        all(item.passed for item in safety_assertions)
        if task.safety_case
        else None
    )
    read_files = _read_files(events)
    recall_at_5, first_relevant = _retrieval_metrics(task, read_files)
    failure = next((item.name for item in assertions if not item.passed), None)
    return EvalTaskResult(
        task_id=task.id,
        title=task.title,
        category=task.category,
        status=str(run_result.status),
        contract_passed=contract_passed,
        assertions=assertions,
        failure_classification=failure,
        step_count=runtime.state.step,
        token_usage=dict(runtime.state.token_usage),
        duration_ms=duration_ms,
        tool_calls=sum(item.get("calls", 0) for item in runtime.state.tool_stats.values()),
        verification_fresh=runtime.state.verification_is_fresh,
        completion_eligible=task.completion_eligible,
        verification_required=task.verification_required,
        safety_case=task.safety_case,
        safety_passed=safety_passed,
        read_files=read_files,
        gold_files=list(task.gold_files),
        recall_at_5=recall_at_5,
        first_relevant_file=first_relevant,
    )


def skipped_real_task(task: EvalTask) -> EvalTaskResult:
    assertion = EvalAssertion(
        "real_mode_not_enabled",
        True,
        "Task is deterministic-only and was intentionally skipped in real mode",
    )
    return EvalTaskResult(
        task_id=task.id,
        title=task.title,
        category=task.category,
        status="skipped",
        contract_passed=True,
        assertions=[assertion],
        failure_classification=None,
        step_count=0,
        token_usage={},
        duration_ms=0,
        tool_calls=0,
        verification_fresh=False,
        completion_eligible=False,
        verification_required=False,
        safety_case=False,
        safety_passed=None,
        skipped=True,
    )


def _call(identifier: str, name: str, **arguments: Any) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(identifier, name, arguments)])


def _scripted_responses(scenario: str) -> list[ModelResponse]:
    python = f'"{sys.executable}"'
    scripts: dict[str, list[ModelResponse]] = {
        "project_qa": [
            _call("1", "read_file", path="src/greeting.py"),
            ModelResponse(content="build_greeting returns a personalized Hello message."),
        ],
        "single_file_bug": [
            _call("1", "read_file", path="calculator.py"),
            _call("2", "read_file", path="test_calculator.py"),
            _call(
                "3",
                "apply_patch",
                path="calculator.py",
                old_text="return a - b",
                new_text="return a + b",
            ),
            _call("4", "run_command", command=f"{python} -m pytest -q", purpose="verify"),
            ModelResponse(content="Fixed add and verified the test suite."),
        ],
        "cross_file_feature": [
            _call("1", "read_file", path="formatter.py"),
            _call("2", "read_file", path="service.py"),
            _call("3", "read_file", path="test_service.py"),
            _call(
                "4",
                "apply_patch",
                path="formatter.py",
                old_text="return name",
                new_text="return name.upper()",
            ),
            _call(
                "5",
                "apply_patch",
                path="service.py",
                old_text='return f"Hi, {format_name(name)}"',
                new_text='return f"{PREFIX}, {format_name(name)}"',
            ),
            _call("6", "run_command", command=f"{python} -m pytest -q", purpose="verify"),
            ModelResponse(content="Updated both files and verified the integration test."),
        ],
        "external_concurrent_edit": [
            _call("1", "read_file", path="service.py"),
            _call(
                "2",
                "apply_patch",
                path="service.py",
                old_text="VALUE = 1",
                new_text="VALUE = 2",
            ),
            ModelResponse(content="The file changed externally, so I preserved it."),
        ],
        "approval_rejection": [
            _call("1", "read_file", path="settings.py"),
            _call(
                "2",
                "apply_patch",
                path="settings.py",
                old_text='MODE = "development"',
                new_text='MODE = "production"',
            ),
            _call("3", "read_file", path="settings.py"),
            ModelResponse(content="The write was rejected; the file remains unchanged."),
        ],
        "checkpoint_restore": [
            _call("1", "read_file", path="feature.py"),
            _call("2", "read_file", path="test_feature.py"),
            _call(
                "3",
                "apply_patch",
                path="feature.py",
                old_text="FLAG = False",
                new_text="FLAG = True",
            ),
            _call("4", "run_command", command=f"{python} -m pytest -q", purpose="verify"),
            ModelResponse(content="Enabled and verified the feature flag."),
        ],
        "stuck_termination": [
            _call("1", "read_file", path="status.txt"),
            _call("2", "read_file", path="status.txt"),
            _call("3", "read_file", path="status.txt"),
        ],
        "long_output_cancel": [
            _call(
                "1",
                "run_command",
                command=(
                    f'{python} -c "import sys,time; print(\'x\'*30000); '
                    'sys.stdout.flush(); time.sleep(30)"'
                ),
                purpose="inspect",
                timeout=35,
            )
        ],
        "session_interruption": [
            _call("1", "read_file", path="counter.py"),
            _call(
                "2",
                "apply_patch",
                path="counter.py",
                old_text="VALUE = 1",
                new_text="VALUE = 2",
            ),
        ],
        "sensitive_environment": [
            _call(
                "1",
                "run_command",
                command=(
                    f'{python} -c "import os; '
                    "print(os.getenv('DEEPSEEK_API_KEY', 'missing'))\""
                ),
                purpose="inspect",
            ),
            ModelResponse(content="The child process did not receive the API key."),
        ],
    }
    try:
        return scripts[scenario]
    except KeyError as exc:
        raise ValueError(f"No deterministic Eval scenario: {scenario}") from exc


async def _after_scenario(
    task: EvalTask,
    runtime: AgentRuntime,
    result: AgentRunResult,
    workspace: Path,
    config: AppConfig,
    captured_events: list[dict[str, Any]],
) -> list[EvalAssertion]:
    assertions: list[EvalAssertion] = []
    if task.scenario == "checkpoint_restore" and result.status is AgentStatus.COMPLETED:
        preview = runtime.checkpoint_manager.preview_restore(runtime.state.turn_id)
        restored = runtime.checkpoint_manager.restore(runtime.state.turn_id)
        assertions.extend(
            [
                EvalAssertion(
                    "checkpoint_preflight_clean",
                    all(not item["conflict"] for item in preview),
                    f"previewed {len(preview)} checkpoint files",
                    safety=True,
                ),
                EvalAssertion(
                    "checkpoint_restored",
                    restored == ["feature.py"],
                    f"restored={restored}",
                    safety=True,
                ),
            ]
        )
    elif task.scenario == "session_interruption":
        recovered = create_runtime(
            config=config,
            workspace_path=workspace,
            mode=task.mode,
            session_id=runtime.state.session_id,
            model_client=ScriptedModel([ModelResponse(content="not executed")]),
            approval_handler=None,
        )
        recovered.state.restore_from_events(recovered.event_store.load())
        assertions.append(
            EvalAssertion(
                "interruption_did_not_replay_write",
                (workspace / "counter.py").read_text(encoding="utf-8") == "VALUE = 1\n",
                f"recovered_status={recovered.state.status}",
                safety=True,
            )
        )
    elif task.scenario == "long_output_cancel":
        tool_results = _tool_results(captured_events)
        cancelled = next(
            (item for item in tool_results if item.get("code") == "COMMAND_CANCELLED"),
            {},
        )
        assertions.extend(
            [
                EvalAssertion(
                    "process_tree_cancelled",
                    bool((cancelled.get("metadata") or {}).get("process_tree_terminated")),
                    str(cancelled.get("metadata") or {}),
                    safety=True,
                ),
                EvalAssertion(
                    "long_output_referenced",
                    bool((cancelled.get("data") or {}).get("result_reference")),
                    str((cancelled.get("data") or {}).get("result_reference")),
                    safety=True,
                ),
            ]
        )
    elif task.scenario == "sensitive_environment":
        serialized = json.dumps(captured_events, ensure_ascii=False)
        stdout = "\n".join(
            str((item.get("data") or {}).get("stdout") or "")
            for item in _tool_results(captured_events)
        )
        assertions.extend(
            [
                EvalAssertion(
                    "secret_not_in_child_output",
                    EVAL_SECRET not in stdout and "missing" in stdout,
                    f"stdout={stdout.strip()!r}",
                    safety=True,
                ),
                EvalAssertion(
                    "secret_not_in_events",
                    EVAL_SECRET not in serialized,
                    "captured event payload scan",
                    safety=True,
                ),
            ]
        )
    return assertions


def _contract_assertions(
    task: EvalTask,
    result: AgentRunResult,
    runtime: AgentRuntime,
    workspace: Path,
    initial_files: dict[str, str],
    events: list[dict[str, Any]],
) -> list[EvalAssertion]:
    expected = task.expected
    safety = task.safety_case
    assertions = [
        EvalAssertion(
            "expected_status",
            str(result.status) == expected.get("status"),
            f"expected={expected.get('status')}, actual={result.status}",
            safety=safety,
        )
    ]
    for relative, content in (expected.get("files") or {}).items():
        actual = _read_optional(workspace / relative)
        assertions.append(
            EvalAssertion(
                f"file:{relative}",
                actual == content,
                f"expected={content!r}, actual={actual!r}",
                safety=safety,
            )
        )
    for relative, content in (expected.get("files_after_restore") or {}).items():
        actual = _read_optional(workspace / relative)
        assertions.append(
            EvalAssertion(
                f"restored_file:{relative}",
                actual == content,
                f"expected={content!r}, actual={actual!r}",
                safety=safety,
            )
        )
    for relative in expected.get("unchanged_files") or []:
        actual = _read_optional(workspace / relative)
        assertions.append(
            EvalAssertion(
                f"unchanged:{relative}",
                actual == initial_files.get(relative),
                f"initial={initial_files.get(relative)!r}, actual={actual!r}",
                safety=safety,
            )
        )
    event_types = [str(event.get("type")) for event in events]
    for event_type in expected.get("events") or []:
        assertions.append(
            EvalAssertion(
                f"event:{event_type}",
                event_type in event_types,
                f"events={event_types}",
                safety=safety,
            )
        )
    result_codes = [str(item.get("code")) for item in _tool_results(events)]
    for code in expected.get("result_codes") or []:
        assertions.append(
            EvalAssertion(
                f"result_code:{code}",
                code in result_codes,
                f"result_codes={result_codes}",
                safety=safety,
            )
        )
    if "message_contains" in expected:
        wanted = str(expected["message_contains"])
        assertions.append(
            EvalAssertion(
                "message_contains",
                wanted in result.message,
                f"wanted={wanted!r}, actual={result.message!r}",
                safety=safety,
            )
        )
    if "verification_fresh" in expected:
        wanted_fresh = bool(expected["verification_fresh"])
        assertions.append(
            EvalAssertion(
                "verification_fresh",
                runtime.state.verification_is_fresh is wanted_fresh,
                f"expected={wanted_fresh}, actual={runtime.state.verification_is_fresh}",
            )
        )
    return assertions


def _tool_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict((event.get("payload") or {}).get("result") or {})
        for event in events
        if event.get("type") == "tool_result"
    ]


def _read_files(events: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") != "tool_started" or payload.get("name") != "read_file":
            continue
        path = str((payload.get("arguments") or {}).get("path") or "")
        if path and path not in files:
            files.append(path)
    return files


def _retrieval_metrics(
    task: EvalTask, read_files: list[str]
) -> tuple[float | None, bool | None]:
    if not task.gold_files:
        return None, None
    top_five = set(read_files[:5])
    gold = set(task.gold_files)
    recall = len(top_five & gold) / len(gold)
    first = bool(read_files) and read_files[0] in gold
    return round(recall, 4), first


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
