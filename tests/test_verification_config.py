from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from coding_agent.verification_config import VerificationConfig, VerificationRule


def test_loads_and_normalizes_workspace_commands(tmp_path: Path) -> None:
    config_dir = tmp_path / ".code-helper"
    config_dir.mkdir()
    (config_dir / "verification.json").write_text(
        json.dumps(
            {
                "commands": [
                    "  python -m pytest -q  ",
                    {"name": "strict", "command": "python scripts/check.py"},
                    "PYTHON -M PYTEST -Q",
                ]
            }
        ),
        encoding="utf-8",
    )

    config = VerificationConfig.load(tmp_path)

    assert config.commands == ("python -m pytest -q", "python scripts/check.py")
    assert config.diagnostics == ()
    assert config.matches("python -m pytest -q")
    assert config.matches(" PYTHON   -M   PYTEST -Q ")


def test_invalid_workspace_config_is_safe_and_diagnostic(tmp_path: Path) -> None:
    config_dir = tmp_path / ".code-helper"
    config_dir.mkdir()
    (config_dir / "verification.json").write_text("[]", encoding="utf-8")

    config = VerificationConfig.load(tmp_path)

    assert config.commands == ()
    assert config.diagnostics
    assert not config.matches("python -m pytest")


def test_selects_verification_commands_by_profile_and_observed_path(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".code-helper"
    config_dir.mkdir()
    (config_dir / "verification.json").write_text(
        json.dumps(
            {
                "commands": ["python -m pytest -q"],
                "rules": [
                    {
                        "task_profiles": ["project"],
                        "paths": ["src/api/**"],
                        "commands": ["python scripts/check_api.py"],
                    },
                    {
                        "task_profiles": ["algorithm"],
                        "commands": ["python scripts/check_algorithm.py"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    config = VerificationConfig.load(tmp_path)

    assert config.diagnostics == ()
    assert config.commands_for(
        task_profile="project", paths=("src\\api\\users.py",)
    ) == ("python -m pytest -q", "python scripts/check_api.py")
    assert config.commands_for(
        task_profile="project", paths=("src/core.py",)
    ) == ("python -m pytest -q",)
    assert config.commands_for(task_profile="algorithm") == (
        "python -m pytest -q",
        "python scripts/check_algorithm.py",
    )
    assert config.all_commands == (
        "python -m pytest -q",
        "python scripts/check_api.py",
        "python scripts/check_algorithm.py",
    )


def test_commands_for_state_uses_reducer_tool_evidence() -> None:
    config = VerificationConfig(
        rules=(
            VerificationRule(
                commands=("python scripts/check_api.py",),
                task_profiles=("project",),
                paths=("src/api/**",),
            ),
        )
    )
    state = SimpleNamespace(
        task_profile="project",
        changed_files=set(),
        recent_actions=[
            {
                "result_code": "OK",
                "signature": json.dumps(
                    {
                        "name": "read_file",
                        "arguments": {"path": "src/api/users.py"},
                    }
                )
            }
        ],
    )

    assert config.commands_for_state(state) == ("python scripts/check_api.py",)

    state.recent_actions[0]["result_code"] = "FILE_NOT_FOUND"
    assert config.commands_for_state(state) == ()


def test_invalid_rule_selectors_are_ignored_with_diagnostics(tmp_path: Path) -> None:
    config_dir = tmp_path / ".code-helper"
    config_dir.mkdir()
    (config_dir / "verification.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "paths": ["../outside/**", "C:/absolute/**"],
                        "commands": ["python unsafe.py"],
                    },
                    {
                        "task_profiles": ["unknown"],
                        "paths": ["src/**"],
                        "commands": ["python broadened.py"],
                    },
                    {"commands": ["python unscoped.py"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    config = VerificationConfig.load(tmp_path)

    assert config.rules == ()
    assert any("relative workspace glob" in item for item in config.diagnostics)
    assert any("no valid values" in item for item in config.diagnostics)
    assert any("must select" in item for item in config.diagnostics)
