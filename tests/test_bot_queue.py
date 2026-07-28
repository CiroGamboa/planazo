"""`PerUserQueue` behaviour (ADR 0014): pure `asyncio`, no bot/telegram/db."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from planazo.bot.queue import DispatchOutcome, PerUserQueue


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_same_key_second_job_waits_for_first_and_ack_is_immediate() -> None:
    queue = PerUserQueue(bound=5)
    events: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_job() -> None:
        events.append("first-start")
        first_started.set()
        await release_first.wait()
        events.append("first-end")

    async def second_job() -> None:
        events.append("second-start")
        events.append("second-end")

    async def second_ack() -> None:
        events.append("second-ack")

    first_task = asyncio.create_task(queue.dispatch("alice", first_job, _noop))
    await first_started.wait()

    second_task = asyncio.create_task(queue.dispatch("alice", second_job, second_ack))
    await asyncio.sleep(0)
    assert events == ["first-start", "second-ack"]

    release_first.set()
    assert await first_task is DispatchOutcome.RAN
    assert await second_task is DispatchOutcome.ENQUEUED
    assert events == ["first-start", "second-ack", "first-end", "second-start", "second-end"]


@pytest.mark.asyncio
async def test_two_keys_do_not_block_each_other() -> None:
    queue = PerUserQueue(bound=5)
    events: list[str] = []
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()

    async def slow_job() -> None:
        events.append("slow-start")
        slow_started.set()
        await release_slow.wait()
        events.append("slow-end")

    async def fast_job() -> None:
        events.append("fast-start")
        events.append("fast-end")

    slow_task = asyncio.create_task(queue.dispatch("alice", slow_job, _noop))
    await slow_started.wait()

    assert await queue.dispatch("bob", fast_job, _noop) is DispatchOutcome.RAN
    assert events == ["slow-start", "fast-start", "fast-end"]

    release_slow.set()
    assert await slow_task is DispatchOutcome.RAN
    assert events == ["slow-start", "fast-start", "fast-end", "slow-end"]


@pytest.mark.asyncio
async def test_bound_admits_waiting_jobs_and_overflows_beyond_it() -> None:
    queue = PerUserQueue(bound=2)
    ran: list[int] = []
    acked: list[int] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_acked = asyncio.Event()
    third_acked = asyncio.Event()

    def make_job(n: int) -> Callable[[], Awaitable[None]]:
        async def job() -> None:
            ran.append(n)

        return job

    def make_ack(n: int, signal: asyncio.Event) -> Callable[[], Awaitable[None]]:
        async def ack() -> None:
            acked.append(n)
            signal.set()

        return ack

    async def first_job() -> None:
        first_started.set()
        await release_first.wait()
        ran.append(1)

    first_task = asyncio.create_task(queue.dispatch("alice", first_job, _noop))
    await first_started.wait()

    second_task = asyncio.create_task(
        queue.dispatch("alice", make_job(2), make_ack(2, second_acked))
    )
    third_task = asyncio.create_task(queue.dispatch("alice", make_job(3), make_ack(3, third_acked)))
    await second_acked.wait()
    await third_acked.wait()

    fourth_outcome = await queue.dispatch("alice", make_job(4), make_ack(4, asyncio.Event()))

    assert fourth_outcome is DispatchOutcome.OVERFLOW
    assert 4 not in ran
    assert 4 not in acked

    release_first.set()
    assert await first_task is DispatchOutcome.RAN
    assert await second_task is DispatchOutcome.ENQUEUED
    assert await third_task is DispatchOutcome.ENQUEUED
    assert ran == [1, 2, 3]
    assert acked == [2, 3]


@pytest.mark.asyncio
async def test_a_raising_job_still_releases_the_lock_for_the_next_dispatch() -> None:
    queue = PerUserQueue(bound=5)

    async def failing_job() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await queue.dispatch("alice", failing_job, _noop)

    ran = False

    async def next_job() -> None:
        nonlocal ran
        ran = True

    assert await queue.dispatch("alice", next_job, _noop) is DispatchOutcome.RAN
    assert ran is True
