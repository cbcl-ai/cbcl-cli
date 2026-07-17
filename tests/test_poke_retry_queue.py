"""FIX P2 (blink-resilience) — durable Manager pokes.

Contract under test (``_manager_action_requests``):

* ``_dispatch_poke(retry_on_failure=True)`` queues an UNDELIVERED poke
  (``handle_chat_message`` returned False) on the controller and starts
  ONE drain task;
* the drain redelivers queued pokes on its tick, removes delivered
  entries, and drops an entry after ``_POKE_RETRY_MAX_ATTEMPTS``
  failures;
* delivered pokes / duplicate-dropped pokes are never queued;
* the queue is size-capped (oldest dropped).
"""

from __future__ import annotations

import asyncio

import pytest

import src.orchestrator._manager_action_requests as mar
from src.orchestrator._manager_action_requests import (
    _dispatch_poke,
    _drain_pending_pokes,
)


class _StubController:
    """Minimal controller: scripted handle_chat_message outcomes."""

    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def handle_chat_message(self, msg: dict, source: str = "") -> bool:
        self.calls.append(msg)
        if self._outcomes:
            return self._outcomes.pop(0)
        return True


def _msg(conv: str = "conv-1") -> dict:
    return {
        "context_key": "workstream:ws-1",
        "user_message": "[Planner] roadmap ready",
        "conversation_id": conv,
    }


@pytest.mark.asyncio
async def test_failed_poke_is_queued_and_drain_started():
    ctrl = _StubController([False])
    delivered = await _dispatch_poke(
        ctrl, _msg(), retry_on_failure=True,
    )
    assert delivered is False
    assert len(ctrl._pending_pokes) == 1
    assert ctrl._pending_pokes[0]["attempts"] == 0
    drain = ctrl._poke_drain_task
    assert isinstance(drain, asyncio.Task) and not drain.done()
    drain.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_delivered_poke_is_never_queued():
    ctrl = _StubController([True])
    delivered = await _dispatch_poke(
        ctrl, _msg(), retry_on_failure=True,
    )
    assert delivered is True
    assert getattr(ctrl, "_pending_pokes", None) in (None, [])


@pytest.mark.asyncio
async def test_drain_redelivers_and_empties(monkeypatch):
    monkeypatch.setattr(mar, "_POKE_RETRY_INTERVAL_SECONDS", 0.0)
    # First delivery fails (queues), the drain's retry succeeds.
    ctrl = _StubController([False, True])
    await _dispatch_poke(ctrl, _msg(), retry_on_failure=True)
    # Cancel the auto-started drain and run one deterministically.
    ctrl._poke_drain_task.cancel()
    await asyncio.sleep(0)
    await asyncio.wait_for(_drain_pending_pokes(ctrl), timeout=1.0)
    assert ctrl._pending_pokes == []
    assert len(ctrl.calls) == 2


@pytest.mark.asyncio
async def test_drain_drops_after_attempt_cap(monkeypatch):
    monkeypatch.setattr(mar, "_POKE_RETRY_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(mar, "_POKE_RETRY_MAX_ATTEMPTS", 2)
    ctrl = _StubController([False, False, False, False])
    await _dispatch_poke(ctrl, _msg(), retry_on_failure=True)
    ctrl._poke_drain_task.cancel()
    await asyncio.sleep(0)
    await asyncio.wait_for(_drain_pending_pokes(ctrl), timeout=1.0)
    # 1 original + 2 capped retries; the entry is gone.
    assert ctrl._pending_pokes == []
    assert len(ctrl.calls) == 3


@pytest.mark.asyncio
async def test_queue_size_cap_drops_oldest(monkeypatch):
    monkeypatch.setattr(mar, "_POKE_RETRY_MAX_QUEUE", 2)
    ctrl = _StubController([False, False, False])
    for i in range(3):
        await _dispatch_poke(
            ctrl, _msg(conv=f"conv-{i}"), retry_on_failure=True,
        )
    assert len(ctrl._pending_pokes) == 2
    convs = [
        e["msg"]["conversation_id"] for e in ctrl._pending_pokes
    ]
    assert convs == ["conv-1", "conv-2"]  # conv-0 (oldest) dropped
    ctrl._poke_drain_task.cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_marked_duplicate_is_dropped_not_queued():
    """A poke whose conversation_id was already marked delivered passes
    the dedup check as 'delivered' — never queued."""
    ctrl = _StubController([True, False])
    # First delivery succeeds and MARKS the id.
    assert await _dispatch_poke(ctrl, _msg("dup-1")) is True
    # Second dispatch of the same id: dropped as duplicate BEFORE the
    # chat call — 'the Manager already has this information'.
    assert await _dispatch_poke(
        ctrl, _msg("dup-1"), retry_on_failure=True,
    ) is True
    assert getattr(ctrl, "_pending_pokes", None) in (None, [])
    assert len(ctrl.calls) == 1
