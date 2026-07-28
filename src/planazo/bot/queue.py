"""A generic per-key FIFO gate: `PerUserQueue` (ADR 0014).

Two `dispatch()` calls for the same key never run their `job` concurrently —
the second's `job` starts only after the first's has finished, in arrival
order. Two calls for different keys run fully concurrently; a key's lock and
waiting count share nothing with any other key's.

This module knows nothing about senders, messages, or replies — the caller
supplies an arbitrary hashable `key`, an arbitrary zero-argument `job`, and an
arbitrary zero-argument `on_enqueued` notification. `bot/app.py` is the only
module that imports the transport this queue's `key` and `job` happen to
carry (a `telegram_user_id` and a handler invocation); this module imports
none of it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import Enum


class DispatchOutcome(Enum):
    """What happened to one `dispatch()` call."""

    RAN = "ran"
    """No job was in flight for this key: this call's `job` ran immediately."""

    ENQUEUED = "enqueued"
    """Another job was in flight for this key: this call waited, then ran."""

    OVERFLOW = "overflow"
    """This key's backlog was already at its bound: `job` never ran."""


class PerUserQueue:
    """A per-key FIFO gate bounding how many callers wait behind one key.

    One `asyncio.Lock` and one waiting-count per key, created on first use and
    never evicted — accepted at MVP scale, since it grows at the same rate as
    one row per sender in the `users` table, which the system already
    accepts.
    """

    def __init__(self, bound: int) -> None:
        self._bound = bound
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiting: dict[str, int] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def dispatch(
        self,
        key: str,
        job: Callable[[], Awaitable[None]],
        on_enqueued: Callable[[], Awaitable[None]],
    ) -> DispatchOutcome:
        """Run `job` for `key`, serialized against any other call for `key`.

        If `key`'s lock is free, this call acquires it and runs `job`
        immediately (`RAN`). The freeness check and the decision to acquire
        happen in the same synchronous stretch of code, with no `await`
        between them — on a single-threaded event loop nothing else can run
        in that stretch, so the check is race-free without any lock of its
        own.

        Otherwise, if `key` already has `bound` callers waiting, this call
        returns `OVERFLOW` immediately and calls neither `job` nor
        `on_enqueued`. Below the bound, it records itself as waiting, awaits
        `on_enqueued()` — before it has the lock, so the notification is
        immediate rather than deferred until this call's own turn — then
        waits for the lock. `asyncio.Lock` wakes waiters in the order they
        started waiting, so two calls queued for the same key run their jobs
        in that same order. The moment this call is woken, before `job` runs,
        it stops counting as waiting and its `job` runs (`ENQUEUED`).

        A `job` that raises still releases the lock on the way out of the
        `async with` block, so the next waiter for `key` still runs; the
        exception itself propagates out of this call unchanged.
        """
        lock = self._lock_for(key)
        if not lock.locked():
            async with lock:
                await job()
            return DispatchOutcome.RAN

        waiting = self._waiting.get(key, 0)
        if waiting >= self._bound:
            return DispatchOutcome.OVERFLOW

        self._waiting[key] = waiting + 1
        await on_enqueued()
        async with lock:
            self._waiting[key] -= 1
            await job()
        return DispatchOutcome.ENQUEUED
