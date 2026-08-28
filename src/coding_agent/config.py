from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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

    @classmethod
    def from_env(cls) -> "AppConfig":
        _load_local_env(Path.cwd() / ".env")
        provider = (
            os.getenv("CODE_HELPER_PROVIDER", DEEPSEEK_PROVIDER).strip().lower()
            or DEEPSEEK_PROVIDER
        )
        is_deepseek = provider == DEEPSEEK_PROVIDER
        default_base_url = (
            DEEPSEEK_BASE_URL if is_deepseek else "https://api.openai.com/v1"
        )
        default_model = DEEPSEEK_DEFAULT_MODEL if is_deepseek else "gpt-4.1-mini"
        api_key = os.getenv("CODE_HELPER_API_KEY", "").strip()
        if not api_key and is_deepseek:
            api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        return cls(
            api_key=api_key,
            provider=provider,
            base_url=os.getenv("CODE_HELPER_BASE_URL", default_base_url).rstrip("/"),
            model=os.getenv("CODE_HELPER_MODEL", default_model),
            thinking_mode=_optional_choice(
                "CODE_HELPER_THINKING_MODE", {"enabled", "disabled"}
            ),
            reasoning_effort=_reasoning_effort(),
            max_steps=_positive_int("CODE_HELPER_MAX_STEPS", 20),
            request_timeout=_positive_float("CODE_HELPER_REQUEST_TIMEOUT", 120.0),
            command_timeout=_positive_float("CODE_HELPER_COMMAND_TIMEOUT", 60.0),
        )


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
