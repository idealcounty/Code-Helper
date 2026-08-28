from __future__ import annotations

import threading

import uvicorn

from .config import AppConfig
from .web.app import create_app


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
    webview.create_window("Code Helper", "http://127.0.0.1:8765", width=1440, height=920)
    webview.start()


if __name__ == "__main__":
    main()
