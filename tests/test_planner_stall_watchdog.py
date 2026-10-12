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
    _cap_cooldown_key,
    _planner_cap_cooldown,
    _planner_consults,
    _planner_heartbeats,
)
from src.pending_consults import consult_key
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


def _research_key() -> str:
    msg = _research_msg()
    return consult_key(
        "planner", mode=msg["mode"], workstream_id=msg["workstream_id"],
        scope_id=msg["scope_id"], objective=msg["objective"],
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("this_session", [True, False], ids=["running", "leftover"])
async def test_a_running_verify_repeats_its_start(monkeypatch, this_session):
    """A verify session can be quiet for longer than the backend's stuck
    threshold. While the Planner runs THIS verify, the heartbeat repeats
    ``started`` (stall-verify-5); an entry left for another session reports
    nothing."""
    import src.handlers as handlers_module

    monkeypatch.setattr(handlers_module, "_VERIFY_LIVENESS_REPORT_SECONDS", 0.0)
    real_sleep = asyncio.sleep
    with patch(
        "src._handlers._agent_feed.push_agent_feed",
        new_callable=AsyncMock,
    ):
        h = await build_harness()
        _arm_consult_spawn(h)
        h.mgr._publish_manager_state = AsyncMock()
        handler = _consult_handler(h)
        state = {"spawned": False, "pulses_left": 3}

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
        h.supervisor.get_agent_current_task.side_effect = lambda _name: (
            next(iter(_planner_consults), "") if this_session else "planner-other"
        )
        verify = {
            "mode": "verify",
            "objective": "verify the scope",
            "workstream_id": "ws-1",
            "scope_id": "scope-1",
        }
        try:
            with patch("asyncio.sleep", _fast_75s_sleep(real_sleep, 0.01)):
                await asyncio.wait_for(handler(verify), timeout=2.0)
                heartbeat = _planner_heartbeats[next(iter(_planner_consults))]
                assert await _wait_for(lambda: heartbeat.done(), real_sleep)
        finally:
            await _cleanup_background()

    statuses = [
        call.args[0]
        for call in h.router.publish_event.call_args_list
        if call.args and call.args[0].get("type") == "consult_status"
    ]
    assert statuses[0]["state"] == "started" and statuses[0]["detail"] == ""
    repeats = statuses[1:]
    if this_session:
        assert repeats
        assert {(s["state"], s["key"], s["detail"]) for s in repeats} == {
            ("started", "verify:scope-1", "the verify session is still running")
        }
    else:
        assert repeats == []


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
# 3. A failed auto-restart is queued for retry instead of dying silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_auto_restart_is_queued_for_retry(monkeypatch):
    """Stall hotfix 2026-09-29: a restart whose spawn raises is a launch
    failure, parked in the pending-consult ledger and retried; the Manager
    is told when retries give up (``MAX_CONSECUTIVE_LAUNCH_FAILURES``)."""
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
            store = h.supervisor.pending_consult_pump.store
            key = _research_key()
            with patch("asyncio.sleep", _fast_75s_sleep(real_sleep, 0.02)):
                await asyncio.wait_for(handler(_research_msg()), timeout=2.0)
                assert await _wait_for(
                    lambda: store.get_pending_consult(key) is not None,
                    real_sleep,
                ), "a dead refire must be queued, not dropped"

            row = store.get_pending_consult(key)
            assert row["last_reason"] == "launch_failed"
            assert row["payload"].get("_restart_count") == 1
            # Only the killed consult's stash remains (its completion is
            # mocked away); the failed restart left none behind.
            assert all(
                c.get("_watchdog_killed") for c in _planner_consults.values()
            ), "no stash may outlive the failed spawn"
        finally:
            await _cleanup_background()


# ---------------------------------------------------------------------------
# 4. A restart whose kill failed is queued, not absorbed by the dying session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_after_a_failed_kill_is_queued_not_absorbed(
    monkeypatch, caplog,
):
    """The watchdog's kill can fail (a docker exec timeout during cleanup):
    the Planner stays busy on the OLD session and its record is never
    popped. The restart used to be read as a duplicate of that session
    ("request absorbed"): one spawn, no queue row, no Manager poke, and the
    consult was lost. It now waits in the queue and starts once the old
    session is cleaned up, with its restart count."""
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
            state["busy"] = True
            return True

        h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
        h.supervisor.is_agent_busy.side_effect = lambda name: state["busy"]
        # The failed cleanup leaves the old synthetic id as the current task.
        h.supervisor.get_agent_current_task.side_effect = (
            lambda name: next(iter(_planner_consults), None)
        )
        h.supervisor._kill_process = AsyncMock(
            side_effect=RuntimeError("docker exec timed out"),
        )

        try:
            pump = h.supervisor.pending_consult_pump
            key = _research_key()
            with (
                caplog.at_level("WARNING", logger="cbcl.handlers"),
                patch("asyncio.sleep", _fast_75s_sleep(real_sleep, 0.02)),
            ):
                await asyncio.wait_for(handler(_research_msg()), timeout=2.0)
                assert await _wait_for(
                    lambda: pump.store.get_pending_consult(key) is not None,
                    real_sleep,
                ), "the restart must be queued while the old session is busy"

            row = pump.store.get_pending_consult(key)
            assert row["last_reason"] == "agent_busy"
            assert row["payload"].get("_restart_count") == 1
            assert h.supervisor.spawn_worker.await_count == 1
            h.mgr.ingest_planner_result.assert_not_awaited()
            assert any(
                "stall kill failed" in record.getMessage()
                for record in caplog.records
            ), "a failed kill must be visible in the log"

            # The supervisor finishes the cleanup: the queued restart starts.
            state["busy"] = False
            await pump.drain(force=True)
            assert h.supervisor.spawn_worker.await_count == 2
            assert pump.store.get_pending_consult(key) is None
        finally:
            await _cleanup_background()


@pytest.mark.asyncio
async def test_restart_of_a_queue_launched_session_keeps_a_newer_queued_request(
    monkeypatch,
):
    """A session the pump launched from the queue carries the pump's retry
    flag in its message. Its stall restart copied that message, flag
    included, so the restart skipped the queue checks: a newer request
    queued for the same consult while the old session was being killed was
    deleted when the restart spawned (or overwritten when it was parked).

    The restart is the daemon's re-run of the EARLIER request. The newer
    one stands, and the Manager is told the earlier one will not run."""
    monkeypatch.setenv("CUBICLE_PLANNER_STALL_SECONDS", "0.01")
    real_sleep = asyncio.sleep
    # ``materialize`` keeps one request per scope (research is keyed per
    # question) and needs no pre-spawn snapshot to mock.
    earlier = {
        "mode": "materialize",
        "objective": "author the scope's tasks",
        "workstream_id": "ws-1",
        "scope_id": "scope-1",
        "trigger": "manager",
    }
    newer = {**earlier, "objective": "author them for the revised spec"}
    key = consult_key(
        "planner", mode="materialize", workstream_id="ws-1", scope_id="scope-1",
    )

    with patch(
        "src._handlers._agent_feed.push_agent_feed",
        new_callable=AsyncMock,
    ):
        h = await build_harness()
        _arm_consult_spawn(h)
        h.mgr._publish_manager_state = AsyncMock()
        handler = _consult_handler(h)
        pump = h.supervisor.pending_consult_pump

        state = {"busy": True, "current": None}
        h.supervisor.is_agent_busy.side_effect = lambda name: state["busy"]
        h.supervisor.get_agent_current_task.side_effect = (
            lambda name: state["current"]
        )

        async def _spawn(name, config, task):
            state["busy"], state["current"] = True, task["task_id"]
            return True

        async def _kill(name):
            # The Manager asks again, for something else, while the stalled
            # session is being killed: the Planner is still busy.
            await handler(dict(newer))
            state["busy"], state["current"] = False, None

        h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
        h.supervisor._kill_process = AsyncMock(side_effect=_kill)

        try:
            # The Planner is busy: the request is queued, then launched by
            # the pump once the Planner frees.
            await handler(dict(earlier))
            assert pump.store.get_pending_consult(key) is not None
            state["busy"] = False
            with patch("asyncio.sleep", _fast_75s_sleep(real_sleep, 0.02)):
                await pump.drain(force=True)
                assert h.supervisor.spawn_worker.await_count == 1
                assert await _wait_for(
                    lambda: h.mgr.ingest_planner_result.await_count >= 1
                    or h.supervisor.spawn_worker.await_count >= 2,
                    real_sleep,
                ), "the watchdog must restart the stalled consult"

            row = pump.store.get_pending_consult(key)
            assert row is not None, "the newer queued request was deleted"
            assert row["payload"]["objective"] == newer["objective"]
            assert "_restart_count" not in row["payload"]
            # The earlier request ran once; its restart was dropped.
            assert h.supervisor.spawn_worker.await_count == 1
            (note,) = [
                call.args[0]
                for call in h.mgr.ingest_planner_result.await_args_list
            ]
            assert note["planner_superseded"] == "rerun"
            assert note["replaced_objective"] == earlier["objective"]
            assert not note.get("kept_running")

            # The newer request is the one the queue starts next.
            await pump.drain(force=True)
            assert h.supervisor.spawn_worker.await_count == 2
            started = h.supervisor.spawn_worker.await_args.args[2]
            assert started["planner_consult"]["objective"] == newer["objective"]
            assert pump.store.get_pending_consult(key) is None
        finally:
            await _cleanup_background()


# ---------------------------------------------------------------------------
# 5. The activity stamp itself (_on_agent_event → consult stash)
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


# ---------------------------------------------------------------------------
# 6. The cap cooldown is keyed like the consult queue
# ---------------------------------------------------------------------------


def test_the_cap_cooldown_is_keyed_like_the_consult_queue():
    """One cooldown per consult, as the queue counts consults: per
    workstream, scope and mode, and per question for research (letter case
    and spacing ignored). Two markers share a cooldown exactly when they
    share a queue key. (Verification has no cooldown: the stall watchdog
    never restarts it.)"""

    def queue_key(marker: dict) -> str:
        return consult_key(
            "planner", mode=marker["mode"],
            workstream_id=marker["workstream_id"], scope_id=marker["scope_id"],
            objective=marker["objective"],
        )

    markers = [
        {"mode": mode, "objective": objective, "workstream_id": workstream,
         "scope_id": scope}
        for mode in ("specify", "scope_plan", "materialize", "research")
        for objective in ("compare vendors", "  Compare   VENDORS ", "GDPR rules")
        for workstream in ("ws-1", "ws-2")
        for scope in ("scope-1", "")
    ]
    for first in markers:
        for second in markers:
            assert (
                _cap_cooldown_key(first) == _cap_cooldown_key(second)
            ) == (queue_key(first) == queue_key(second)), (first, second)
    # A bare truthy marker (legacy and error paths) never raises.
    assert _cap_cooldown_key(True) == _cap_cooldown_key({})


@pytest.mark.asyncio
async def test_a_capped_research_question_does_not_refuse_the_other_questions(
    monkeypatch,
):
    """Research is queued per question, so its stall cooldown is too. One
    question that stalled to the restart cap used to refuse every other
    question queued for the same workstream and scope as "STALLED
    repeatedly on this exact consult", though it had never run."""
    monkeypatch.setenv("CUBICLE_PLANNER_STALL_SECONDS", "0.01")
    monkeypatch.setenv("CUBICLE_PLANNER_MAX_RESTARTS", "0")
    real_sleep = asyncio.sleep
    stalled = _research_msg(trigger="manager")
    other = _research_msg(objective="compare the vendors", trigger="manager")

    with patch(
        "src._handlers._agent_feed.push_agent_feed",
        new_callable=AsyncMock,
    ):
        h = await build_harness()
        _arm_consult_spawn(h)
        h.mgr._publish_manager_state = AsyncMock()
        handler = _consult_handler(h)
        pump = h.supervisor.pending_consult_pump

        # The Planner is busy with another consult: both questions queue.
        state = {"busy": True, "current": None}
        h.supervisor.is_agent_busy.side_effect = lambda name: state["busy"]
        h.supervisor.get_agent_current_task.side_effect = (
            lambda name: state["current"]
        )

        async def _spawn(name, config, task):
            state["busy"], state["current"] = True, task["task_id"]
            return True

        async def _kill(name):
            # As in production: the killed worker flushes its cancelled
            # task_complete, which removes the consult's record.
            sid = state["current"]
            state["busy"], state["current"] = False, None
            await h.on_event("planner", {
                "type": "task_complete",
                "task_id": sid,
                "status": "cancelled",
                "planner_consult": dict(_planner_consults[sid]),
            })

        h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
        h.supervisor._kill_process = AsyncMock(side_effect=_kill)

        def _pokes() -> list[dict]:
            return [
                call.args[0]
                for call in h.mgr.ingest_planner_result.await_args_list
            ]

        try:
            await handler(dict(stalled))
            await handler(dict(other))
            assert [
                row["payload"]["objective"]
                for row in pump.store.list_pending_consults()
            ] == [stalled["objective"], other["objective"]]

            # The Planner frees: the first question starts, stalls and hits
            # the restart cap.
            state["busy"] = False
            with patch("asyncio.sleep", _fast_75s_sleep(real_sleep, 0.02)):
                await pump.drain(force=True)
                assert h.supervisor.spawn_worker.await_count == 1
                assert await _wait_for(
                    lambda: h.mgr.ingest_planner_result.await_count >= 1,
                    real_sleep,
                ), "the stalled question must end in the give-up poke"
            (gave_up,) = _pokes()
            assert gave_up.get("planner_stall_cap") is True
            assert gave_up["planner_consult"]["objective"] == stalled["objective"]
            assert list(_planner_cap_cooldown) == [_cap_cooldown_key(stalled)]

            # The other question never ran: the queue starts it.
            await pump.drain(force=True)
            assert h.supervisor.spawn_worker.await_count == 2
            started = h.supervisor.spawn_worker.await_args.args[2]
            assert started["planner_consult"]["objective"] == other["objective"]
            assert pump.store.list_pending_consults() == []
            assert len(_pokes()) == 1, "the other question must not be refused"

            # It reports back. The stalled question, asked again, is still
            # in its cooldown: refused once, with the reason, and not run.
            _planner_consults.clear()
            state["busy"], state["current"] = False, None
            await handler({**stalled, "objective": "  Investigate THE  corpus "})
            assert h.supervisor.spawn_worker.await_count == 2
            assert pump.store.list_pending_consults() == []
            refused = _pokes()[-1]
            assert len(_pokes()) == 2
            assert "STALLED repeatedly" in refused["planner_error"]
            statuses = [
                call.args[0]
                for call in h.router.publish_event.call_args_list
                if call.args and call.args[0].get("type") == "consult_status"
            ]
            assert (statuses[-1]["state"], statuses[-1]["reason"]) == (
                "failed", "stall_cooldown",
            )
        finally:
            await _cleanup_background()


# ---------------------------------------------------------------------------
# 7. A session abandoned at the restart cap is not the consult of record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_after_a_failed_cap_kill_is_refused_not_absorbed(
    monkeypatch,
):
    """At the restart cap the watchdog abandons the session for good. If its
    kill fails, the Planner stays busy on that session and its record is
    never removed. The same request, asked again, must not be read as a
    duplicate of the dead session ("request absorbed") and lost. The stall
    cooldown answers it at once, with a poke, although the Planner is still
    busy. It used to wait in the queue behind the busy Planner (told it
    would start automatically), and was refused only if the Planner freed
    within the cooldown, or else started the stall loop again late."""
    monkeypatch.setenv("CUBICLE_PLANNER_STALL_SECONDS", "0.01")
    monkeypatch.setenv("CUBICLE_PLANNER_MAX_RESTARTS", "0")
    real_sleep = asyncio.sleep
    msg = _research_msg(trigger="manager")

    with patch(
        "src._handlers._agent_feed.push_agent_feed",
        new_callable=AsyncMock,
    ):
        h = await build_harness()
        _arm_consult_spawn(h)
        h.mgr._publish_manager_state = AsyncMock()
        handler = _consult_handler(h)
        pump = h.supervisor.pending_consult_pump

        state = {"busy": False}

        async def _spawn(*args, **kwargs):
            state["busy"] = True
            return True

        h.supervisor.spawn_worker = AsyncMock(side_effect=_spawn)
        h.supervisor.is_agent_busy.side_effect = lambda name: state["busy"]
        # The failed cleanup leaves the old synthetic id as the current task.
        h.supervisor.get_agent_current_task.side_effect = (
            lambda name: next(iter(_planner_consults), None)
        )
        h.supervisor._kill_process = AsyncMock(
            side_effect=RuntimeError("docker exec timed out"),
        )

        def _pokes() -> list[dict]:
            return [
                call.args[0]
                for call in h.mgr.ingest_planner_result.await_args_list
            ]

        try:
            key = _research_key()
            with patch("asyncio.sleep", _fast_75s_sleep(real_sleep, 0.02)):
                await asyncio.wait_for(handler(dict(msg)), timeout=2.0)
                assert await _wait_for(
                    lambda: h.mgr.ingest_planner_result.await_count >= 1,
                    real_sleep,
                ), "the restart cap must end in the give-up poke"
            (gave_up,) = _pokes()
            assert gave_up.get("planner_stall_cap") is True
            # The kill failed: the abandoned session's record is still there.
            (abandoned,) = _planner_consults.values()
            assert abandoned["_watchdog_killed"] == "cap"

            def _statuses() -> list[dict]:
                return [
                    call.args[0]
                    for call in h.router.publish_event.call_args_list
                    if call.args and call.args[0].get("type") == "consult_status"
                ]

            reported = len(_statuses())
            await handler(dict(msg))
            # Refused at once while the Planner is still busy: neither
            # absorbed by the abandoned session nor parked behind it.
            assert len(_pokes()) == 2, "absorbed by the session the watchdog gave up on"
            assert "STALLED repeatedly" in _pokes()[-1]["planner_error"]
            assert pump.store.get_pending_consult(key) is None
            assert [
                (status["state"], status["reason"]) for status in _statuses()[reported:]
            ] == [("failed", "stall_cooldown")]
            assert h.supervisor.spawn_worker.await_count == 1

            # The supervisor finishes the cleanup: nothing was queued, so
            # nothing starts and nothing more is said.
            state["busy"] = False
            await pump.drain(force=True)
            assert h.supervisor.spawn_worker.await_count == 1
            assert len(_pokes()) == 2
        finally:
            await _cleanup_background()
