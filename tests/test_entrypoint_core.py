from __future__ import annotations

from pathlib import Path

from coding_agent import cli, desktop
from coding_agent.config import AppConfig
from coding_agent.runtime import create_runtime
from coding_agent.web import app as web_app


def test_cli_web_and_desktop_are_wired_to_the_same_core_factory(
    tmp_path: Path,
) -> None:
    """All user-facing entry points must share Runtime and its event source."""
    assert cli.create_runtime is create_runtime
    assert web_app.create_runtime is create_runtime
    assert desktop.create_app is web_app.create_app

    config = AppConfig(
        api_key="entrypoint-test",
        base_url="https://example.invalid/v1",
        user_memory_dir=tmp_path.parent / "entrypoint-user-memory",
    )
    direct = create_runtime(config=config, workspace_path=tmp_path, session_id="shared")
    manager = web_app.WebSessionManager(config=config)
    web_session = manager.create(
        str(tmp_path), "act", session_id="shared", approval_policy="ask"
    )

    assert type(web_session.runtime.runner) is type(direct.runner)
    assert web_session.runtime.event_store.path == direct.event_store.path
    assert web_session.runtime.registry.names() == direct.registry.names()
