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
    test_cases: dict[str, int] | None = None
    variables: tuple[dict[str, object], ...] = ()
    aggregate_constraints: tuple[str, ...] = ()
    input_shape: tuple[str, ...] = ()
    output_shape: tuple[str, ...] = ()
    samples: tuple[str, ...] = ()
    confidence: float = 0.0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "input_description": self.input_description,
            "output_description": self.output_description,
            "constraints": list(self.constraints),
            "examples": list(self.examples),
            "raw_length": self.raw_length,
            "test_cases": self.test_cases,
            "variables": list(self.variables),
            "aggregate_constraints": list(self.aggregate_constraints),
            "input_shape": list(self.input_shape),
            "output_shape": list(self.output_shape),
            "samples": list(self.samples),
            "confidence": self.confidence,
            "warnings": list(self.warnings),
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
    variables, aggregate_constraints, test_cases = _extract_constraints(constraints)
    warnings: list[str] = []
    if not constraints:
        warnings.append("未识别到明确约束，边界用例需要人工补充。")
    if not sections.get("input"):
        warnings.append("未找到标准 Input 段落。")
    if not sections.get("output"):
        warnings.append("未找到标准 Output 段落。")
    confidence = 0.2
    confidence += 0.25 if constraints else 0
    confidence += 0.2 if variables else 0
    confidence += 0.15 if sections.get("input") else 0
    confidence += 0.15 if sections.get("output") else 0
    confidence += 0.05 if examples else 0
    return ProblemSpec(
        title=title[:300],
        input_description=sections.get("input", "")[:2_000],
        output_description=sections.get("output", "")[:2_000],
        constraints=constraints[:20],
        examples=examples[:20],
        raw_length=len(raw),
        test_cases=test_cases,
        variables=tuple(variables),
        aggregate_constraints=tuple(aggregate_constraints),
        samples=examples[:20],
        confidence=round(min(confidence, 1.0), 2),
        warnings=tuple(warnings),
    )


def suggest_boundary_cases(spec: ProblemSpec, *, limit: int = 16) -> list[dict[str, object]]:
    """Create conservative scalar boundary suggestions from parsed integer bounds.

    The parser cannot infer a full input grammar, so suggestions are explicitly
    labelled and must be adapted by a human before execution.
    """
    suggestions: list[dict[str, object]] = []
    seen: set[str] = set()
    for variable in spec.variables:
        name = str(variable.get("name") or "value")
        values: list[int] = []
        if isinstance(variable.get("min"), int):
            values.extend([int(variable["min"]), int(variable["min"]) + 1])
        if isinstance(variable.get("max"), int):
            values.extend([int(variable["max"]) - 1, int(variable["max"])])
        for value in values:
            input_data = str(value)
            if input_data in seen:
                continue
            seen.add(input_data)
            suggestions.append({"label": f"boundary-{name}-{value}", "input": input_data, "source": "boundary", "variable": name})
            if len(suggestions) >= max(0, limit):
                return suggestions
    if not suggestions:
        suggestions.append({"label": "boundary-empty", "input": "0", "source": "boundary", "warning": "题面未提供可解析的整数上下界"})
    return suggestions


_BOUND_RE = re.compile(
    r"(?P<name>[A-Za-z_]\w*)\s*(?P<op><=|≤|>=|≥|<|>)\s*(?P<value>[-+]?\d+(?:\s*[⋅xX*]\s*10\s*\^?\s*\d+)?)"
)
_REVERSE_BOUND_RE = re.compile(
    r"(?P<value>[-+]?\d+(?:\s*[⋅xX*]\s*10\s*\^?\s*\d+)?)\s*(?P<op><=|≤|>=|≥|<|>)\s*(?P<name>[A-Za-z_]\w*)"
)


def _extract_constraints(lines: tuple[str, ...]) -> tuple[list[dict[str, object]], list[str], dict[str, int] | None]:
    variables: dict[str, dict[str, object]] = {}
    aggregate: list[str] = []
    test_cases: dict[str, int] | None = None
    for line in lines:
        normalized = " ".join(line.split())
        if "sum" in normalized.casefold() or "总和" in normalized:
            aggregate.append(normalized)
        for match in _REVERSE_BOUND_RE.finditer(normalized):
            value = _parse_bound_number(match.group("value"))
            if value is None:
                continue
            name = match.group("name")
            item = variables.setdefault(name, {"name": name, "type": "integer"})
            op = match.group("op")
            if op in {"<=", "≤", "<"}:
                item["min"] = value + (1 if op == "<" else 0)
            else:
                item["max"] = value - (1 if op == ">" else 0)
        for match in _BOUND_RE.finditer(normalized):
            name = match.group("name")
            value = _parse_bound_number(match.group("value"))
            if value is None:
                continue
            item = variables.setdefault(name, {"name": name, "type": "integer"})
            op = match.group("op")
            if op in {"<=", "≤", "<"}:
                item["max"] = value - (1 if op == "<" else 0)
            else:
                item["min"] = value + (1 if op == ">" else 0)
            if name.casefold() in {"t", "test", "tests"}:
                test_cases = test_cases or {}
                if "min" in item:
                    test_cases["min"] = int(item["min"])
                if "max" in item:
                    test_cases["max"] = int(item["max"])
    return list(variables.values()), list(dict.fromkeys(aggregate)), test_cases


def _parse_bound_number(value: str) -> int | None:
    compact = re.sub(r"\s+", "", value).replace("⋅", "*").lower()
    match = re.fullmatch(r"([+-]?\d+)(?:\*x?10\^?([+-]?\d+))?", compact)
    if not match:
        return None
    try:
        base = int(match.group(1))
        exponent = int(match.group(2) or 0)
        return base * (10 ** exponent)
    except (OverflowError, ValueError):
        return None


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
