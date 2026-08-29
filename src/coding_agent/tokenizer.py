"""Optional token estimation for context observability.

The agent keeps its character budget as the deterministic compaction guard.
This module adds a best-effort token estimate when ``tiktoken`` is installed,
without making that optional dependency a runtime requirement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    tokens: int
    backend: str
    exact: bool


class TokenEstimator:
    """Estimate provider prompt tokens and degrade safely when unavailable."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = (model_name or "").strip()
        self._encoding: Any | None = None
        self.backend = "char_proxy"
        self.exact = False
        try:
            import tiktoken  # type: ignore[import-not-found]

            try:
                self._encoding = tiktoken.encoding_for_model(self.model_name)
            except (KeyError, ValueError):
                # DeepSeek-compatible gateways commonly use a cl100k-like
                # tokenizer; label this as an approximation, not exact usage.
                self._encoding = tiktoken.get_encoding("cl100k_base")
            self.backend = f"tiktoken:{getattr(self._encoding, 'name', 'unknown')}"
        except (ImportError, AttributeError, RuntimeError):
            self._encoding = None

    def estimate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> TokenEstimate:
        payload = {"messages": messages, "tools": tools or []}
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if self._encoding is not None:
            try:
                tokens = len(self._encoding.encode(text))
                return TokenEstimate(max(tokens, 0), self.backend, self.exact)
            except (TypeError, ValueError, RuntimeError):
                pass
        # A conservative, deterministic fallback.  Keep at least one token
        # for a non-empty request so UI percentages remain meaningful.
        return TokenEstimate(max(1, (len(text) + 3) // 4) if text else 0, self.backend, False)
