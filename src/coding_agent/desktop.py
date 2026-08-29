from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any

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

    def copy_text(self, text: str) -> bool:
        """Copy UI text through the native clipboard when WebView APIs are unavailable."""
        return _copy_text_to_system_clipboard(str(text))


def _copy_text_to_system_clipboard(text: str) -> bool:
    if os.name == "nt":
        return _copy_text_to_windows_clipboard(text)

    # PyWebView's desktop target is Windows, but this keeps editable installs
    # useful on other platforms without adding a clipboard dependency.
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


def _copy_text_to_windows_clipboard(text: str) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    payload = (text.replace("\x00", "") + "\x00").encode("utf-16-le")
    memory = kernel32.GlobalAlloc(0x0042, len(payload))  # GMEM_MOVEABLE | GMEM_ZEROINIT
    if not memory:
        return False
    pointer = kernel32.GlobalLock(memory)
    if not pointer:
        kernel32.GlobalFree(memory)
        return False
    ctypes.memmove(pointer, payload, len(payload))
    kernel32.GlobalUnlock(memory)

    opened = False
    for _ in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01)
    if not opened:
        kernel32.GlobalFree(memory)
        return False

    transferred = False
    try:
        if user32.EmptyClipboard() and user32.SetClipboardData(13, memory):  # CF_UNICODETEXT
            transferred = True
            return True
        return False
    finally:
        user32.CloseClipboard()
        if not transferred:
            kernel32.GlobalFree(memory)


def _create_listener(preferred_port: int = 8765) -> socket.socket:
    """Reserve a private loopback port so desktop instances never reuse stale servers."""
    ports = [preferred_port] if preferred_port == 0 else [preferred_port, 0]
    last_error: OSError | None = None
    for port in ports:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            return listener
        except OSError as exc:
            last_error = exc
            listener.close()
    raise RuntimeError("Cannot reserve a local port for Code Helper") from last_error


def _create_desktop_window(webview_module: Any, bridge: DesktopBridge, url: str) -> object:
    return webview_module.create_window(
        "Code Helper",
        url,
        width=1540,
        height=960,
        min_size=(1080, 700),
        js_api=bridge,
        text_select=True,
    )


def main() -> None:
    """Open the local Web UI in a native WebView when pywebview is installed."""
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - optional desktop dependency
        raise SystemExit("Install desktop extras first: pip install -e .[desktop]") from exc

    config = AppConfig.from_env()
    app = create_app(config)
    listener = _create_listener()
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        listener.close()
        raise SystemExit("Code Helper local service could not start")
    bridge = DesktopBridge()
    window = _create_desktop_window(webview, bridge, f"http://127.0.0.1:{port}")
    bridge.attach(window)
    webview.start()


if __name__ == "__main__":
    main()
