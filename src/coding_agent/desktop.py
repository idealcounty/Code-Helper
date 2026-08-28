from __future__ import annotations

import threading

import uvicorn

from .config import AppConfig
from .web.app import create_app


class DesktopBridge:
    """Small native bridge; Agent behavior remains in the self-written backend."""

    def __init__(self) -> None:
        self.window = None

    def attach(self, window: object) -> None:
        self.window = window

    def pick_folder(self) -> str | None:
        if self.window is None:
            return None
        import webview

        selection = self.window.create_file_dialog(webview.FOLDER_DIALOG)  # type: ignore[union-attr]
        if not selection:
            return None
        return str(selection[0])


def main() -> None:
    """Open the local Web UI in a native WebView when pywebview is installed."""
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - optional desktop dependency
        raise SystemExit("Install desktop extras first: pip install -e .[desktop]") from exc

    config = AppConfig.from_env()
    app = create_app(config)
    server = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning")
    thread = threading.Thread(target=uvicorn.Server(server).run, daemon=True)
    thread.start()
    bridge = DesktopBridge()
    window = webview.create_window(
        "Code Helper",
        "http://127.0.0.1:8765",
        width=1540,
        height=960,
        min_size=(1080, 700),
        js_api=bridge,
    )
    bridge.attach(window)
    webview.start()


if __name__ == "__main__":
    main()
