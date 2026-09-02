description: Implement a new feature safely with a minimal vertical slice.
when_to_use: The user asks for new behavior, a new tool, endpoint, UI capability, or integration.

# Add Feature

1. Clarify the observable behavior, boundaries, and acceptance criteria.
2. Read existing implementation, callers, rules, and relevant tests.
3. Create a short plan; every important step should include an `acceptance` condition.
4. Keep one step `in_progress`, make the smallest necessary change, and add a behavior test.
5. Run targeted verification, then broader verification when the change crosses module boundaries.
6. Complete every plan step and obtain fresh verification before claiming completion.

Do not perform unrelated refactors, bypass permissions, or treat acceptance text as a command to execute.
