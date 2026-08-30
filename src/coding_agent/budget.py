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
    input_price_per_million_usd: float | None = None
    output_price_per_million_usd: float | None = None
    cost_limit_usd: float | None = None
    session_cost_limit_usd: float | None = None
    max_steps: int | None = None
    clock: Callable[[], float] = monotonic
    started_at: str = ""
    started_tick: float | None = None
    consumed_tokens: int = 0
    session_consumed_tokens: int = 0
    consumed_cost_usd: float = 0.0
    session_consumed_cost_usd: float = 0.0
    cost_estimated: bool = False
    session_cost_estimated: bool = False
    # Time already spent before a process restart or approval recovery.  It is
    # deliberately kept out of the public snapshot; ``elapsed_seconds`` is
    # still the stable persisted fact used to restore it.
    elapsed_offset_seconds: float = 0.0

    def __post_init__(self) -> None:
        prices = (
            self.input_price_per_million_usd,
            self.output_price_per_million_usd,
        )
        if any(value is not None for value in prices) and not all(
            value is not None for value in prices
        ):
            raise ValueError("Both input and output Token prices must be configured")
        if (self.cost_limit_usd is not None or self.session_cost_limit_usd is not None) and not all(
            value is not None for value in prices
        ):
            raise ValueError("Cost budgets require input and output Token prices")

    def start(self, *, max_steps: int | None = None) -> None:
        self.started_at = datetime.now(UTC).isoformat()
        self.started_tick = self.clock()
        self.consumed_tokens = 0
        self.consumed_cost_usd = 0.0
        self.cost_estimated = False
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
        consumed_cost = snapshot.get("consumed_cost_usd", 0.0)
        try:
            consumed_cost_value = float(consumed_cost)
        except (TypeError, ValueError):
            consumed_cost_value = 0.0
        self.consumed_cost_usd = max(0.0, consumed_cost_value)
        self.cost_estimated = bool(snapshot.get("cost_estimated", False))
        session_consumed = snapshot.get("session_consumed_tokens", 0)
        try:
            session_value = int(session_consumed)
        except (TypeError, ValueError):
            session_value = 0
        self.session_consumed_tokens = max(0, session_value)
        session_cost = snapshot.get("session_consumed_cost_usd", 0.0)
        try:
            session_cost_value = float(session_cost)
        except (TypeError, ValueError):
            session_cost_value = 0.0
        self.session_consumed_cost_usd = max(0.0, session_cost_value)
        self.session_cost_estimated = bool(
            snapshot.get("session_cost_estimated", False)
        )

    def sync_session_usage(self, total_tokens: int) -> int:
        """Record the monotonic total usage observed for the current session."""
        self.session_consumed_tokens = max(
            self.session_consumed_tokens, max(0, int(total_tokens))
        )
        return self.session_consumed_tokens

    def sync_session_cost(self, usage: dict[str, Any]) -> float:
        cost, estimated = self.estimate_usage_cost(usage)
        self.session_consumed_cost_usd = max(
            self.session_consumed_cost_usd, cost
        )
        self.session_cost_estimated = self.session_cost_estimated or estimated
        return self.session_consumed_cost_usd

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

    @property
    def remaining_tokens(self) -> int | None:
        if self.token_limit is None:
            return None
        return max(0, self.token_limit - self.consumed_tokens)

    @property
    def remaining_session_tokens(self) -> int | None:
        if self.session_token_limit is None:
            return None
        return max(0, self.session_token_limit - self.session_consumed_tokens)

    @property
    def output_token_ceiling(self) -> int | None:
        """Upper-bound one completion by the smallest remaining Token budget."""
        limits = [
            value
            for value in (self.remaining_tokens, self.remaining_session_tokens)
            if value is not None
        ]
        cost_ceiling = self._remaining_cost_output_tokens()
        if cost_ceiling is not None:
            limits.append(cost_ceiling)
        return min(limits) if limits else None

    def record_usage(self, usage: dict[str, Any]) -> int:
        total = _usage_total(usage)
        self.consumed_tokens += max(0, total)
        cost, estimated = self.estimate_usage_cost(usage)
        self.consumed_cost_usd += cost
        self.cost_estimated = self.cost_estimated or estimated
        return self.consumed_tokens

    def estimate_usage_cost(self, usage: dict[str, Any]) -> tuple[float, bool]:
        input_price = self.input_price_per_million_usd
        output_price = self.output_price_per_million_usd
        if input_price is None or output_price is None:
            return 0.0, False
        prompt = _non_negative_int(usage.get("prompt_tokens"))
        completion = _non_negative_int(usage.get("completion_tokens"))
        if prompt is not None and completion is not None:
            cost = (prompt * input_price + completion * output_price) / 1_000_000
            return cost, False
        total = _non_negative_int(usage.get("total_tokens"))
        if total is not None:
            # Missing split usage cannot be priced exactly. Use the higher
            # configured rate so a cost guard never silently under-counts.
            return total * max(input_price, output_price) / 1_000_000, True
        return 0.0, True

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

    def check_costs(self) -> None:
        if (
            self.cost_limit_usd is not None
            and self.consumed_cost_usd >= self.cost_limit_usd
        ):
            raise BudgetExceeded(
                "COST_BUDGET_EXHAUSTED",
                f"Run used ${self.consumed_cost_usd:.6f} of ${self.cost_limit_usd:.6f} allowed cost",
            )
        if (
            self.session_cost_limit_usd is not None
            and self.session_consumed_cost_usd >= self.session_cost_limit_usd
        ):
            raise BudgetExceeded(
                "SESSION_COST_BUDGET_EXHAUSTED",
                f"Session used ${self.session_consumed_cost_usd:.6f} of ${self.session_cost_limit_usd:.6f} allowed cost",
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
            "remaining_tokens": self.remaining_tokens,
            "session_consumed_tokens": self.session_consumed_tokens,
            "session_token_limit": self.session_token_limit,
            "remaining_session_tokens": self.remaining_session_tokens,
            "consumed_cost_usd": round(self.consumed_cost_usd, 8),
            "cost_limit_usd": self.cost_limit_usd,
            "cost_estimated": self.cost_estimated,
            "session_consumed_cost_usd": round(
                self.session_consumed_cost_usd, 8
            ),
            "session_cost_limit_usd": self.session_cost_limit_usd,
            "session_cost_estimated": self.session_cost_estimated,
            "input_price_per_million_usd": self.input_price_per_million_usd,
            "output_price_per_million_usd": self.output_price_per_million_usd,
            "max_steps": self.max_steps,
        }

    def _remaining_cost_output_tokens(self) -> int | None:
        price = self.output_price_per_million_usd
        if price is None or price <= 0:
            return None
        remaining_costs = [
            max(0.0, limit - consumed)
            for limit, consumed in (
                (self.cost_limit_usd, self.consumed_cost_usd),
                (
                    self.session_cost_limit_usd,
                    self.session_consumed_cost_usd,
                ),
            )
            if limit is not None
        ]
        if not remaining_costs:
            return None
        return max(1, int(min(remaining_costs) * 1_000_000 / price))


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


def _usage_total(usage: dict[str, Any]) -> int:
    total = _non_negative_int(usage.get("total_tokens"))
    if total is not None:
        return total
    return (_non_negative_int(usage.get("prompt_tokens")) or 0) + (
        _non_negative_int(usage.get("completion_tokens")) or 0
    )
