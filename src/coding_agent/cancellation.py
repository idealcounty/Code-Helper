from __future__ import annotations

import asyncio
from threading import Lock


class RunCancelled(RuntimeError):
    def __init__(self, reason: str = "user_requested") -> None:
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """One-turn cooperative cancellation signal shared by every runtime layer."""

    def __init__(self) -> None:
        self._requested = False
        self._reason = ""
        self._waiters: set[asyncio.Future[str]] = set()
        self._lock = Lock()

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason or "cancelled"

    def cancel(self, reason: str = "user_requested") -> bool:
        with self._lock:
            if self._requested:
                return False
            self._requested = True
            self._reason = reason
            waiters = tuple(self._waiters)
        for waiter in waiters:
            if waiter.done():
                continue
            waiter.get_loop().call_soon_threadsafe(_resolve_waiter, waiter, reason)
        return True

    def reset(self) -> None:
        with self._lock:
            waiters = tuple(self._waiters)
            self._waiters.clear()
            self._requested = False
            self._reason = ""
        for waiter in waiters:
            if not waiter.done():
                waiter.get_loop().call_soon_threadsafe(waiter.cancel)

    async def wait(self) -> str:
        with self._lock:
            if self._requested:
                return self._reason or "cancelled"
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.add(waiter)
        try:
            return await waiter
        finally:
            with self._lock:
                self._waiters.discard(waiter)

    def raise_if_cancelled(self) -> None:
        if self.requested:
            raise RunCancelled(self.reason)


def _resolve_waiter(waiter: asyncio.Future[str], reason: str) -> None:
    if not waiter.done():
        waiter.set_result(reason)
