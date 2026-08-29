"""Explicit task profiles layered on top of the shared Agent Loop."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskProfile:
    name: str
    prompt_addendum: str
    retrieval_strategy: str
    planning_policy: str
    verification_policy: str
    allowed_tools: frozenset[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.allowed_tools is not None:
            data["allowed_tools"] = sorted(self.allowed_tools)
        return data


PROJECT_PROFILE = TaskProfile(
    name="project",
    prompt_addendum=(
        "Project profile: inspect repository structure, make the smallest safe change, "
        "and verify the affected behavior."
    ),
    retrieval_strategy="repo_map",
    planning_policy="impact_then_edit",
    verification_policy="affected_tests_then_broader",
)

ALGORITHM_PROFILE = TaskProfile(
    name="algorithm",
    prompt_addendum=(
        "Algorithm profile: extract input/output, constraints and edge cases; state "
        "complexity; implement and verify with examples, boundaries and reproducible tests."
    ),
    retrieval_strategy="题目与当前文件",
    planning_policy="model_then_implement",
    verification_policy="sample_boundary_random",
    # A profile may narrow tools, but it never grants capabilities.
    allowed_tools=frozenset(
        {
            "read_file",
            "list_files",
            "search_files",
            "search_text",
            "apply_patch",
            "write_file",
            "run_command",
            "judge_algorithm",
            "update_plan",
            "get_diff",
        }
    ),
)

PROFILES = {item.name: item for item in (PROJECT_PROFILE, ALGORITHM_PROFILE)}
_ALGORITHM_MARKERS = re.compile(
    r"(leetcode|算法|复杂度|时间复杂度|空间复杂度|输入输出|数据范围|边界条件|对拍|随机测试|样例)",
    re.IGNORECASE,
)


def classify_task(text: str) -> str:
    """Conservative classifier; ambiguous requests stay in project mode."""
    normalized = str(text or "")
    matches = len(_ALGORITHM_MARKERS.findall(normalized))
    return "algorithm" if matches >= 2 else "project"


def resolve_profile(requested: str | None, text: str) -> TaskProfile:
    value = str(requested or "auto").strip().lower()
    if value == "auto":
        value = classify_task(text)
    return PROFILES.get(value, PROJECT_PROFILE)


def get_profile(name: str | None) -> TaskProfile:
    return PROFILES.get(str(name or "project").lower(), PROJECT_PROFILE)
