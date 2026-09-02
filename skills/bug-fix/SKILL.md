description: Diagnose and safely fix a reported defect with a regression proof.
when_to_use: A reproducible bug, failing test, regression, or unexpected behavior is the main task.

# Bug Fix

1. Read the report, error output, and smallest relevant code path.
2. Reproduce the problem and write a concrete root-cause hypothesis.
3. Add or confirm a regression test that fails before the fix.
4. Make the smallest fix, run the regression, and verify the affected scope.
5. Report root cause, changed files, evidence, and remaining risk.

Simple single-file fixes may omit a formal plan. Cross-file fixes should use a plan with explicit acceptance conditions. Never hide a failed verification or broaden permissions to make a test pass.
