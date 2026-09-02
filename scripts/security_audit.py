"""Audit tracked project material for credentials and accidental local data.

The audit is intentionally conservative: placeholders such as ``<YOUR_KEY>``
and ``${CODE_HELPER_API_KEY}`` are allowed, while plausible credentials in
tracked files are reported.  Findings are written as JSON/Markdown so the
result can be attached to a release review.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_RE = re.compile(
    r"^(?:$|your[_ -]?(?:api[_ -]?)?key|replace[_ -]?me|change[_ -]?me|"
    r"changeme|dummy|example|test(?:-key)?|<[^>]+>|\$\{[^}]+\})$",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:sk|ds|deepseek)[_-]?[A-Za-z0-9]{24,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|deepseek[_ -]?api[_ -]?key)\s*[:=]\s*['\"]?([^'\"\s,;]+)"
    ),
)
PERSONAL_PATH_RE = re.compile(
    r"(?i)(?:[A-Z]:\\" + r"Users\\[^\\\s]+|/" + r"home/[^/\s]+)"
)
KNOWN_TEST_TOKENS = {
    "sk-abcdefghijklmnopqrstuvwxyz",
    "AKIA1234567890ABCDEF",
    "AIzaSyAbcdefghijklmnopqrstu",
    "sk-ant_api-token-12345678901234567890",
    "glpat_12345678901234567890",
    "npm_12345678901234567890",
    "hf_12345678901234567890",
    "sbp_12345678901234567890",
    "sk_live_1234567890123456",
    "rk_test_1234567890123456",
}


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    code: str
    path: str
    detail: str


def tracked_files(root: Path = ROOT) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError("git ls-files failed")
    names = [item for item in result.stdout.decode("utf-8", "replace").split("\0") if item]
    return [root / name for name in names]


def untracked_files(root: Path = ROOT) -> list[Path]:
    """Return non-ignored files not yet staged, so new files are audited too."""

    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    names = [item for item in result.stdout.decode("utf-8", "replace").split("\0") if item]
    return [root / name for name in names]


def history_text(root: Path = ROOT) -> str:
    """Read patch text from all local refs without exposing it in reports."""

    result = subprocess.run(
        ["git", "log", "--all", "--patch", "--no-ext-diff", "--format="],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace")


def audit_history_text(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else match.group(0)
            value = value.strip().strip("'\"")
            if _looks_like_secret(value) and value not in KNOWN_TEST_TOKENS:
                findings.append(Finding("high", "HISTORY_POSSIBLE_SECRET", ".git-history", f"credential-like value near byte {match.start()}"))
    return findings


def _looks_like_secret(value: str) -> bool:
    value = value.strip().strip("'\"")
    if PLACEHOLDER_RE.match(value):
        return False
    if len(value) < 16:
        return False
    return bool(re.search(r"[A-Za-z]", value) and re.search(r"\d", value))


def audit_text(path: Path, text: str, *, root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    display = str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    for pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else match.group(0)
            if _looks_like_secret(value) and value not in KNOWN_TEST_TOKENS:
                findings.append(Finding("high", "POSSIBLE_SECRET", display, f"credential-like value near byte {match.start()}"))
    for match in PERSONAL_PATH_RE.finditer(text):
        findings.append(Finding("medium", "PERSONAL_PATH", display, f"local path near byte {match.start()}"))
    return findings


def audit_repository(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    try:
        files = tracked_files(root)
    except (OSError, RuntimeError) as exc:
        return [Finding("high", "GIT_SCAN_FAILED", ".", str(exc))]
    all_files = list(dict.fromkeys(files + untracked_files(root)))
    tracked_names = {path.name for path in files}
    if ".env" in tracked_names:
        findings.append(Finding("high", "ENV_TRACKED", ".env", ".env must never be committed"))
    example = root / ".env.server.example"
    if example.is_file():
        try:
            example_text = example.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(Finding("high", "EXAMPLE_UNREADABLE", ".env.server.example", str(exc)))
        else:
            for line_no, line in enumerate(example_text.splitlines(), 1):
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip("'\"")
                if "KEY" in key.upper() and _looks_like_secret(value):
                    findings.append(Finding("high", "EXAMPLE_CONTAINS_SECRET", ".env.server.example", f"line {line_no} contains a non-placeholder key"))
    for path in all_files:
        if path.name in {".env", ".env.local"} or not path.is_file():
            continue
        try:
            if path.stat().st_size > 5_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(audit_text(path, text, root=root))
    history = history_text(root)
    if history:
        findings.extend(audit_history_text(history))
    return findings


def render_markdown(findings: Iterable[Finding], *, passed: bool) -> str:
    rows = list(findings)
    lines = [
        "# Code Helper 安全审计",
        "",
        "> 审计不会打印密钥内容；扫描范围包含当前工作树、未跟踪文件和本地 Git 历史。",
        "",
        f"结论：**{'Passed' if passed else 'Failed'}**",
        "",
        "| 严重级别 | 代码 | 文件 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(f"| {item.severity} | `{item.code}` | `{item.path}` | {item.detail} |" for item in rows)
    if not rows:
        lines.append("| — | — | — | 未发现高风险密钥、被跟踪 `.env` 或示例文件中的真实凭据 |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, help="写入 security.json 和 security.md")
    parser.add_argument("--strict-paths", action="store_true", help="把本机绝对路径也作为失败项")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    findings = audit_repository(root)
    blocking = [item for item in findings if item.severity == "high" or args.strict_paths]
    passed = not blocking
    payload = {"schema_version": 1, "passed": passed, "findings": [asdict(item) for item in findings]}
    if args.output_dir:
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "security.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output_dir / "security.md").write_text(render_markdown(findings, passed=passed), encoding="utf-8")
    print(json.dumps({"passed": passed, "finding_count": len(findings)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
