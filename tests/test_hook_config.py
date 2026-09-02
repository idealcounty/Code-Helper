from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from coding_agent.hook_config import load_hook_config
from coding_agent.hooks import ExternalHookSpec, HookDecision, HookManager


def test_loads_restricted_external_hook_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".code-helper"
    config_dir.mkdir()
    (config_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "name": "guard-update-plan",
                        "event": "PreToolUse",
                        "matcher": {"tool": "update_plan"},
                        "argv": [sys.executable, "-c", "print('ok')"],
                        "timeout": 2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    config = load_hook_config(tmp_path)

    assert config.diagnostics == ()
    assert len(config.hooks) == 1
    assert config.hooks[0].event == "pre_tool"
    assert config.hooks[0].matcher == "update_plan"
    assert config.hooks[0].cwd == tmp_path.resolve()


def test_external_pre_hook_can_deny_with_json_decision(tmp_path: Path) -> None:
    script = (
        "import json,sys; json.load(sys.stdin); "
        "print(json.dumps({'allow': False, 'reason': 'external policy', 'code': 'HOOK_DENIED'}))"
    )
    config = load_hook_config_from_specs(tmp_path, sys.executable, script)

    decision = asyncio.run(
        HookManager(external=list(config.hooks)).before(
            "apply_patch", {"path": "sample.py"}
        )
    )

    assert decision.allow is False
    assert decision.reason == "external policy"
    assert decision.hook == "deny-patch"


def load_hook_config_from_specs(root: Path, executable: str, script: str):
    config_dir = root / ".code-helper"
    config_dir.mkdir()
    (config_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "name": "deny-patch",
                        "event": "pre_tool",
                        "matcher": "apply_patch",
                        "argv": [executable, "-c", script],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return load_hook_config(root)


def test_hook_config_reports_malformed_and_unsafe_entries(tmp_path: Path) -> None:
    config_dir = tmp_path / ".code-helper"
    config_dir.mkdir()
    path = config_dir / "hooks.json"
    path.write_text("{not-json", encoding="utf-8")
    assert load_hook_config(tmp_path).diagnostics

    path.write_text(json.dumps({"hooks": "not-a-list"}), encoding="utf-8")
    assert "hooks.json must contain" in load_hook_config(tmp_path).diagnostics[0]

    entries: list[object] = [
        "not-an-object",
        {"event": "unknown", "argv": ["echo"]},
        {"event": "pre_tool", "argv": []},
        {"event": "pre_tool", "argv": ["", "ok"]},
        {"event": "pre_tool", "argv": ["echo", "bad\x00arg"]},
        {"event": "pre_tool", "argv": ["echo"], "timeout": "bad"},
        {"event": "pre_tool", "argv": ["echo"], "timeout": 0.01},
        {"event": "pre_tool", "argv": ["echo"], "timeout": 11},
        {"event": "pre_tool", "argv": ["echo"], "cwd": ""},
        {"event": "pre_tool", "argv": ["echo"], "cwd": ".."},
        {"event": "pre_tool", "argv": ["echo"], "matcher": 3},
        {"event": "pre_tool", "argv": ["echo"], "matcher": {"tool": "update_plan"}},
        {"event": "PostToolUse", "argv": ["echo"]},
        {"event": "OnVerification", "argv": ["echo"]},
        {"event": "OnTaskEnd", "argv": ["echo"]},
        {"event": "pre_tool", "argv": ["echo"], "name": "valid"},
    ]
    path.write_text(json.dumps({"hooks": entries}), encoding="utf-8")
    config = load_hook_config(tmp_path)

    assert len(config.hooks) == 5
    assert config.hooks[-1].name == "valid"
    assert any("only the first" in item for item in config.diagnostics) is False

    path.write_text(json.dumps({"hooks": entries + [{"event": "pre_tool", "argv": ["echo"]} for _ in range(16)]}), encoding="utf-8")
    limited = load_hook_config(tmp_path)
    assert len(limited.hooks) <= 16
    assert any("only the first" in item for item in limited.diagnostics)


def test_hook_config_uses_default_event_name_and_ignores_invalid_matcher_dict(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".code-helper"
    config_dir.mkdir()
    (config_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {"event": "pre_tool", "argv": ["echo"], "matcher": {"other": "x"}},
                    {"event": "pre_tool", "argv": ["echo"], "name": ""},
                ]
            }
        ),
        encoding="utf-8",
    )

    config = load_hook_config(tmp_path)

    assert len(config.hooks) == 2
    assert config.hooks[0].matcher is None
    assert config.hooks[1].name.startswith("external:pre_tool:")


def test_hook_decision_from_result_normalizes_supported_values() -> None:
    original = HookDecision(additional_context="keep", hook="original")

    assert HookDecision.from_result(None, hook="none").hook == "none"
    assert HookDecision.from_result(original, hook="fallback").hook == "original"
    assert HookDecision.from_result(True, hook="true").allow is True
    denied = HookDecision.from_result(False, hook="false")
    assert denied.allow is False and denied.reason == "hook denied"
    mapping = HookDecision.from_result(
        {"allow": False, "reason": "blocked", "additional_context": 7, "code": "X"},
        hook="mapping",
    )
    assert mapping.allow is False
    assert mapping.additional_context == "7"
    assert mapping.code == "X"
    assert HookDecision.from_result(["unsupported"], hook="other").allow is True


def test_external_hook_run_handles_empty_invalid_and_nonzero_output(tmp_path: Path) -> None:
    empty = ExternalHookSpec(
        event="pre_tool", argv=(sys.executable, "-c", "print()"), workspace_root=tmp_path
    )
    assert asyncio.run(empty.run({})).allow is True

    invalid = ExternalHookSpec(
        event="pre_tool", argv=(sys.executable, "-c", "print('not-json')"), workspace_root=tmp_path
    )
    invalid_result = asyncio.run(invalid.run({}))
    assert invalid_result.allow is False and invalid_result.code == "HOOK_FAILED"

    nonzero = ExternalHookSpec(
        event="pre_tool",
        argv=(sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(3)"),
        workspace_root=tmp_path,
    )
    nonzero_result = asyncio.run(nonzero.run({}))
    assert nonzero_result.allow is False and nonzero_result.code == "HOOK_DENIED"


def test_external_hook_timeout_and_matcher_are_deterministic(tmp_path: Path) -> None:
    hook = ExternalHookSpec(
        event="pre_tool",
        argv=(sys.executable, "-c", "import time; time.sleep(1)"),
        workspace_root=tmp_path,
        matcher="apply_patch",
        timeout=0.1,
    )
    assert hook.matches("apply_patch") is True
    assert hook.matches("read_file") is False
    result = asyncio.run(hook.run({}))
    assert result.allow is False and result.code == "HOOK_TIMEOUT"


def test_hook_trace_failures_never_change_tool_hook_result() -> None:
    traces: list[dict[str, object]] = []

    async def trace(payload: dict[str, object]) -> None:
        traces.append(payload)
        raise RuntimeError("trace sink unavailable")

    async def verification(_: dict[str, object]) -> HookDecision:
        return HookDecision(additional_context="keep going")

    manager = HookManager(verification=[verification], trace=trace)
    decisions = asyncio.run(manager.on_verification({"ok": True}))

    assert decisions[0].allow is True
    assert len(traces) == 2
