description: Review code changes and report actionable findings without modifying files.
when_to_use: The user asks for review, audit, quality assessment, or pre-merge feedback without requesting changes.

# Code Review

1. Read the diff, surrounding implementation, relevant tests, and project rules.
2. Prioritize correctness, security, data loss, compatibility, and regression risks.
3. Use read-only checks or a safe reproduction to confirm important findings.
4. Report severity, file and line, impact, evidence, and a concrete recommendation.
5. Separate must-fix findings from optional improvements and state clearly when no blocking issue was found.

This workflow is read-only. Do not write files, run mutating commands, create commits, or claim that a suggested fix was applied. A later explicit fix request starts a new turn using `bug-fix` or `add-feature`.
