from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class EvalTask:
    id: str
    title: str
    category: str
    scenario: str
    task: str
    mode: str
    fixture_files: dict[str, str]
    expected: dict[str, Any]
    gold_files: tuple[str, ...] = ()
    gold_symbols: tuple[str, ...] = ()
    completion_eligible: bool = False
    verification_required: bool = False
    safety_case: bool = False
    real_enabled: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvalTask":
        fixture = payload.get("fixture") or {}
        gold = payload.get("gold") or {}
        metrics = payload.get("metrics") or {}
        return cls(
            id=str(payload["id"]),
            title=str(payload["title"]),
            category=str(payload["category"]),
            scenario=str(payload["scenario"]),
            task=str(payload["task"]),
            mode=str(payload.get("mode", "act")),
            fixture_files={
                str(path): str(content)
                for path, content in (fixture.get("files") or {}).items()
            },
            expected=dict(payload.get("expected") or {}),
            gold_files=tuple(map(str, gold.get("files") or [])),
            gold_symbols=tuple(map(str, gold.get("symbols") or [])),
            completion_eligible=bool(metrics.get("completion_eligible", False)),
            verification_required=bool(metrics.get("verification_required", False)),
            safety_case=bool(metrics.get("safety_case", False)),
            real_enabled=bool(payload.get("real_enabled", False)),
        )


@dataclass(frozen=True, slots=True)
class EvalAssertion:
    name: str
    passed: bool
    detail: str
    safety: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "safety": self.safety,
        }


@dataclass(slots=True)
class EvalTaskResult:
    task_id: str
    title: str
    category: str
    status: str
    contract_passed: bool
    assertions: list[EvalAssertion]
    failure_classification: str | None
    step_count: int
    token_usage: dict[str, int]
    duration_ms: int
    tool_calls: int
    verification_fresh: bool
    completion_eligible: bool
    verification_required: bool
    safety_case: bool
    safety_passed: bool | None
    read_files: list[str] = field(default_factory=list)
    gold_files: list[str] = field(default_factory=list)
    recall_at_5: float | None = None
    first_relevant_file: bool | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "category": self.category,
            "status": self.status,
            "contract_passed": self.contract_passed,
            "assertions": [item.to_dict() for item in self.assertions],
            "failure_classification": self.failure_classification,
            "step_count": self.step_count,
            "token_usage": self.token_usage,
            "duration_ms": self.duration_ms,
            "tool_calls": self.tool_calls,
            "verification_fresh": self.verification_fresh,
            "completion_eligible": self.completion_eligible,
            "verification_required": self.verification_required,
            "safety_case": self.safety_case,
            "safety_passed": self.safety_passed,
            "read_files": self.read_files,
            "gold_files": self.gold_files,
            "recall_at_5": self.recall_at_5,
            "first_relevant_file": self.first_relevant_file,
            "skipped": self.skipped,
        }


def write_fixture(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            raise ValueError(f"Eval fixture escapes its workspace: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
