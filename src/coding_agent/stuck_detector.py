from __future__ import annotations

from typing import Any


class StuckDetector:
    def __init__(self, repeated_action_threshold: int = 3) -> None:
        self.repeated_action_threshold = repeated_action_threshold

    def is_stuck(self, recent_actions: list[dict[str, Any]]) -> bool:
        threshold = self.repeated_action_threshold
        if len(recent_actions) < threshold:
            return False
        window = recent_actions[-threshold:]
        signatures = {
            (item.get("signature"), item.get("result_code")) for item in window
        }
        return len(signatures) == 1
