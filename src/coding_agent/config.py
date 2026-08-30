from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True, slots=True)
class AppConfig:
    api_key: str = field(repr=False)
    provider: str = DEEPSEEK_PROVIDER
    base_url: str = DEEPSEEK_BASE_URL
    model: str = DEEPSEEK_DEFAULT_MODEL
    thinking_mode: str | None = None
    reasoning_effort: str | None = None
    max_steps: int = 20
    request_timeout: float = 120.0
    command_timeout: float = 60.0
    run_timeout: float = 600.0
    token_budget: int | None = None
    session_token_budget: int | None = None
    max_output_tokens: int | None = None
    input_price_per_million_usd: float | None = None
    output_price_per_million_usd: float | None = None
    turn_cost_budget_usd: float | None = None
    session_cost_budget_usd: float | None = None
    result_store_max_bytes: int = 50_000_000
    result_store_max_files: int = 512
    event_store_max_bytes: int = 100_000_000
    event_store_max_files: int = 256
    user_memory_enabled: bool = False
    user_memory_dir: Path | None = None
    default_workspace: Path | None = None
    default_mode: str = "act"
    default_reasoning_profile: str = "auto"
    default_task_profile: str = "auto"
    default_approval_policy: str = "ask"
    default_layout_mode: str = "editor"
    enabled_skills: tuple[str, ...] | None = None
    server_workspace_root: Path | None = None

    def __post_init__(self) -> None:
        prices = (
            self.input_price_per_million_usd,
            self.output_price_per_million_usd,
        )
        if any(value is not None for value in prices) and not all(
            value is not None for value in prices
        ):
            raise ValueError(
                "Input and output prices must be configured together"
            )
        if (
            self.turn_cost_budget_usd is not None
            or self.session_cost_budget_usd is not None
        ) and not all(value is not None for value in prices):
            raise ValueError("Cost budgets require input and output prices")

    @classmethod
    def from_env(cls, settings_path: Path | None = None) -> "AppConfig":
        _load_local_env(Path.cwd() / ".env")
        settings = load_user_settings(settings_path)
        provider = (
            os.getenv("CODE_HELPER_PROVIDER", DEEPSEEK_PROVIDER).strip().lower()
            or DEEPSEEK_PROVIDER
        )
        is_deepseek = provider == DEEPSEEK_PROVIDER
        default_base_url = (
            DEEPSEEK_BASE_URL if is_deepseek else "https://api.openai.com/v1"
        )
        default_model = DEEPSEEK_DEFAULT_MODEL if is_deepseek else "gpt-4.1-mini"
        if "api_key" in settings:
            api_key = str(settings.get("api_key") or "").strip()
        else:
            api_key = os.getenv("CODE_HELPER_API_KEY", "").strip()
        if "api_key" not in settings and not api_key and is_deepseek:
            api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        reasoning_profile = _settings_choice(
            settings, "default_reasoning_profile", {"auto", "fast", "balanced", "deep"}, "auto"
        )
        reasoning_effort = (
            {"fast": "low", "balanced": "medium", "deep": "high"}.get(reasoning_profile)
            if "default_reasoning_profile" in settings
            else _reasoning_effort()
        )
        enabled_skills = settings.get("enabled_skills")
        normalized_skills = (
            tuple(dict.fromkeys(str(item) for item in enabled_skills if str(item).strip()))
            if isinstance(enabled_skills, list)
            else None
        )
        workspace_raw = str(settings.get("default_workspace") or "").strip()
        return cls(
            api_key=api_key,
            provider=provider,
            base_url=os.getenv("CODE_HELPER_BASE_URL", default_base_url).rstrip("/"),
            model=os.getenv("CODE_HELPER_MODEL", default_model),
            thinking_mode=_optional_choice(
                "CODE_HELPER_THINKING_MODE", {"enabled", "disabled"}
            ),
            reasoning_effort=reasoning_effort,
            max_steps=_positive_int("CODE_HELPER_MAX_STEPS", 20),
            request_timeout=_positive_float("CODE_HELPER_REQUEST_TIMEOUT", 120.0),
            command_timeout=_positive_float("CODE_HELPER_COMMAND_TIMEOUT", 60.0),
            run_timeout=_positive_float("CODE_HELPER_RUN_TIMEOUT", 600.0),
            token_budget=_optional_positive_int("CODE_HELPER_TOKEN_BUDGET"),
            session_token_budget=_optional_positive_int(
                "CODE_HELPER_SESSION_TOKEN_BUDGET"
            ),
            max_output_tokens=_optional_positive_int(
                "CODE_HELPER_MAX_OUTPUT_TOKENS"
            ),
            input_price_per_million_usd=_optional_positive_float(
                "CODE_HELPER_INPUT_PRICE_PER_MILLION_USD"
            ),
            output_price_per_million_usd=_optional_positive_float(
                "CODE_HELPER_OUTPUT_PRICE_PER_MILLION_USD"
            ),
            turn_cost_budget_usd=_optional_positive_float(
                "CODE_HELPER_TURN_COST_BUDGET_USD"
            ),
            session_cost_budget_usd=_optional_positive_float(
                "CODE_HELPER_SESSION_COST_BUDGET_USD"
            ),
            result_store_max_bytes=_positive_int(
                "CODE_HELPER_RESULT_STORE_MAX_BYTES", 50_000_000
            ),
            result_store_max_files=_positive_int(
                "CODE_HELPER_RESULT_STORE_MAX_FILES", 512
            ),
            event_store_max_bytes=_positive_int(
                "CODE_HELPER_EVENT_STORE_MAX_BYTES", 100_000_000
            ),
            event_store_max_files=_positive_int(
                "CODE_HELPER_EVENT_STORE_MAX_FILES", 256
            ),
            user_memory_enabled=_boolean("CODE_HELPER_USER_MEMORY_ENABLED", False),
            user_memory_dir=_optional_path("CODE_HELPER_USER_MEMORY_DIR"),
            default_workspace=Path(workspace_raw).expanduser().resolve() if workspace_raw else None,
            default_mode=_settings_choice(settings, "default_mode", {"ask", "plan", "act"}, "act"),
            default_reasoning_profile=reasoning_profile,
            default_task_profile=_settings_choice(
                settings, "default_task_profile", {"auto", "project", "algorithm"}, "auto"
            ),
            default_approval_policy=_settings_choice(
                settings, "default_approval_policy", {"ask", "auto", "full"}, "ask"
            ),
            default_layout_mode=_settings_choice(
                settings, "default_layout_mode", {"editor", "focus"}, "editor"
            ),
            enabled_skills=normalized_skills,
            server_workspace_root=_optional_path("CODE_HELPER_WORKSPACE_ROOT"),
        )


def default_settings_path() -> Path:
    override = os.getenv("CODE_HELPER_SETTINGS_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "CodeHelper" / "settings.json"
    return Path.home() / ".code-helper" / "settings.json"


def load_user_settings(path: Path | None = None) -> dict[str, Any]:
    target = (path or default_settings_path()).expanduser()
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_user_settings(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = (path or default_settings_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def _settings_choice(
    settings: dict[str, Any], name: str, choices: set[str], fallback: str
) -> str:
    value = str(settings.get(name) or fallback).strip().lower()
    return value if value in choices else fallback


def _optional_choice(name: str, choices: set[str]) -> str | None:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return None
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{name} must be one of: {allowed}")
    return value


def _reasoning_effort() -> str | None:
    """Accept user-facing profiles while keeping provider values normalized."""
    value = os.getenv("CODE_HELPER_REASONING_EFFORT", "").strip().lower()
    if not value or value == "auto":
        return None
    profiles = {"fast": "low", "balanced": "medium", "deep": "high"}
    return profiles.get(value, value)


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _optional_positive_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _optional_positive_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1", "yes"}


def _optional_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _load_local_env(path: Path) -> None:
    """Load a minimal KEY=VALUE file without overriding the real environment."""
    if not path.is_file():
        return
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid .env entry at line {line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            raise ValueError(f"Invalid environment name at line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)
