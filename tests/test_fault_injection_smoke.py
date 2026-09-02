from __future__ import annotations

from pathlib import Path

from scripts.fault_injection_smoke import (
    _command_timeout,
    _model_429,
    _path_boundary,
    _post_hook_failure,
    _stream_argument_recovery,
)


def test_fault_injection_model_protocol_recovers_streamed_arguments() -> None:
    result = _stream_argument_recovery()
    assert result["passed"] is True
    assert result["requests"] == [True, False]


def test_fault_injection_rate_limit_is_normalized_without_secret() -> None:
    result = _model_429()
    assert result == {"name": "model_http_429", "passed": True, "error_code": "HTTP_429"}


def test_fault_injection_tool_and_boundary_failures_are_safe(tmp_path: Path) -> None:
    assert _path_boundary(tmp_path)["error_code"] == "PATH_OUTSIDE_WORKSPACE"
    assert _command_timeout(tmp_path)["error_code"] == "COMMAND_TIMEOUT"
    assert _post_hook_failure(tmp_path)["error_code"] == "HOOK_FAILED"
