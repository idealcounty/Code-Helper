import pytest

from coding_agent.budget import BudgetExceeded, RunBudget


def test_resume_preserves_elapsed_time_and_tokens_without_expanding_limits() -> None:
    now = [100.0]
    budget = RunBudget(
        max_seconds=10.0,
        token_limit=100,
        max_steps=4,
        clock=lambda: now[0],
    )

    budget.resume(
        {
            "started_at": "2026-08-30T10:00:00+00:00",
            "elapsed_seconds": 8.5,
            "consumed_tokens": 90,
            "max_seconds": 9999,
            "token_limit": 9999,
        }
    )

    assert budget.started_at == "2026-08-30T10:00:00+00:00"
    assert budget.elapsed_seconds == 8.5
    assert budget.consumed_tokens == 90
    assert budget.max_seconds == 10.0
    assert budget.token_limit == 100
    now[0] += 1.0
    assert budget.elapsed_seconds == 9.5

    budget.record_usage({"total_tokens": 10})
    with pytest.raises(BudgetExceeded) as error:
        budget.check_tokens()
    assert error.value.code == "TOKEN_BUDGET_EXHAUSTED"


def test_resume_elapsed_budget_expires_before_new_model_request() -> None:
    budget = RunBudget(max_seconds=5.0, clock=lambda: 1.0)
    budget.resume({"elapsed_seconds": 5.0, "consumed_tokens": 0})

    with pytest.raises(BudgetExceeded) as error:
        budget.check_time()
    assert error.value.code == "TIME_BUDGET_EXHAUSTED"


def test_session_token_budget_is_independent_from_per_turn_budget() -> None:
    budget = RunBudget(token_limit=100, session_token_limit=150)
    budget.start()
    budget.sync_session_usage(150)

    with pytest.raises(BudgetExceeded) as error:
        budget.check_session_tokens()
    assert error.value.code == "SESSION_TOKEN_BUDGET_EXHAUSTED"
    assert budget.consumed_tokens == 0
    assert budget.session_consumed_tokens == 150


def test_output_token_ceiling_tracks_smallest_remaining_budget() -> None:
    budget = RunBudget(token_limit=100, session_token_limit=80)
    budget.start()

    assert budget.output_token_ceiling == 80

    budget.record_usage({"total_tokens": 30})
    budget.sync_session_usage(65)

    assert budget.remaining_tokens == 70
    assert budget.remaining_session_tokens == 15
    assert budget.output_token_ceiling == 15
    snapshot = budget.snapshot()
    assert snapshot["remaining_tokens"] == 70
    assert snapshot["remaining_session_tokens"] == 15


def test_cost_budget_uses_configured_input_and_output_prices() -> None:
    budget = RunBudget(
        input_price_per_million_usd=1.0,
        output_price_per_million_usd=2.0,
        cost_limit_usd=0.005,
    )
    budget.start()

    budget.record_usage({"prompt_tokens": 1_000, "completion_tokens": 2_000})

    assert budget.consumed_cost_usd == pytest.approx(0.005)
    assert budget.cost_estimated is False
    with pytest.raises(BudgetExceeded) as error:
        budget.check_costs()
    assert error.value.code == "COST_BUDGET_EXHAUSTED"


def test_cost_budget_conservatively_prices_usage_without_token_split() -> None:
    budget = RunBudget(
        input_price_per_million_usd=1.0,
        output_price_per_million_usd=3.0,
        session_cost_limit_usd=0.01,
    )

    budget.record_usage({"total_tokens": 1_000})

    assert budget.consumed_cost_usd == pytest.approx(0.003)
    assert budget.cost_estimated is True


def test_remaining_cost_constrains_provider_output_ceiling() -> None:
    budget = RunBudget(
        input_price_per_million_usd=1.0,
        output_price_per_million_usd=2.0,
        cost_limit_usd=0.004,
    )
    budget.start()

    assert budget.output_token_ceiling == 2_000

    budget.record_usage({"prompt_tokens": 1_000, "completion_tokens": 500})

    assert budget.consumed_cost_usd == pytest.approx(0.002)
    assert budget.output_token_ceiling == 1_000
