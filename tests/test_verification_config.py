from __future__ import annotations

import json
from pathlib import Path

from coding_agent.verification_config import VerificationConfig


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

