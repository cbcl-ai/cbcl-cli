"""The fixture drains real coroutine cleanup rather than clearing handles."""

import asyncio

import pytest

from src.handlers import _BACKGROUND_TASKS, _spawn_background
from tests.background_tasks import drain_handler_background_tasks


@pytest.mark.asyncio
async def test_background_drain_awaits_cancellation_finally():
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def pending_work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finalized.set()

    task = _spawn_background(pending_work())
    await started.wait()
    await drain_handler_background_tasks()

    assert task.cancelled()
    assert finalized.is_set()
    assert task not in _BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_background_drain_does_not_hide_cleanup_failure():
    started = asyncio.Event()

    async def failing_cleanup():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("cleanup failed")

    task = _spawn_background(failing_cleanup())
    await started.wait()

    with pytest.raises(AssertionError, match="cleanup failed"):
        await drain_handler_background_tasks()

    assert task.done()
    assert task not in _BACKGROUND_TASKS
