from __future__ import annotations

import json
from typing import Any


RECOVERABLE_RESULT_CODES = frozenset(
    {
        "NO_CHANGES",
        "EDIT_NOT_FOUND",
        "EDIT_NOT_UNIQUE",
        "FILE_CHANGED",
        "CHECKPOINT_CONFLICT",
        "FILE_EXISTS",
    }
)
RECOVERABLE_TOOL_NAMES = frozenset({"apply_patch", "write_file"})
# A duplicate write is deliberately converted into an observation by the
# runner.  If the model ignores that observation and emits the same write
# again, the loop is still a write loop and should finish as a recoverable
# partial result instead of surfacing the generic AGENT_STUCK failure.
TERMINAL_WRITE_RESULT_CODES = frozenset(
    {"OK", "NO_CHANGES", "DUPLICATE_TOOL_CALL"}
)


class StuckDetector:
    def __init__(self, repeated_action_threshold: int = 3) -> None:
        self.repeated_action_threshold = repeated_action_threshold

    def is_stuck(self, recent_actions: list[dict[str, Any]]) -> bool:
        threshold = self.repeated_action_threshold
        if len(recent_actions) < threshold:
            return False
        window = recent_actions[-threshold:]
        signatures = {
            (
                item.get("signature"),
                item.get("result_code"),
                item.get("result_fingerprint"),
            )
            for item in window
        }
        return len(signatures) == 1

    def recovery_hint(self, recent_actions: list[dict[str, Any]]) -> str | None:
        """Suggest one safe recovery for repeated edit failures.

        Repeated reads are still terminated by the detector. Edit failures are
        different: a previous attempt may have changed the file, or the model
        may be using stale ``old_text``. Give it one bounded chance to re-read
        the file before declaring the run stuck.
        """
        if not self.is_stuck(recent_actions):
            return None
        latest = recent_actions[-1]
        try:
            signature = json.loads(str(latest.get("signature") or "{}"))
        except (TypeError, ValueError):
            return None
        result_code = str(latest.get("result_code") or "")
        name = str(signature.get("name") or "")
        if result_code in RECOVERABLE_RESULT_CODES and name in RECOVERABLE_TOOL_NAMES:
            return (
                "The same edit failed repeatedly. Do not repeat the identical tool call. "
                "Re-read the target file to refresh its current contents, then choose a "
                "corrected edit; if the requested change is already present, explain that "
                "no further write is needed."
            )
        # A successful mutation can still be a model-side loop (for example,
        # the same patch is emitted after every round). Give the model one
        # bounded chance to observe the current state before terminating. For
        # repeated reads, terminate immediately: there is no new state to
        # discover and deterministic stuck evals must remain bounded.
        if name in RECOVERABLE_TOOL_NAMES:
            return (
                "The identical tool call produced the same result repeatedly. Do not "
                "repeat it again; inspect the latest state, choose a different next "
                "action, or finish if the task is already satisfied."
            )
        return None

    def is_successful_write_loop(self, recent_actions: list[dict[str, Any]]) -> bool:
        """Return whether a stuck window contains only completed writes.

        A repeated read loop is unsafe to continue and remains a hard failure.
        A repeated successful or duplicate-blocked write is different: the
        requested mutation may already be present, so the caller can stop
        further writes and report a recoverable partial result instead of
        presenting it as an execution crash.
        """
        if not self.is_stuck(recent_actions):
            return False
        latest = recent_actions[-1]
        if str(latest.get("result_code") or "") not in TERMINAL_WRITE_RESULT_CODES:
            return False
        try:
            signature = json.loads(str(latest.get("signature") or "{}"))
        except (TypeError, ValueError):
            return False
        return str(signature.get("name") or "") in RECOVERABLE_TOOL_NAMES
