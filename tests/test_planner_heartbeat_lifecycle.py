"""AREA-2 planner-heartbeat lifecycle (verify turn-end incident 2026-07-17).

The per-consult heartbeat used to be fire-and-forget with an AGENT-shaped
exit (``not is_agent_busy("planner")`` sampled every 75s). Every refire
path respawns the Planner within ~1s of the idle flip — a gap a 75s poll
essentially never observes — so the stale heartbeat re-latched onto the
NEXT consult's busy flag and kept pulsing its own elapsed counter: the
interleaved "49m/50m" + "4/5/6m" double counter the user saw. Contracts
pinned here (``handlers._planner_heartbeat`` + friends):

* CANCEL-ON-POP — the heartbeat task handle is kept in
  ``_planner_heartbeats`` (keyed by the consult's synthetic id) and
  cancelled from every consult exit pop in ``_on_agent_event``
  (``_cancel_planner_heartbeat``): a refire spawned <75s after the
  completion can never inherit a stale counter (THE regression test);
* CONSULT-SHAPED EXIT — belt-and-suspenders behind the cancel: the loop
  breaks when ITS OWN ``_planner_consults`` entry is gone, busy flag or
  not, so even a missed cancel dies at its first tick;
* SINGLE-FLIGHT VERIFY PER SCOPE — ``_handle_consult_planner`` drops a
  non-refire verify whose scope already has a live verify consult or a
  daemon refire in flight (``_verify_refire_pending``), closing the
  back-to-back double-run + double-heartbeat window at the source;
* HONEST CUMULATIVE ELAPSED — the verdictless refire threads
  ``_verify_first_started`` + ``_verify_attempt`` through so the refired
  attempt's heartbeat/notices report scope-level wall-clock instead of
  resetting to zero.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.handlers import (
    _BACKGROUND_TASKS,
    _planner_cap_cooldown,
    _planner_consults,
    _planner_heartbeats,
    _verify_refire_pending,
)
from tests.test_planner_ingest_background import _drain_background
from tests.test_planner_verify_honesty import (
    _arm_consult_spawn,
    _cleanup_background,
    _scope_httpx,
)
from tests.test_review_circuit_breaker import build_harness


@pytest.fixture(autouse=True)
def _clean_module_state():
    _planner_consults.clear()
    _planner_cap_cooldown.clear()
    _planner_heartbeats.clear()
    _verify_refire_pending.clear()
    yield
    _planner_consults.clear()
    _planner_cap_cooldown.clear()
    _planner_heartbeats.clear()
    _verify_refire_pending.clear()


def _consult_handler(h):
    return {
        c.args[0]: c.args[1] for c in h.router.on.call_args_list
    }["consult_planner"]


def _verify_msg(scope_id: str = "scope-1", **extra) -> dict:
    return {
        "mode": "verify",
        "objective": "verify the scope",
        "workstream_id": "ws-1",
        "scope_id": scope_id,
        **extra,
    }


# ---------------------------------------------------------------------------
# THE regression: complete → refire <75s → the old heartbeat died
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refire_within_sleep_window_kills_the_old_heartbeat():
    """Verdictless completion → refire spawned well inside the old
    heartbeat's 75s sleep window (the incident shape). The OLD heartbeat
    must be cancelled on the completion pop; exactly ONE elapsed counter
    (the refired consult's own heartbeat) remains — and the refired
    marker carries the cumulative bookkeeping."""
    h = await build_harness()
    _arm_consult_spawn(h)
    h.mgr._publish_manager_state = AsyncMock()
    handler = _consult_handler(h)

    try:
        # Consult A (first verify attempt).
        await asyncio.wait_for(handler(_verify_msg()), timeout=2.0)
        assert len(_planner_consults) == 1
        synthetic_a = next(iter(_planner_consults))
        marker_a = h.supervisor.spawn_worker.call_args.args[2][
            "planner_consult"
        ]
        assert marker_a["_verify_attempt"] == 1
        assert marker_a["_verify_first_started"] > 0
        heartbeat_a = _planner_heartbeats[synthetic_a]
        assert not heartbeat_a.done()

        # A ends VERDICTLESS — the honesty check refires the SAME consult
        # within ~1s, deep inside heartbeat A's 75s sleep window.
        _client, cls = _scope_httpx({
            "state": "verifying",
            "execution_plan": {"verification": {"status": "pending"}},
        })
        event = {
            "type": "task_complete",
            "task_id": synthetic_a,
            "status": "planning",
            "is_review_completion": True,
            "planner_consult": dict(marker_a),
        }
        with patch("httpx.AsyncClient", cls):
            await asyncio.wait_for(h.on_event("planner", event), timeout=2.0)
            await _drain_background()

        # The OLD heartbeat died with its consult — no stale counter.
        assert heartbeat_a.done(), (
            "the first consult's heartbeat must be cancelled on the "
            "completion pop — a refire <75s later would otherwise "
            "re-latch it onto the new consult (interleaved counters)"
        )
        assert synthetic_a not in _planner_heartbeats
        assert synthetic_a not in _planner_consults

        # Exactly ONE live heartbeat remains: the refired consult B's own.
        assert h.supervisor.spawn_worker.await_count == 2
        synthetic_b = h.supervisor.spawn_worker.call_args.args[2]["task_id"]
        assert synthetic_b != synthetic_a
        live = {
            sid for sid, t in _planner_heartbeats.items() if not t.done()
        }
        assert live == {synthetic_b}

        # Honest cumulative bookkeeping threaded through the refire.
        marker_b = _planner_consults[synthetic_b]
        assert marker_b["_verdictless_refire"] is True
        assert marker_b["_verify_attempt"] == 2
        assert (
            marker_b["_verify_first_started"]
            == marker_a["_verify_first_started"]
        )
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_error_exit_pop_cancels_the_heartbeat_too():
    """The error/kill pop in ``_on_agent_event`` is the OTHER consult exit
    site — it must cancel the heartbeat the same way."""
    h = await build_harness()
    _arm_consult_spawn(h)
    h.mgr._publish_manager_state = AsyncMock()
    handler = _consult_handler(h)

    try:
        await asyncio.wait_for(handler(_verify_msg()), timeout=2.0)
        synthetic_a = next(iter(_planner_consults))
        heartbeat_a = _planner_heartbeats[synthetic_a]

        # Supervisor-synthesized kill (no marker on the event).
        await asyncio.wait_for(h.on_event("planner", {
            "type": "error",
            "fatal": True,
            "reason": "heartbeat_timeout",
            "task_id": synthetic_a,
        }), timeout=2.0)
        await _drain_background()

        assert heartbeat_a.done()
        assert synthetic_a not in _planner_heartbeats
    finally:
        await _cleanup_background()


# ---------------------------------------------------------------------------
# Consult-shaped exit (belt-and-suspenders behind the cancel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_exits_on_stash_pop_even_while_planner_busy():
    """Even WITHOUT the cancel, a heartbeat whose consult left
    ``_planner_consults`` must break at its next tick — the busy flag
    alone (a refired consult) can never keep it alive."""
    h = await build_harness()
    _arm_consult_spawn(h)
    h.mgr._publish_manager_state = AsyncMock()
    handler = _consult_handler(h)

    real_sleep = asyncio.sleep

    async def _fast_sleep(_delay, *args, **kwargs):
        await real_sleep(0)

    state = {"spawned": False}

    async def _spawn(*args, **kwargs):
        state["spawned"] = True
        return True

    # Busy FOREVER once spawned — the exact re-latch condition.
    h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
    h.supervisor.is_agent_busy.side_effect = (
        lambda name: state["spawned"]
    )

    try:
        with patch("asyncio.sleep", _fast_sleep):
            await asyncio.wait_for(handler(_verify_msg()), timeout=2.0)
            synthetic_a = next(iter(_planner_consults))
            heartbeat_a = _planner_heartbeats[synthetic_a]
            # Simulate a pop path that (hypothetically) missed the cancel.
            _planner_consults.pop(synthetic_a)
            for _ in range(200):
                if heartbeat_a.done():
                    break
                await real_sleep(0.01)

        assert heartbeat_a.done()
        assert not heartbeat_a.cancelled(), (
            "the loop must EXIT consult-shaped on its own — this test "
            "deliberately never cancelled it"
        )
        # The finally self-pruned the handle.
        assert synthetic_a not in _planner_heartbeats
    finally:
        await _cleanup_background()


# ---------------------------------------------------------------------------
# Single-flight verify per scope (consult layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_verify_for_same_scope_is_dropped_silently():
    """A live verify consult for scope X dedupes a second verify for X —
    silently (verify posture: no failure poke), before the busy check."""
    h = await build_harness()
    _arm_consult_spawn(h)
    h.mgr._publish_manager_state = AsyncMock()
    handler = _consult_handler(h)

    try:
        await asyncio.wait_for(handler(_verify_msg("scope-1")), timeout=2.0)
        assert h.supervisor.spawn_worker.await_count == 1

        # Same scope again (sweeper/backend refire racing the live run).
        await asyncio.wait_for(handler(_verify_msg("scope-1")), timeout=2.0)
        assert h.supervisor.spawn_worker.await_count == 1  # dropped
        h.mgr.ingest_planner_result.assert_not_awaited()  # silent

        # A DIFFERENT scope is not blocked by scope-1's consult (the
        # dedupe is scope-shaped; the busy-refuse is mocked idle here).
        await asyncio.wait_for(handler(_verify_msg("scope-2")), timeout=2.0)
        assert h.supervisor.spawn_worker.await_count == 2
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_pending_refire_window_dedupes_backend_fired_verify():
    """A verify arriving while a daemon refire for the same scope is in
    its idle-wait/backoff window (old stash popped, Planner idle) must be
    dropped — the exact double-run window the busy check cannot see. The
    refire itself (marker flag) is exempt from its own guard."""
    h = await build_harness()
    _arm_consult_spawn(h)
    h.mgr._publish_manager_state = AsyncMock()
    handler = _consult_handler(h)

    try:
        _verify_refire_pending.add("scope-9")
        await asyncio.wait_for(handler(_verify_msg("scope-9")), timeout=2.0)
        h.supervisor.spawn_worker.assert_not_awaited()  # dropped
        h.mgr.ingest_planner_result.assert_not_awaited()  # silent

        # The refire-flagged consult IS the pending refire — it spawns.
        await asyncio.wait_for(handler(_verify_msg(
            "scope-9", _verdictless_refire=True,
        )), timeout=2.0)
        assert h.supervisor.spawn_worker.await_count == 1
    finally:
        _verify_refire_pending.discard("scope-9")
        await _cleanup_background()


# ---------------------------------------------------------------------------
# Cumulative first-started stamping (non-refire paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_fired_verify_starts_a_fresh_cumulative_clock():
    """A verify with no threaded timestamp (backend/sweeper-fired) stamps
    a fresh ``_verify_first_started`` + attempt 1 — the daemon can only
    thread what it refires itself."""
    h = await build_harness()
    _arm_consult_spawn(h)
    h.mgr._publish_manager_state = AsyncMock()
    handler = _consult_handler(h)

    try:
        await asyncio.wait_for(handler(_verify_msg()), timeout=2.0)
        marker = h.supervisor.spawn_worker.call_args.args[2][
            "planner_consult"
        ]
        assert marker["_verify_attempt"] == 1
        assert isinstance(marker["_verify_first_started"], float)
        assert marker["_verify_first_started"] > 0
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_non_verify_consults_carry_no_verify_bookkeeping():
    h = await build_harness()
    _arm_consult_spawn(h)
    h.mgr._publish_manager_state = AsyncMock()
    handler = _consult_handler(h)

    try:
        await asyncio.wait_for(handler({
            "mode": "roadmap",
            "objective": "map the work",
            "workstream_id": "ws-1",
        }), timeout=2.0)
        marker = h.supervisor.spawn_worker.call_args.args[2][
            "planner_consult"
        ]
        assert "_verify_first_started" not in marker
        assert "_verify_attempt" not in marker
    finally:
        await _cleanup_background()
