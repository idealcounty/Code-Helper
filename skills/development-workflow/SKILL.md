description: Route code changes into a lightweight inspect, plan, implement, verify, finish workflow.
when_to_use: When a request modifies, fixes, or reviews project code and the right workflow is unclear.

# Development Workflow

This is the routing skill, not the implementation checklist. Decide which concrete skill applies, load it, and then follow that skill:

- New behavior, interface, page, or tool: load `add-feature`.
- A reported defect, failing test, regression, or exception: load `bug-fix`.
- A request that only asks for findings, audit, or risk analysis: load `code-review`.
- Pure explanation or read-only project questions do not require a development workflow.

Do not claim that the routing skill itself completed the work. A concrete skill must be loaded before changing files.
