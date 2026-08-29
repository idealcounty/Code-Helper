from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProblemSpec:
    """Conservative structure extracted from a natural-language problem statement."""

    title: str = ""
    input_description: str = ""
    output_description: str = ""
    constraints: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    raw_length: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "input_description": self.input_description,
            "output_description": self.output_description,
            "constraints": list(self.constraints),
            "examples": list(self.examples),
            "raw_length": self.raw_length,
        }


def parse_problem(text: str) -> ProblemSpec:
    """Extract common sections without pretending to fully understand the statement."""
    raw = str(text or "").strip()
    sections = _sections(raw)
    title = raw.splitlines()[0].lstrip("# ").strip() if raw else ""
    constraints = tuple(
        line.strip(" -*")
        for line in sections.get("constraints", "").splitlines()
        if line.strip()
    )
    if not constraints:
        constraints = tuple(
            match.group(0).strip()
            for match in re.finditer(r"[^\n]{0,80}(?:<=|≥|>=|范围|约束)[^\n]{0,100}", raw)
        )[:20]
    examples = tuple(
        line.strip()
        for line in sections.get("examples", "").splitlines()
        if line.strip()
    )
    return ProblemSpec(
        title=title[:300],
        input_description=sections.get("input", "")[:2_000],
        output_description=sections.get("output", "")[:2_000],
        constraints=constraints[:20],
        examples=examples[:20],
        raw_length=len(raw),
    )


def _sections(text: str) -> dict[str, str]:
    headings = list(
        re.finditer(
            r"(?im)^\s*(?:#+\s*)?(input|output|constraints?|examples?|输入|输出|约束|限制|示例|样例)\s*:?\s*$",
            text,
        )
    )
    result: dict[str, str] = {}
    aliases = {
        "input": "input", "输入": "input",
        "output": "output", "输出": "output",
        "constraint": "constraints", "constraints": "constraints", "约束": "constraints", "限制": "constraints",
        "example": "examples", "examples": "examples", "示例": "examples", "样例": "examples",
    }
    for index, heading in enumerate(headings):
        key = aliases[heading.group(1).lower()]
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        result[key] = text[heading.end() : end].strip()
    return result
