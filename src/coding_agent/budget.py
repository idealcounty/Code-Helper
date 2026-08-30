from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Callable


class BudgetExceeded(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class RunBudget:
    """Observable per-turn wall-time, Step, and provider-reported Token budget."""

    max_seconds: float | None = None
    token_limit: int | None = None
    session_token_limit: int | None = None
    max_steps: int | None = None
    clock: Callable[[], float] = monotonic
    started_at: str = ""
    started_tick: float | None = None
    consumed_tokens: int = 0
    session_consumed_tokens: int = 0
    # Time already spent before a process restart or approval recovery.  It is
    # deliberately kept out of the public snapshot; ``elapsed_seconds`` is
    # still the stable persisted fact used to restore it.
    elapsed_offset_seconds: float = 0.0

    def start(self, *, max_steps: int | None = None) -> None:
        self.started_at = datetime.now(UTC).isoformat()
        self.started_tick = self.clock()
        self.consumed_tokens = 0
        self.elapsed_offset_seconds = 0.0
        if self.max_steps is None:
            self.max_steps = max_steps

    def resume(self, snapshot: dict[str, Any]) -> None:
        """Resume a persisted budget without expanding its configured limits.

        This is used when an approval or interrupted tool is continued after a
        process restart. The configured ``max_*`` values remain authoritative;
        only already-consumed usage and elapsed time are restored from the
        durable snapshot.
        """
        if not isinstance(snapshot, dict):
            self.start()
            return
        started_at = snapshot.get("started_at")
        self.started_at = str(started_at) if started_at else datetime.now(UTC).isoformat()
        self.started_tick = self.clock()
        elapsed = snapshot.get("elapsed_seconds", 0.0)
        try:
            elapsed_value = float(elapsed)
        except (TypeError, ValueError):
            elapsed_value = 0.0
        self.elapsed_offset_seconds = max(0.0, elapsed_value)
        consumed = snapshot.get("consumed_tokens", 0)
        try:
            consumed_value = int(consumed)
        except (TypeError, ValueError):
            consumed_value = 0
        self.consumed_tokens = max(0, consumed_value)
        session_consumed = snapshot.get("session_consumed_tokens", 0)
        try:
            session_value = int(session_consumed)
        except (TypeError, ValueError):
            session_value = 0
        self.session_consumed_tokens = max(0, session_value)

    def sync_session_usage(self, total_tokens: int) -> int:
        """Record the monotonic total usage observed for the current session."""
        self.session_consumed_tokens = max(
            self.session_consumed_tokens, max(0, int(total_tokens))
        )
        return self.session_consumed_tokens

    @property
    def active(self) -> bool:
        return self.started_tick is not None

    @property
    def elapsed_seconds(self) -> float:
        if self.started_tick is None:
            return 0.0
        return max(
            0.0,
            self.elapsed_offset_seconds + self.clock() - self.started_tick,
        )

    @property
    def remaining_seconds(self) -> float | None:
        if self.max_seconds is None:
            return None
        return max(0.0, self.max_seconds - self.elapsed_seconds)

    def record_usage(self, usage: dict[str, Any]) -> int:
        total = usage.get("total_tokens")
        if not isinstance(total, int):
            prompt = usage.get("prompt_tokens", 0)
            completion = usage.get("completion_tokens", 0)
            total = (prompt if isinstance(prompt, int) else 0) + (
                completion if isinstance(completion, int) else 0
            )
        self.consumed_tokens += max(0, total)
        return self.consumed_tokens

    def check_time(self) -> None:
        if self.max_seconds is not None and self.elapsed_seconds >= self.max_seconds:
            raise BudgetExceeded(
                "TIME_BUDGET_EXHAUSTED",
                f"Run exceeded its {self.max_seconds:g} second wall-time budget",
            )

    def check_tokens(self) -> None:
        if self.token_limit is not None and self.consumed_tokens >= self.token_limit:
            raise BudgetExceeded(
                "TOKEN_BUDGET_EXHAUSTED",
                f"Run used {self.consumed_tokens} of {self.token_limit} allowed tokens",
            )

    def check_session_tokens(self) -> None:
        if (
            self.session_token_limit is not None
            and self.session_consumed_tokens >= self.session_token_limit
        ):
            raise BudgetExceeded(
                "SESSION_TOKEN_BUDGET_EXHAUSTED",
                f"Session used {self.session_consumed_tokens} of {self.session_token_limit} allowed tokens",
            )

    def check_step(self, next_step: int) -> None:
        if self.max_steps is not None and next_step > self.max_steps:
            raise BudgetExceeded(
                "STEP_BUDGET_EXHAUSTED",
                f"Run reached its {self.max_steps} Step limit",
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "max_seconds": self.max_seconds,
            "remaining_seconds": (
                round(self.remaining_seconds, 3)
                if self.remaining_seconds is not None
                else None
            ),
            "consumed_tokens": self.consumed_tokens,
            "token_limit": self.token_limit,
            "session_consumed_tokens": self.session_consumed_tokens,
            "session_token_limit": self.session_token_limit,
            "max_steps": self.max_steps,
        }
