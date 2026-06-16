"""T8.2.1 — the shared _supervise wrapper: loud restart on crash, clean stop
on cancel, no restart-spin on a normal return."""
from __future__ import annotations

import asyncio
import pytest

from src import daemon as d


@pytest.mark.asyncio
async def test_crash_restarts_with_backoff(monkeypatch):
    _real = asyncio.sleep
    async def _no_sleep(*_a, **_k):
        await _real(0)
    monkeypatch.setattr(d.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def _factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        # On the 3rd attempt, return cleanly (no restart_on_clean_return).

    await d._supervise("t", _factory)
    assert calls["n"] == 3  # crashed twice, restarted, then returned


@pytest.mark.asyncio
async def test_cancel_propagates_no_restart():
    async def _factory():
        await asyncio.sleep(3600)

    task = asyncio.create_task(d._supervise("t", _factory))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_clean_return_does_not_restart():
    calls = {"n": 0}

    async def _factory():
        calls["n"] += 1

    await d._supervise("t", _factory)
    assert calls["n"] == 1  # ran once, returned, did NOT restart-spin


@pytest.mark.asyncio
async def test_should_run_false_stops_without_restart(monkeypatch):
    _real = asyncio.sleep
    async def _no_sleep(*_a, **_k):
        await _real(0)
    monkeypatch.setattr(d.asyncio, "sleep", _no_sleep)

    async def _factory():
        raise RuntimeError("boom")

    # should_run False → graceful stop, no restart even though it crashed.
    await d._supervise("t", _factory, should_run=lambda: False)
