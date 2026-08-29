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
