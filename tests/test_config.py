from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.config import AppConfig


CONFIG_ENV_NAMES = {
    "CODE_HELPER_API_KEY",
    "CODE_HELPER_PROVIDER",
    "CODE_HELPER_BASE_URL",
    "CODE_HELPER_MODEL",
    "CODE_HELPER_THINKING_MODE",
    "CODE_HELPER_REASONING_EFFORT",
    "CODE_HELPER_RUN_TIMEOUT",
    "CODE_HELPER_TOKEN_BUDGET",
    "CODE_HELPER_SESSION_TOKEN_BUDGET",
    "CODE_HELPER_MAX_OUTPUT_TOKENS",
    "CODE_HELPER_INPUT_PRICE_PER_MILLION_USD",
    "CODE_HELPER_OUTPUT_PRICE_PER_MILLION_USD",
    "CODE_HELPER_TURN_COST_BUDGET_USD",
    "CODE_HELPER_SESSION_COST_BUDGET_USD",
    "CODE_HELPER_RESULT_STORE_MAX_BYTES",
    "CODE_HELPER_RESULT_STORE_MAX_FILES",
    "CODE_HELPER_EVENT_STORE_MAX_BYTES",
    "CODE_HELPER_EVENT_STORE_MAX_FILES",
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


def test_run_budget_settings_are_loaded(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv("CODE_HELPER_RUN_TIMEOUT", "45.5")
    monkeypatch.setenv("CODE_HELPER_TOKEN_BUDGET", "12000")
    monkeypatch.setenv("CODE_HELPER_SESSION_TOKEN_BUDGET", "50000")
    monkeypatch.setenv("CODE_HELPER_MAX_OUTPUT_TOKENS", "4096")

    config = AppConfig.from_env()

    assert config.run_timeout == 45.5
    assert config.token_budget == 12000
    assert config.session_token_budget == 50000
    assert config.max_output_tokens == 4096


def test_result_store_limits_are_loaded(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv("CODE_HELPER_RESULT_STORE_MAX_BYTES", "2048")
    monkeypatch.setenv("CODE_HELPER_RESULT_STORE_MAX_FILES", "3")

    config = AppConfig.from_env()

    assert config.result_store_max_bytes == 2048
    assert config.result_store_max_files == 3


def test_cost_budget_settings_require_explicit_provider_prices(monkeypatch) -> None:
    _clear_config_environment(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv("CODE_HELPER_INPUT_PRICE_PER_MILLION_USD", "0.5")
    monkeypatch.setenv("CODE_HELPER_OUTPUT_PRICE_PER_MILLION_USD", "2.0")
    monkeypatch.setenv("CODE_HELPER_TURN_COST_BUDGET_USD", "0.25")
    monkeypatch.setenv("CODE_HELPER_SESSION_COST_BUDGET_USD", "1.5")

    config = AppConfig.from_env()

    assert config.input_price_per_million_usd == 0.5
    assert config.output_price_per_million_usd == 2.0
    assert config.turn_cost_budget_usd == 0.25
    assert config.session_cost_budget_usd == 1.5


def test_cost_budget_without_complete_price_pair_is_rejected(monkeypatch) -> None:
    _clear_config_environment(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv("CODE_HELPER_INPUT_PRICE_PER_MILLION_USD", "0.5")
    monkeypatch.setenv("CODE_HELPER_TURN_COST_BUDGET_USD", "0.25")

    with pytest.raises(ValueError, match="configured together"):
        AppConfig.from_env()


def test_event_store_limits_are_loaded(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    monkeypatch.setenv("CODE_HELPER_EVENT_STORE_MAX_BYTES", "4096")
    monkeypatch.setenv("CODE_HELPER_EVENT_STORE_MAX_FILES", "5")

    config = AppConfig.from_env()

    assert config.event_store_max_bytes == 4096
    assert config.event_store_max_files == 5
