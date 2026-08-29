from __future__ import annotations

import json
from pathlib import Path

from .types import EvalTask


TASK_ROOT = Path(__file__).resolve().parent / "tasks"


def load_tasks(task_ids: set[str] | None = None) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    for path in sorted(TASK_ROOT.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        task = EvalTask.from_dict(payload)
        if task_ids is None or task.id in task_ids:
            tasks.append(task)
    if task_ids:
        missing = task_ids - {task.id for task in tasks}
        if missing:
            raise ValueError(f"Unknown Eval task ids: {', '.join(sorted(missing))}")
    if len({task.id for task in tasks}) != len(tasks):
        raise ValueError("Eval task ids must be unique")
    return tasks
