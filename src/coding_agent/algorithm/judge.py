from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class JudgeCase:
    input_data: str
    expected_output: str
    label: str = "case"

    def to_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "input": self.input_data,
            "expected": self.expected_output,
        }


@dataclass(frozen=True, slots=True)
class JudgeCaseResult:
    label: str
    status: str
    expected_output: str
    actual_output: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "status": self.status,
            "expected": self.expected_output,
            "actual": self.actual_output,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class JudgeReport:
    seed: int
    total: int
    passed: int
    failed: int
    cases: tuple[JudgeCaseResult, ...] = ()
    first_failure: dict[str, str] | None = None
    minimized_input: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.total > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "ok": self.ok,
            "cases": [item.to_dict() for item in self.cases],
            "first_failure": self.first_failure,
            "minimized_input": self.minimized_input,
        }


class AlgorithmJudge:
    """Compare deterministic candidate outputs with normalized expected outputs."""

    def __init__(self, *, seed: int = 0) -> None:
        self.seed = int(seed)

    def evaluate(
        self,
        cases: Iterable[JudgeCase],
        outputs: Iterable[tuple[str, str, str | None]],
    ) -> JudgeReport:
        results: list[JudgeCaseResult] = []
        for case, output in zip(cases, outputs):
            actual, status, detail = output
            if status != "ok":
                results.append(
                    JudgeCaseResult(case.label, status, case.expected_output, actual, detail)
                )
                continue
            expected_normalized = normalize_output(case.expected_output)
            actual_normalized = normalize_output(actual)
            passed = expected_normalized == actual_normalized
            results.append(
                JudgeCaseResult(
                    case.label,
                    "passed" if passed else "wrong_answer",
                    expected_normalized,
                    actual_normalized,
                    "" if passed else "normalized output differs",
                )
            )
        passed_count = sum(item.status == "passed" for item in results)
        first = next((item.to_dict() for item in results if item.status != "passed"), None)
        return JudgeReport(
            seed=self.seed,
            total=len(results),
            passed=passed_count,
            failed=len(results) - passed_count,
            cases=tuple(results),
            first_failure=first,
        )

    @staticmethod
    def with_minimized_input(report: JudgeReport, input_data: str) -> JudgeReport:
        return replace(report, minimized_input=str(input_data))


def shrink_input_candidates(input_data: str, *, limit: int = 32) -> list[str]:
    """Generate deterministic smaller inputs for reproducing a failing case."""
    text = str(input_data)
    candidates: list[str] = []
    lines = text.splitlines(keepends=True)
    for index in range(len(lines)):
        candidates.append("".join(lines[:index] + lines[index + 1 :]))
    tokens = re.findall(r"-?\d+|\S+", text)
    for index, token in enumerate(tokens):
        if re.fullmatch(r"-?\d+", token):
            value = int(token)
            replacement = str(value // 2)
            candidates.append(
                " ".join(tokens[:index] + [replacement] + tokens[index + 1 :]) + "\n"
            )
    candidates.extend([text.strip() + "\n", "0\n", ""])
    unique: list[str] = []
    for candidate in candidates:
        if candidate != text and candidate not in unique:
            unique.append(candidate)
        if len(unique) >= limit:
            break
    return unique


def normalize_output(value: str) -> str:
    """Ignore surrounding whitespace and normalize line endings, not answer content."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.strip().split("\n"))
