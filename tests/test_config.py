from __future__ import annotations

from pathlib import Path

from coding_agent.config import AppConfig


CONFIG_ENV_NAMES = {
    "CODE_HELPER_API_KEY",
    "CODE_HELPER_PROVIDER",
    "CODE_HELPER_BASE_URL",
    "CODE_HELPER_MODEL",
    "CODE_HELPER_THINKING_MODE",
    "CODE_HELPER_REASONING_EFFORT",
    "DEEPSEEK_API_KEY",
}


def _clear_config_environment(monkeypatch) -> None:
    for name in CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_local_env_is_loaded_without_exposing_key_in_repr(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_config_environment(monkeypatch)
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
    _clear_config_environment(monkeypatch)
    monkeypatch.setenv("CODE_HELPER_API_KEY", "from-environment")
    (tmp_path / ".env").write_text(
        "CODE_HELPER_API_KEY=from-file\n", encoding="utf-8"
    )

    assert AppConfig.from_env().api_key == "from-environment"


def test_deepseek_is_the_default_provider_and_accepts_native_key_name(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_config_environment(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")

    config = AppConfig.from_env()

    assert config.api_key == "deepseek-test-key"
    assert config.provider == "deepseek"
    assert config.base_url == "https://api.deepseek.com"
    assert config.model == "deepseek-v4-flash"


def test_deepseek_thinking_settings_are_loaded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_config_environment(monkeypatch)
    monkeypatch.setenv("CODE_HELPER_THINKING_MODE", "enabled")
    monkeypatch.setenv("CODE_HELPER_REASONING_EFFORT", "high")

    config = AppConfig.from_env()

    assert config.thinking_mode == "enabled"
    assert config.reasoning_effort == "high"


def test_reasoning_profiles_are_normalized(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv("CODE_HELPER_REASONING_EFFORT", "deep")
    assert AppConfig.from_env().reasoning_effort == "high"
    monkeypatch.setenv("CODE_HELPER_REASONING_EFFORT", "auto")
    assert AppConfig.from_env().reasoning_effort is None
