from __future__ import annotations

from pathlib import Path

from coding_agent.config import AppConfig


def test_local_env_is_loaded_without_exposing_key_in_repr(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODE_HELPER_API_KEY", raising=False)
    monkeypatch.delenv("CODE_HELPER_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "CODE_HELPER_API_KEY=private-test-value\nCODE_HELPER_MODEL=test-model\n",
        encoding="utf-8",
    )

    config = AppConfig.from_env()

    assert config.api_key == "private-test-value"
    assert config.model == "test-model"
    assert "private-test-value" not in repr(config)


def test_real_environment_overrides_local_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODE_HELPER_API_KEY", "from-environment")
    (tmp_path / ".env").write_text(
        "CODE_HELPER_API_KEY=from-file\n", encoding="utf-8"
    )

    assert AppConfig.from_env().api_key == "from-environment"
