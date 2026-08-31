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
    "CODE_HELPER_MAX_STEPS",
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
    "CODE_HELPER_SETTINGS_PATH",
    "CODE_HELPER_WORKSPACE_ROOT",
}


@pytest.fixture(autouse=True)
def _isolate_user_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODE_HELPER_SETTINGS_PATH", str(tmp_path / "settings.json"))


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


def test_complex_task_budget_defaults_to_160_steps(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_config_environment(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")

    config = AppConfig.from_env()

    assert config.max_steps == 160
    assert config.run_timeout == 4800.0


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


def test_persisted_ui_settings_override_defaults(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "ui-settings.json"
    settings.write_text(
        '{"api_key":"saved-key","default_workspace":"%s",'
        '"default_mode":"plan","default_reasoning_profile":"balanced",'
        '"default_task_profile":"algorithm","default_approval_policy":"auto",'
        '"default_layout_mode":"focus",'
        '"enabled_skills":["bug-fix"]}' % str(tmp_path).replace("\\", "\\\\"),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODE_HELPER_API_KEY", "environment-key")

    config = AppConfig.from_env(settings)

    assert config.api_key == "saved-key"
    assert config.default_workspace == tmp_path.resolve()
    assert config.default_mode == "plan"
    assert config.reasoning_effort == "medium"
    assert config.default_task_profile == "algorithm"
    assert config.default_approval_policy == "auto"
    assert config.default_layout_mode == "focus"
    assert config.enabled_skills == ("bug-fix",)


def test_server_workspace_root_is_loaded_from_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CODE_HELPER_WORKSPACE_ROOT", str(tmp_path))

    config = AppConfig.from_env()

    assert config.server_workspace_root == tmp_path.resolve()


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
