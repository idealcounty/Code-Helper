"""Small, dependency-free metadata helpers shared by evidence probes."""

from __future__ import annotations

import platform
import os
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOTS = ("src/", "scripts/", "tests/", "evals/", ".github/")
SNAPSHOT_FILES = {"pyproject.toml", "pytest.ini", "requirements.txt"}


def _is_snapshot_file(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return normalized in SNAPSHOT_FILES or normalized.startswith(SNAPSHOT_ROOTS)


def _git_output(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout or b""
    if isinstance(output, bytes):
        return output.decode("utf-8", "replace").strip()
    return str(output).strip()


def collect_metadata() -> dict[str, Any]:
    """Return safe version/environment metadata without reading credentials."""

    status = _git_output("status", "--porcelain")
    snapshot_hash = hashlib.sha256()
    if status is not None:
        snapshot_hash.update(status.encode("utf-8", "replace"))
        snapshot_hash.update((_git_output("diff", "--no-ext-diff", "--binary") or "").encode("utf-8", "replace"))
        untracked = _git_output("ls-files", "--others", "--exclude-standard", "-z") or ""
        for name in [item for item in untracked.split("\0") if item]:
            if not _is_snapshot_file(name):
                continue
            path = ROOT / name
            snapshot_hash.update(name.encode("utf-8", "replace"))
            try:
                if path.is_file() and path.stat().st_size <= 20_000_000:
                    snapshot_hash.update(path.read_bytes())
            except OSError:
                snapshot_hash.update(b"<unreadable>")
    return {
        "git_commit": _git_output("rev-parse", "HEAD"),
        "git_dirty": bool(status),
        "git_snapshot_sha256": snapshot_hash.hexdigest() if status is not None else None,
        "environment": {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
        },
    }
