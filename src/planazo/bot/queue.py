"""A generic per-key FIFO gate: `PerUserQueue` (ADR 0014).

Two `dispatch()` calls for the same key never run their `job` concurrently —
the second's `job` starts only after the first's has finished, in arrival
order. Two calls for different keys run fully concurrently; a key's state
shares nothing with any other key's.

Arrival order is decided by call order, not by how long a caller's
`on_enqueued()` happens to take. A caller's place in the FIFO line is
reserved synchronously, in the same stretch of code that discovers a job is
already in flight for its key — strictly before `on_enqueued()` is ever
awaited. This is what keeps two callers in arrival order even when the
second caller's `on_enqueued()` resolves before the first caller's does (a
real possibility once `on_enqueued` is a network call with variable
latency, as it is in production).

This module knows nothing about senders, messages, or replies — the caller
supplies an arbitrary hashable `key`, an arbitrary zero-argument `job`, and an
arbitrary zero-argument `on_enqueued` notification. `bot/app.py` is the only
module that imports the transport this queue's `key` and `job` happen to
carry (a `telegram_user_id` and a handler invocation); this module imports
none of it.
"""

from __future__ import annotations

import asyncio
from collections import deque
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

    Per key, this holds a `busy` flag, a waiting count, and a `deque` of
    "turn" events — created lazily on first use and never evicted, accepted
    at MVP scale since it grows at the same rate as one row per sender in
    the `users` table, which the system already accepts.
    """

    def __init__(self, bound: int) -> None:
        self._bound = bound
        self._busy: dict[str, bool] = {}
        self._waiting: dict[str, int] = {}
        self._turns: dict[str, deque[asyncio.Event]] = {}

    async def dispatch(
        self,
        key: str,
        job: Callable[[], Awaitable[None]],
        on_enqueued: Callable[[], Awaitable[None]],
    ) -> DispatchOutcome:
        """Run `job` for `key`, serialized against any other call for `key`.

        If `key` is not busy, this call claims it and runs `job` immediately
        (`RAN`). The "is it busy" check and the decision to claim it happen
        in the same synchronous stretch of code, with no `await` between
        them — on a single-threaded event loop nothing else can run in that
        stretch, so the check is race-free without any lock of its own.

        Otherwise, if `key` already has `bound` callers waiting, this call
        returns `OVERFLOW` immediately and calls neither `job` nor
        `on_enqueued`. Below the bound, this call reserves its place in
        `key`'s FIFO line — appends its own turn event to `key`'s `deque`
        and records itself as waiting — synchronously, before awaiting
        anything. Only after that reservation does it `await on_enqueued()`
        (the caller's immediate acknowledgment — sent before this call's
        turn arrives, which is what makes it *immediate* rather than
        delivered after waiting), then wait on its own turn event. Because
        the reservation happens before `on_enqueued()` is awaited, two calls
        for the same key run their jobs in the order they were *called* in,
        regardless of how long either call's `on_enqueued()` takes to
        resolve. The moment this call's turn event is set, it stops
        counting as waiting and its `job` runs (`ENQUEUED`).

        A `job` that raises still hands `key` off to the next waiter (or
        frees it, if there is none) — that handoff happens in a `finally`
        block, so it runs regardless of how `job()` exits. The exception
        itself propagates out of this call unchanged.
        """
        if not self._busy.get(key, False):
            self._busy[key] = True
            outcome = DispatchOutcome.RAN
        else:
            waiting = self._waiting.get(key, 0)
            if waiting >= self._bound:
                return DispatchOutcome.OVERFLOW

            turn = asyncio.Event()
            self._turns.setdefault(key, deque()).append(turn)
            self._waiting[key] = waiting + 1

            await on_enqueued()
            await turn.wait()
            self._waiting[key] -= 1
            outcome = DispatchOutcome.ENQUEUED

        try:
            await job()
        finally:
            self._advance(key)
        return outcome

    def _advance(self, key: str) -> None:
        """Hand `key` off to its next waiter, or free it if there is none.

        Synchronous and called from a `finally` block, so it always runs
        exactly once per `dispatch()` call that claimed or was woken for
        `key` — whether `job()` returned or raised.
        """
        turns = self._turns.get(key)
        if turns:
            turns.popleft().set()
        else:
            self._busy[key] = False
