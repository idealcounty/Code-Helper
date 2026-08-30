from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent import desktop


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_desktop_bridge_uses_native_clipboard(monkeypatch: Any) -> None:
    copied: list[str] = []
    monkeypatch.setattr(
        desktop,
        "_copy_text_to_system_clipboard",
        lambda text: copied.append(text) is None,
    )

    result = desktop.DesktopBridge().copy_text("nested/file.cpp")

    assert result is True
    assert copied == ["nested/file.cpp"]


def test_desktop_window_enables_text_selection() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class FakeWebView:
        @staticmethod
        def create_window(title: str, url: str, **kwargs: object) -> object:
            calls.append((title, url, kwargs))
            return object()

    bridge = desktop.DesktopBridge()
    window = desktop._create_desktop_window(
        FakeWebView, bridge, "http://127.0.0.1:9876"
    )

    assert window is not None
    assert calls[0][0:2] == ("Code Helper", "http://127.0.0.1:9876")
    assert calls[0][2]["js_api"] is bridge
    assert calls[0][2]["text_select"] is True


def test_desktop_listener_can_use_an_ephemeral_port() -> None:
    listener = desktop._create_listener(0)
    fallback = None
    try:
        address, port = listener.getsockname()
        assert address == "127.0.0.1"
        assert port > 0
        fallback = desktop._create_listener(port)
        assert fallback.getsockname()[1] != port
    finally:
        if fallback is not None:
            fallback.close()
        listener.close()


def test_windows_package_collects_tiktoken_extensions() -> None:
    spec = (PROJECT_ROOT / "packaging" / "coding-agent.spec").read_text(
        encoding="utf-8"
    )

    assert 'collect_submodules("tiktoken_ext")' in spec
    assert '*tiktoken_extensions' in spec
