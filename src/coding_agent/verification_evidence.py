from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import PurePath
from typing import Any, Collection


class VerificationKind(StrEnum):
    TEST = "test"
    BUILD = "build"
    LINT = "lint"
    TYPECHECK = "typecheck"
    COMPILE = "compile"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


class VerificationSource(StrEnum):
    USER_REQUESTED = "user_requested"
    PROJECT_INFERRED = "project_inferred"
    RELATED_TEST_INFERRED = "related_test_inferred"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    command: str
    kind: VerificationKind
    source: VerificationSource
    started_sequence: int
    finished_sequence: int
    exit_code: int | None
    related_files: tuple[str, ...]
    output_summary: str
    passed: bool
    applicable: bool
    accepted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_NON_SUBSTANTIVE = re.compile(
    r"^(?:echo|printf|pwd|cd|dir|ls|type|cat|get-location|write-output)\b",
    re.IGNORECASE,
)
_ALGORITHM_JUDGE = re.compile(r"^judge_algorithm\b", re.IGNORECASE)
_MASKED_FAILURE = re.compile(
    r"(?:\|\||;)\s*(?:true|exit\s+0|echo\b|write-output\b)",
    re.IGNORECASE,
)
_TEST_PATTERNS = (
    re.compile(
        r"^(?:(?:uv|poetry|pipenv)\s+run\s+)?"
        r"(?:python(?:3(?:\.\d+)?)?\s+-m\s+)?pytest\b",
        re.I,
    ),
    re.compile(r"^python(?:3(?:\.\d+)?)?\s+-m\s+unittest\b", re.I),
    re.compile(r"^(?:npm(?:\s+run)?|pnpm|yarn|bun)\s+test\b", re.I),
    re.compile(r"^(?:cargo|go)\s+test\b", re.I),
    re.compile(r"^(?:mvnw?|\.?[\\/]gradlew?|gradle)\b.*\btest\b", re.I),
)
_BUILD_PATTERNS = (
    re.compile(r"^(?:npm\s+run|pnpm|yarn|bun\s+run)\s+build\b", re.I),
    re.compile(r"^python(?:3(?:\.\d+)?)?\s+-m\s+build\b", re.I),
    re.compile(r"^(?:cargo\s+(?:build|check)|go\s+build)\b", re.I),
    re.compile(
        r"^(?:mvnw?|\.?[\\/]gradlew?|gradle)\b.*\b"
        r"(?:package|build|assemble)\b",
        re.I,
    ),
    re.compile(r"^cmake\s+--build\b", re.I),
)
_LINT_PATTERNS = (
    re.compile(r"^(?:ruff(?:\s+check)?|flake8|pylint|eslint|biome\s+check)\b", re.I),
    re.compile(r"^(?:black|isort)\b.*\s--check\b", re.I),
    re.compile(r"^(?:npm\s+run|pnpm|yarn)\s+lint\b", re.I),
)
_TYPECHECK_PATTERNS = (
    re.compile(r"^(?:mypy|pyright)\b", re.I),
    re.compile(r"^(?:npx\s+)?tsc\b.*(?:--noemit|-b)\b", re.I),
    re.compile(r"^(?:npm\s+run|pnpm|yarn)\s+typecheck\b", re.I),
)
_COMPILE_PATTERNS = (
    re.compile(r"^python(?:3(?:\.\d+)?)?\s+-m\s+compileall\b", re.I),
    re.compile(r"^(?:g\+\+|clang\+\+|gcc|clang|javac|dotnet\s+build)\b", re.I),
)
_TARGET_TOKEN = re.compile(
    r"(?P<path>[^\s'\"]+(?:test[^\s'\"]*|spec[^\s'\"]*)\.(?:py|js|jsx|ts|tsx|java|cpp|cc|cxx|go|rs))",
    re.IGNORECASE,
)


def build_verification_evidence(
    *,
    command: str,
    purpose: str,
    result: dict[str, Any],
    objective: str,
    changed_files: set[str],
    started_sequence: int,
    finished_sequence: int,
    project_commands: Collection[str] | None = None,
) -> VerificationEvidence:
    normalized = " ".join(command.strip().split())
    data = result.get("data") or {}
    exit_code = data.get("exit_code")
    if not isinstance(exit_code, int):
        exit_code = None
    output_summary = _output_summary(data)
    explicitly_requested = bool(normalized) and normalized.casefold() in objective.casefold()
    kind = _classify(normalized)
    configured = _matches_project_command(normalized, project_commands)
    if (explicitly_requested or configured) and kind is VerificationKind.UNKNOWN:
        kind = VerificationKind.CUSTOM
    source = _source(kind, explicitly_requested, configured)
    passed = bool(result.get("ok")) and exit_code == 0
    related_files = tuple(sorted(changed_files))

    applicable, reason = _applicability(
        command=normalized,
        purpose=purpose,
        kind=kind,
        source=source,
        configured=configured,
        changed_files=related_files,
    )
    accepted = passed and applicable
    if applicable and not passed:
        reason = f"Recognized {kind.value} verification failed with exit code {exit_code}"

    return VerificationEvidence(
        command=normalized,
        kind=kind,
        source=source,
        started_sequence=started_sequence,
        finished_sequence=finished_sequence,
        exit_code=exit_code,
        related_files=related_files,
        output_summary=output_summary,
        passed=passed,
        applicable=applicable,
        accepted=accepted,
        reason=reason,
    )


def _classify(command: str) -> VerificationKind:
    if (
        not command
        or _NON_SUBSTANTIVE.match(command)
        or _MASKED_FAILURE.search(command)
    ):
        return VerificationKind.UNKNOWN
    if _ALGORITHM_JUDGE.match(command):
        return VerificationKind.CUSTOM
    classification_view = re.sub(
        r'^(?:"[^"\r\n]*[\\/]python(?:\.exe)?"|[^\s]*[\\/]python(?:\.exe)?)\s+',
        "python ",
        command,
        count=1,
        flags=re.IGNORECASE,
    )
    segments = [
        part.strip()
        for part in re.split(r"\s*(?:&&|;)\s*", classification_view)
        if part.strip()
    ]
    for kind, patterns in (
        (VerificationKind.TEST, _TEST_PATTERNS),
        (VerificationKind.BUILD, _BUILD_PATTERNS),
        (VerificationKind.LINT, _LINT_PATTERNS),
        (VerificationKind.TYPECHECK, _TYPECHECK_PATTERNS),
        (VerificationKind.COMPILE, _COMPILE_PATTERNS),
    ):
        if any(pattern.search(segment) for segment in segments for pattern in patterns):
            return kind
    return VerificationKind.UNKNOWN


def _source(
    kind: VerificationKind, explicitly_requested: bool, configured: bool = False
) -> VerificationSource:
    if explicitly_requested:
        return VerificationSource.USER_REQUESTED
    if configured:
        return VerificationSource.PROJECT_INFERRED
    if kind is VerificationKind.TEST:
        return VerificationSource.RELATED_TEST_INFERRED
    if kind is not VerificationKind.UNKNOWN:
        return VerificationSource.PROJECT_INFERRED
    return VerificationSource.UNTRUSTED


def _applicability(
    *,
    command: str,
    purpose: str,
    kind: VerificationKind,
    source: VerificationSource,
    configured: bool = False,
    changed_files: tuple[str, ...],
) -> tuple[bool, str]:
    if purpose != "verify":
        return False, "Command was not requested as verification"
    if _NON_SUBSTANTIVE.match(command):
        return False, "Informational shell commands are not verification"
    if _MASKED_FAILURE.search(command):
        return False, "Command can mask a failed verification exit status"
    if kind is VerificationKind.UNKNOWN and source is not VerificationSource.USER_REQUESTED:
        return False, "Unknown command is not trusted only because purpose='verify'"
    if source is VerificationSource.USER_REQUESTED:
        return True, "The user explicitly requested this substantive verification command"
    if configured:
        return True, "The command is listed in the workspace verification configuration"

    targets = [match.group("path") for match in _TARGET_TOKEN.finditer(command)]
    if kind is VerificationKind.TEST and targets and len(changed_files) > 1:
        target_stems = {
            PurePath(target.replace("\\", "/")).stem.casefold()
            for target in targets
        }
        uncovered = [
            path
            for path in changed_files
            if not any(
                PurePath(path.replace("\\", "/")).stem.casefold() in stem
                for stem in target_stems
            )
        ]
        if uncovered:
            return (
                False,
                "Targeted test does not demonstrate coverage for all changed files: "
                + ", ".join(uncovered),
            )
    return True, f"Recognized {kind.value} command with {source.value} scope"


def _matches_project_command(
    command: str, project_commands: Collection[str] | None
) -> bool:
    normalized = " ".join(command.strip().split()).casefold()
    if not normalized or not project_commands:
        return False
    return any(" ".join(str(item).strip().split()).casefold() == normalized for item in project_commands)


def _output_summary(data: dict[str, Any], limit: int = 600) -> str:
    stdout = str(data.get("stdout") or "").strip()
    stderr = str(data.get("stderr") or "").strip()
    combined = "\n".join(part for part in (stdout, stderr) if part)
    if len(combined) <= limit:
        return combined
    return f"{combined[: limit // 2]}\n...<truncated>...\n{combined[-limit // 2 :]}"
