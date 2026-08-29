from __future__ import annotations

import random

from .judge import JudgeCase


def integer_cases(
    *, seed: int, count: int, low: int = -100, high: int = 100
) -> list[JudgeCase]:
    """Generate reproducible one-integer cases for simple algorithm demos."""
    rng = random.Random(seed)
    return [
        JudgeCase(str(value), "", f"random-{index}")
        for index, value in enumerate(rng.randint(low, high) for _ in range(max(0, count)))
    ]
