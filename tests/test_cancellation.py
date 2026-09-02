from __future__ import annotations

import asyncio

import pytest

from coding_agent.cancellation import CancellationToken, RunCancelled, _resolve_waiter


def test_cancellation_token_is_idempotent_and_resettable() -> None:
    token = CancellationToken()
    assert token.requested is False
    assert token.reason == "cancelled"
    assert token.cancel("stop-now") is True
    assert token.cancel("second-request") is False
    assert token.requested is True
    assert token.reason == "stop-now"
    with pytest.raises(RunCancelled, match="stop-now"):
        token.raise_if_cancelled()
    assert asyncio.run(token.wait()) == "stop-now"
    token.reset()
    assert token.requested is False
    assert token.reason == "cancelled"


def test_cancellation_waiter_receives_cancel_reason() -> None:
    async def scenario() -> str:
        token = CancellationToken()
        waiter = asyncio.create_task(token.wait())
        await asyncio.sleep(0)
        assert token.cancel("user") is True
        return await waiter

    assert asyncio.run(scenario()) == "user"


def test_cancellation_skips_done_waiters_and_resolver_is_idempotent() -> None:
    async def scenario() -> None:
        token = CancellationToken()
        done = asyncio.get_running_loop().create_future()
        done.set_result("already-done")
        token._waiters.add(done)
        assert token.cancel("stop") is True
        assert done.result() == "already-done"

        pending = asyncio.get_running_loop().create_future()
        _resolve_waiter(pending, "resolved")
        assert await pending == "resolved"
        _resolve_waiter(pending, "ignored")
        assert pending.result() == "resolved"

    asyncio.run(scenario())


def test_cancellation_reset_cancels_pending_waiters() -> None:
    async def scenario() -> None:
        token = CancellationToken()
        waiter = asyncio.create_task(token.wait())
        await asyncio.sleep(0)
        token.reset()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert waiter.cancelled()

    asyncio.run(scenario())
