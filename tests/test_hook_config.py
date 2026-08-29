from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from coding_agent.hook_config import load_hook_config
from coding_agent.hooks import HookManager


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
