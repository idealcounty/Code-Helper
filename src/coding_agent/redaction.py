"""Best-effort redaction for durable Agent telemetry and tool artifacts."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from typing import Any


REDACTED = "[REDACTED]"


class Redactor:
    """Redact common credentials while preserving the surrounding structure.

    This is an audit-safety boundary, not a secret detector. Callers should
    still avoid putting credentials in prompts or command lines.
    """

    _secret_key = re.compile(
        r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|secret|password|credential|private[_-]?key)",
        re.IGNORECASE,
    )
    _patterns = (
        (
            re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
            rf"\1{REDACTED}",
        ),
        (
            re.compile(
                r"(?i)(api[_-]?key|access[_-]?token|secret|password|credential)\s*[:=]\s*(['\"]?)[^\s,'\"}}]+\2"
            ),
            rf"\1={REDACTED}",
        ),
        (
            re.compile(r"\b(?:sk|rk|ghp|github_pat|xox[baprs])-[-_A-Za-z0-9]{12,}\b"),
            REDACTED,
        ),
        (
            re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            REDACTED,
        ),
        (
            re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
            REDACTED,
        ),
        (
            re.compile(r"\b(?:sk-ant|glpat|npm|hf|sbp)_[A-Za-z0-9_-]{20,}\b"),
            REDACTED,
        ),
        (
            re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
            REDACTED,
        ),
        (
            re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s]+@"),
            rf"\1{REDACTED}@",
        ),
    )

    def __init__(self, secret_values: Iterable[str] | None = None) -> None:
        self._secret_values = tuple(
            sorted(
                {
                    value
                    for value in (secret_values or ())
                    if isinstance(value, str) and len(value) >= 6
                },
                key=len,
                reverse=True,
            )
        )

    def redact(self, value: Any, *, _key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): (
                    REDACTED
                    if self._secret_key.search(str(key))
                    and not isinstance(item, (Mapping, list, tuple, set))
                    else self.redact(item, _key=str(key))
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item, _key=_key) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item, _key=_key) for item in value)
        if isinstance(value, set):
            return {self.redact(item, _key=_key) for item in value}
        if isinstance(value, str):
            if _key and self._secret_key.search(_key):
                return REDACTED
            return self.redact_text(value)
        return copy.deepcopy(value)

    def redact_text(self, text: str) -> str:
        result = text
        for secret in self._secret_values:
            result = result.replace(secret, REDACTED)
        for pattern, replacement in self._patterns:
            result = pattern.sub(replacement, result)
        return result
