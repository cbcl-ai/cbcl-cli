"""Planner stall watchdog — incident 2026-08-04 (Presale Office, FO-002.S03).

Three defects wedged a healthy Office Foundry run for hours:

1. WALL-CLOCK KILLS — the per-consult stall watchdog compared total
   elapsed time against the stall ceiling, so a HEALTHY consult that was
   visibly streaming tool activity was killed at exactly the ceiling
   (the observed 40:00 SIGTERM of a streaming S03 scope_plan). The
   watchdog now measures SILENCE: ``_on_agent_event`` stamps
   ``_last_activity_monotonic`` on the consult's ``_planner_consults``
   stash entry for every Planner ``progress`` frame, and the stall
   branch fires only after ``stall_after`` seconds of NO output.

2. THE POP-CANCEL RACE — the watchdog's kill makes the worker flush a
   cancelled ``task_complete``, whose ``_on_agent_event`` pop calls
   ``_cancel_planner_heartbeat`` — which cancelled the WATCHDOG TASK
   ITSELF mid-intervention (it runs inside the heartbeat), silently
   losing the auto-restart refire / the cap's give-up poke. The
   intervention now self-deregisters its handle from
   ``_planner_heartbeats`` before killing, making that cancel a no-op.

3. SILENT RESTART FAILURE — a refire that raised was logged and
   dropped; the consult's own completion was already suppressed
   (``_watchdog_killed``), so nothing ever told the Manager. The
   watchdog now sends the honest failure poke when the refire dies.

Contracts pinned here (``handlers._planner_heartbeat`` + friends):

* a consult with fresh activity is NEVER killed, however long it runs;
* a genuinely silent consult still dies at the ceiling, the kill/refire
  chain is bounded by ``CUBICLE_PLANNER_MAX_RESTARTS``, ends in ONE
  give-up poke (``planner_stall_cap``) + the cap cooldown — and the
  chain survives the production pop-cancel race at every kill;
* a failed auto-restart pokes the Manager instead of dying silently.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from src.handlers import (
    _BACKGROUND_TASKS,
    _planner_cap_cooldown,
    _planner_consults,
    _planner_heartbeats,
)
from tests.test_planner_consult_outcome_gates import (
    _arm_consult_spawn,
    _cleanup_background,
)
from tests.test_review_circuit_breaker import build_harness


@pytest.fixture(autouse=True)
def _clean_module_state():
    _planner_consults.clear()
    _planner_cap_cooldown.clear()
    _planner_heartbeats.clear()
    yield
    _planner_consults.clear()
    _planner_cap_cooldown.clear()
    _planner_heartbeats.clear()


def _consult_handler(h):
    return {
        c.args[0]: c.args[1] for c in h.router.on.call_args_list
    }["consult_planner"]


def _research_msg(**extra) -> dict:
    # ``research`` is non-verify (the stall branch applies) and NOT
    # outcome-gated (no pre-spawn httpx snapshot to mock).
    return {
        "mode": "research",
        "objective": "investigate the corpus",
        "workstream_id": "ws-1",
        "scope_id": "scope-1",
        **extra,
    }


def _fast_75s_sleep(real_sleep, tick_seconds: float = 0.0):
    """Map the heartbeat's 75s tick to ``tick_seconds`` real time and
    every other sleep to a zero-yield."""

    async def _sleep(delay, *args, **kwargs):
        if delay >= 74:
            await real_sleep(tick_seconds)
        else:
            await real_sleep(0)

    return _sleep


async def _wait_for(predicate, real_sleep, iterations: int = 400) -> bool:
    for _ in range(iterations):
        if predicate():
            return True
        await real_sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# 1. Idle-based stall detection: streaming consults survive wall-clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_consult_survives_past_the_wall_clock_ceiling(
    monkeypatch,
):
    """A consult whose activity clock stays fresh outlives the stall
    ceiling by many ticks — the pre-fix wall-clock check would have
    killed it at the first tick past ``stall_after``."""
    monkeypatch.setenv("CUBICLE_PLANNER_STALL_SECONDS", "0.05")
    real_sleep = asyncio.sleep

    with patch(
        "src._handlers._agent_feed.push_agent_feed",
        new_callable=AsyncMock,
    ):
        h = await build_harness()
        _arm_consult_spawn(h)
        h.mgr._publish_manager_state = AsyncMock()
        h.supervisor._kill_process = AsyncMock()
        handler = _consult_handler(h)

        state = {"spawned": False, "pulses_left": 6}

        async def _spawn(*args, **kwargs):
            state["spawned"] = True
            return True

        def _busy(name):
            if not state["spawned"]:
                return False
            if state["pulses_left"] > 0:
                state["pulses_left"] -= 1
                return True
            return False

        h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
        h.supervisor.is_agent_busy.side_effect = _busy

        try:
            # Each 75s tick costs 0.02s real time → 6 pulses ≈ 0.12s of
            # wall-clock, far past the 0.05s stall ceiling.
            with patch(
                "asyncio.sleep", _fast_75s_sleep(real_sleep, 0.02),
            ):
                await asyncio.wait_for(handler(_research_msg()), timeout=2.0)
                sid = next(iter(_planner_consults))
                # Continuously-active consult: the activity stamp sits in
                # the future for the test's duration, so idle stays < 0
                # at every tick while elapsed blows through the ceiling.
                _planner_consults[sid]["_last_activity_monotonic"] = (
                    time.monotonic() + 3600
                )
                heartbeat = _planner_heartbeats[sid]
                assert await _wait_for(
                    lambda: heartbeat.done(), real_sleep,
                ), "heartbeat should exit consult-shaped once idle"

            # Never killed, never refired, never poked a failure.
            h.supervisor._kill_process.assert_not_awaited()
            assert h.supervisor.spawn_worker.await_count == 1
            h.mgr.ingest_planner_result.assert_not_awaited()
            # The pulse branch actually ran repeatedly (we were past the
            # ceiling in elapsed terms the whole time).
            assert h.mgr._publish_manager_state.await_count >= 5
        finally:
            await _cleanup_background()


# ---------------------------------------------------------------------------
# 2. Silent consult: bounded kill/refire chain that survives the
#    production pop-cancel race and ends in ONE honest give-up poke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_consult_chain_is_capped_and_survives_pop_cancel(
    monkeypatch,
):
    """No activity at all → the watchdog kills at the ceiling and
    refires; each kill triggers the PRODUCTION sequence (the worker's
    cancelled ``task_complete`` → ``_on_agent_event`` pop →
    ``_cancel_planner_heartbeat``). Pre-fix, that cancel killed the
    intervening watchdog inside ``_kill_process`` and the chain died
    silently at the FIRST kill (the observed S03 wedge: kill at +40:00,
    no respawn, no poke). Post-fix the chain runs to the restart cap:
    3 spawns, 3 kills, ONE give-up poke, cooldown armed."""
    monkeypatch.setenv("CUBICLE_PLANNER_STALL_SECONDS", "0.01")
    monkeypatch.setenv("CUBICLE_PLANNER_MAX_RESTARTS", "2")
    real_sleep = asyncio.sleep

    with patch(
        "src._handlers._agent_feed.push_agent_feed",
        new_callable=AsyncMock,
    ):
        h = await build_harness()
        _arm_consult_spawn(h)
        h.mgr._publish_manager_state = AsyncMock()
        handler = _consult_handler(h)

        state = {"busy": False}

        async def _spawn(*args, **kwargs):
            state["busy"] = True
            return True

        h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
        h.supervisor.is_agent_busy.side_effect = lambda name: state["busy"]

        async def _kill(agent_name):
            state["busy"] = False
            # THE RACE: the killed worker flushes its cancelled
            # task_complete before exiting; the supervisor reader routes
            # it through _on_agent_event while the watchdog awaits the
            # kill — popping the stash AND calling
            # _cancel_planner_heartbeat on the intervening task.
            sids = list(_planner_consults)
            if sids:
                sid = sids[-1]
                await h.on_event("planner", {
                    "type": "task_complete",
                    "task_id": sid,
                    "status": "cancelled",
                    "planner_consult": dict(_planner_consults[sid]),
                })
            await real_sleep(0)  # let a (pre-fix) cancel land mid-await

        h.supervisor._kill_process = AsyncMock(side_effect=_kill)

        try:
            with patch("asyncio.sleep", _fast_75s_sleep(real_sleep, 0.02)):
                await asyncio.wait_for(handler(_research_msg()), timeout=2.0)
                assert await _wait_for(
                    lambda: h.mgr.ingest_planner_result.await_count >= 1,
                    real_sleep,
                ), (
                    "the stall chain must end in the give-up poke — a "
                    "silent death means the pop-cancel race ate the "
                    "intervention again"
                )

            # Original + exactly MAX_RESTARTS refires, each killed.
            assert h.supervisor.spawn_worker.await_count == 3
            assert h.supervisor._kill_process.await_count == 3
            # ONE authoritative give-up poke, honestly labelled.
            h.mgr.ingest_planner_result.assert_awaited_once()
            payload = h.mgr.ingest_planner_result.await_args.args[0]
            assert payload.get("planner_stall_cap") is True
            assert "auto-restart cap" in payload.get("planner_error", "")
            # Cap cooldown armed against an immediate re-consult loop.
            assert _planner_cap_cooldown, "cap cooldown must be armed"
        finally:
            await _cleanup_background()


# ---------------------------------------------------------------------------
# 3. A failed auto-restart pokes the Manager instead of dying silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_auto_restart_pokes_the_manager(monkeypatch):
    monkeypatch.setenv("CUBICLE_PLANNER_STALL_SECONDS", "0.01")
    real_sleep = asyncio.sleep

    with patch(
        "src._handlers._agent_feed.push_agent_feed",
        new_callable=AsyncMock,
    ):
        h = await build_harness()
        _arm_consult_spawn(h)
        h.mgr._publish_manager_state = AsyncMock()
        handler = _consult_handler(h)

        state = {"busy": False}

        async def _spawn(*args, **kwargs):
            if h.supervisor.spawn_worker.await_count > 1:
                raise RuntimeError("spawn infrastructure down")
            state["busy"] = True
            return True

        async def _kill(agent_name):
            state["busy"] = False

        h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
        h.supervisor.is_agent_busy.side_effect = lambda name: state["busy"]
        h.supervisor._kill_process = AsyncMock(side_effect=_kill)

        try:
            with patch("asyncio.sleep", _fast_75s_sleep(real_sleep, 0.02)):
                await asyncio.wait_for(handler(_research_msg()), timeout=2.0)
                assert await _wait_for(
                    lambda: h.mgr.ingest_planner_result.await_count >= 1,
                    real_sleep,
                ), "a dead refire must surface as a failure poke"

            payload = h.mgr.ingest_planner_result.await_args.args[0]
            assert "automatic restart failed" in (
                payload.get("planner_error") or ""
            )
        finally:
            await _cleanup_background()


# ---------------------------------------------------------------------------
# 4. The activity stamp itself (_on_agent_event → consult stash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_progress_frames_stamp_the_consult_activity_clock():
    h = await build_harness()
    _arm_consult_spawn(h)
    h.mgr._publish_manager_state = AsyncMock()
    handler = _consult_handler(h)

    try:
        await asyncio.wait_for(handler(_research_msg()), timeout=2.0)
        sid = next(iter(_planner_consults))
        assert "_last_activity_monotonic" not in _planner_consults[sid]

        before = time.monotonic()
        await asyncio.wait_for(h.on_event("planner", {
            "type": "progress",
            "task_id": sid,
            "event_type": "tool_run",
            "content": "Read /workspace/spec.md",
        }), timeout=2.0)
        stamp = _planner_consults[sid].get("_last_activity_monotonic")
        assert isinstance(stamp, float) and stamp >= before

        # A frame for an UNKNOWN task id stamps nothing (no KeyError).
        await asyncio.wait_for(h.on_event("planner", {
            "type": "progress",
            "task_id": "planner-goneaway",
            "event_type": "checkpoint",
            "content": "late frame",
        }), timeout=2.0)
    finally:
        await _cleanup_background()
