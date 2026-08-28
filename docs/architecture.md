# Architecture and dependency boundary

## Goal

Code Helper is a local coding agent. A user request is processed by a self-written loop that calls a language model, validates native tool calls, executes local tools, feeds observations back to the model, verifies changes, and stops with an evidence-based result.

```text
CLI / Web UI
    -> AgentRunner
    -> ContextManager
    -> OpenAI-compatible model API
    -> ToolExecutor
    -> local filesystem / subprocess
```

## Self-written agent logic

The repository implements these responsibilities directly:

- turn and step lifecycle;
- conversation and context construction;
- native tool-call parsing;
- tool schema validation and local execution;
- workspace and permission policy;
- approval, cancellation, limits, and loop termination;
- event logging and UI event streaming;
- file version checks;
- verification freshness and final status;
- repeated-action detection and error handling.

## Third-party dependency boundary

| Dependency | Permitted responsibility | Not permitted to do |
| --- | --- | --- |
| `httpx` | HTTP requests to an OpenAI-compatible endpoint | Agent orchestration or tool execution |
| `FastAPI` | Local HTTP/WebSocket transport | Agent decisions or context management |
| `uvicorn` | Run the local Web server | Agent logic |
| `PyWebView` (optional) | Display the local Web UI in a desktop window | Invoke an existing coding agent |
| `PyInstaller` (optional) | Package the application | Provide agent behavior |

No existing coding-agent product is invoked or wrapped. No agent framework or agent SDK is used.

## Core execution contract

The model may request an action, but it never directly accesses the machine. Every action passes through parameter validation, workspace policy, optional user approval, execution, result normalization, and event logging. A code-changing task is complete only when applicable structured verification evidence succeeds after the latest mutation; `purpose="verify"` alone is never trusted. Checkpoint restore preflights the current file hashes against the latest Agent-owned outputs and refuses conflicts unless the user explicitly confirms a forced restore.
