from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppConfig:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1-mini"
    reasoning_effort: str | None = None
    max_steps: int = 20
    request_timeout: float = 120.0
    command_timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "AppConfig":
        api_key = os.getenv("CODE_HELPER_API_KEY", "").strip()
        return cls(
            api_key=api_key,
            base_url=os.getenv(
                "CODE_HELPER_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/"),
            model=os.getenv("CODE_HELPER_MODEL", "gpt-4.1-mini"),
            reasoning_effort=(
                os.getenv("CODE_HELPER_REASONING_EFFORT", "").strip() or None
            ),
            max_steps=_positive_int("CODE_HELPER_MAX_STEPS", 20),
            request_timeout=_positive_float("CODE_HELPER_REQUEST_TIMEOUT", 120.0),
            command_timeout=_positive_float("CODE_HELPER_COMMAND_TIMEOUT", 60.0),
        )


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

