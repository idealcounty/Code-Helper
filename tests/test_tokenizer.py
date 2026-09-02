from __future__ import annotations

from coding_agent.context import ContextManager
from coding_agent.session import AgentState
from coding_agent.tokenizer import TokenEstimator


def test_token_estimator_returns_a_nonzero_observable_estimate() -> None:
    estimate = TokenEstimator("deepseek-v4-flash").estimate(
        [{"role": "user", "content": "Explain this function."}],
        [{"type": "function", "function": {"name": "read_file"}}],
    )

    assert estimate.tokens > 0
    assert estimate.backend
    assert isinstance(estimate.exact, bool)


def test_context_exposes_token_estimate_without_changing_char_budget() -> None:
    context = ContextManager(model_name="deepseek-v4-flash").build(
        AgentState.create(), []
    )

    assert context.estimated_chars > 0
    assert context.estimated_tokens > 0
    assert context.token_estimator


def test_token_estimator_uses_encoding_and_falls_back_when_encoding_fails() -> None:
    class Encoding:
        name = "fake"

        def __init__(self, error: Exception | None = None) -> None:
            self.error = error

        def encode(self, _: str) -> list[int]:
            if self.error:
                raise self.error
            return [1, 2, 3]

    estimator = TokenEstimator("fake")
    estimator._encoding = Encoding()
    estimator.backend = "tiktoken:fake"
    exact = estimator.estimate([{"role": "user", "content": "hello"}])
    assert exact.tokens == 3
    assert exact.backend == "tiktoken:fake"

    estimator._encoding = Encoding(ValueError("broken encoder"))
    fallback = estimator.estimate([{"role": "user", "content": "hello"}])
    assert fallback.tokens > 0
    assert fallback.exact is False
