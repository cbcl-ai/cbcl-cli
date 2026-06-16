"""T4.3.2 — Manager give-up is user-visible (one AR) + self-healing (retry tick)."""
from __future__ import annotations

import asyncio
import pytest

from src.orchestrator import manager_controller as mc
from src.orchestrator.manager_controller import (
    ManagerController,
    MANAGER_GIVEUP_RETRY_BASE_SECONDS,
    MANAGER_GIVEUP_RETRY_MAX_SECONDS,
    MANAGER_MAX_CONSECUTIVE_CRASHES,
)


def _make_controller(**kw):
    return ManagerController(
        supervisor=None, router=None,
        session_manager=type("S", (), {
            "clear_session": staticmethod(lambda *a, **k: asyncio.sleep(0)),
            "get_session_id": staticmethod(lambda *a, **k: None),
        })(),
        config_store=type("C", (), {})(),
        office_id="off-1", backend_url="http://x", security_token="tok",
        **kw,
    )


@pytest.mark.asyncio
async def test_giveup_emits_one_ar_and_starts_retry(monkeypatch):
    c = _make_controller()
    escalations = []

    async def _esc(reason):
        escalations.append(reason)

    monkeypatch.setattr(c, "_escalate_manager_giveup", _esc)
    monkeypatch.setattr(c, "_spawn_manager", lambda: asyncio.sleep(0))
    c._consecutive_crashes = MANAGER_MAX_CONSECUTIVE_CRASHES
    await c._restart_manager("test-crash")
    assert c._given_up is True
    assert len(escalations) == 1
    assert c._giveup_retry_task is not None
    c._giveup_retry_task.cancel()


@pytest.mark.asyncio
async def test_giveup_only_escalates_once(monkeypatch):
    c = _make_controller()
    escalations = []

    async def _esc(reason):
        escalations.append(reason)

    monkeypatch.setattr(c, "_escalate_manager_giveup", _esc)
    c._consecutive_crashes = MANAGER_MAX_CONSECUTIVE_CRASHES
    await c._restart_manager("c1")
    c._consecutive_crashes = MANAGER_MAX_CONSECUTIVE_CRASHES
    await c._restart_manager("c2")
    assert len(escalations) == 1
    if c._giveup_retry_task:
        c._giveup_retry_task.cancel()


@pytest.mark.asyncio
async def test_retry_loop_respawns_and_clears_giveup(monkeypatch):
    c = _make_controller()
    c._given_up = True
    _real_sleep = asyncio.sleep

    async def _no_sleep(*_a, **_k):
        await _real_sleep(0)

    monkeypatch.setattr(mc.asyncio, "sleep", _no_sleep)
    spawned = []

    async def _spawn():
        spawned.append(1)

    monkeypatch.setattr(c, "_spawn_manager", _spawn)
    await c._giveup_retry_loop()
    assert spawned == [1]
    assert c._given_up is False
    assert c._consecutive_crashes == 0


@pytest.mark.asyncio
async def test_giveup_backoff_advances_and_caps(monkeypatch):
    # F4: prove the exponential backoff advances (10m→20m→40m) and CAPS at
    # the max — the spawn fails 3× then succeeds.
    c = _make_controller()
    c._given_up = True
    slept: list[float] = []
    _real_sleep = asyncio.sleep

    async def _record_sleep(delay, *_a, **_k):
        slept.append(delay)
        await _real_sleep(0)

    monkeypatch.setattr(mc.asyncio, "sleep", _record_sleep)
    attempts = {"n": 0}

    async def _spawn():
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise RuntimeError("spawn boom")
        # 4th attempt succeeds.

    monkeypatch.setattr(c, "_spawn_manager", _spawn)
    await c._giveup_retry_loop()

    base = MANAGER_GIVEUP_RETRY_BASE_SECONDS
    assert slept == [base, base * 2, base * 4 if base * 4 <= MANAGER_GIVEUP_RETRY_MAX_SECONDS else MANAGER_GIVEUP_RETRY_MAX_SECONDS, MANAGER_GIVEUP_RETRY_MAX_SECONDS]
    # Capped: never exceeds the max.
    assert max(slept) == MANAGER_GIVEUP_RETRY_MAX_SECONDS
    assert c._given_up is False


@pytest.mark.asyncio
async def test_stop_cancels_giveup_retry_task(monkeypatch):
    # F1: a controller torn down while in give-up must cancel its retry loop,
    # not leak it (it would keep waking and spawning against a dead office).
    c = _make_controller()
    c._given_up = True
    _real_sleep = asyncio.sleep

    async def _long_sleep(*_a, **_k):
        await _real_sleep(60)

    monkeypatch.setattr(mc.asyncio, "sleep", _long_sleep)
    monkeypatch.setattr(c, "_spawn_manager", lambda: asyncio.sleep(0))
    c._giveup_retry_task = asyncio.create_task(c._giveup_retry_loop())
    await _real_sleep(0)  # let it start + enter the sleep
    task = c._giveup_retry_task

    await c.stop()

    assert c._giveup_retry_task is None
    await _real_sleep(0)
    assert task.cancelled() or task.done()
