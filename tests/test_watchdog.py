"""Tests for TaskWatchdog — crash recovery for stuck in-progress tasks.

The watchdog now only handles:
- In-progress tasks with no active agent session (crash recovery)
Review and blocked tasks are handled by the Manager Assistant via per-agent queues.

Crash-recovery contract (T1.1.1): NO ``in_progress → ready`` move is ever
issued (the backend rejects that transition). Recovery is re-spawn-in-place
via ``dispatcher.add_task`` (queue re-add, no status flip); after 3 crashes
the task is moved ``in_progress → blocked`` with an ESCALATED comment so the
Manager Assistant triages it.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.watchdog import RECENTLY_DISPATCHED_TTL, TaskWatchdog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws(board_items=None, detail=None) -> AsyncMock:
    """Create a mock WS/HTTP client."""
    ws = AsyncMock()

    async def _request(method, payload=None, **kwargs):
        if method == "get_board":
            return {"items": board_items or []}
        if method == "get_task_detail":
            return detail or {"brief": {"goal": "test"}, "title": "T"}
        if method == "move_task":
            return {"ok": True}
        if method == "update_task":
            return {"ok": True}
        return {}

    ws.request = AsyncMock(side_effect=_request)
    return ws


def _make_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.is_busy = False
    return mgr


def _make_config(agents=None) -> MagicMock:
    cfg = MagicMock()
    agents = agents or {"analyst": {"name": "analyst"}}
    cfg.get_agent = MagicMock(side_effect=lambda n: agents.get(n))
    return cfg


def _make_supervisor(busy=None, can_spawn=True) -> MagicMock:
    sup = MagicMock()
    busy_set = busy or set()
    sup.is_agent_busy = MagicMock(side_effect=lambda n: n in busy_set)
    sup.can_spawn.return_value = can_spawn
    sup.get_agent_current_task.return_value = None
    return sup


def _make_dispatcher() -> MagicMock:
    disp = MagicMock()
    disp.add_task = AsyncMock()
    disp.wake = MagicMock()
    return disp


# ---------------------------------------------------------------------------
# Init tests
# ---------------------------------------------------------------------------


class TestInit:
    """Tests for TaskWatchdog initialization."""

    def test_creates_with_process_model_components(self):
        # message_router was removed from TaskWatchdog — it's no longer
        # needed since re-dispatch goes through the shared
        # ``dispatcher`` rather than a dedicated router channel.
        sup = _make_supervisor()
        disp = _make_dispatcher()

        wd = TaskWatchdog(
            ws=_make_ws(), executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1",
            supervisor=sup, dispatcher=disp,
        )
        assert wd._supervisor is sup
        assert wd._dispatcher is disp


class TestWake:
    """Tests for wake() signal."""

    def test_wake_sets_event(self):
        wd = TaskWatchdog(
            ws=_make_ws(), executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1",
        )
        assert not wd._wake_event.is_set()
        wd.wake()
        assert wd._wake_event.is_set()


class TestCrashStateAccessors:
    """T8/1.1+2.1: read-only crash-state accessors the dispatcher consults."""

    def _wd(self):
        return TaskWatchdog(
            ws=_make_ws(), executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None, office_id="off1",
        )

    def test_respawn_capped_false_below_cap(self):
        wd = self._wd()
        assert wd.respawn_capped("t1") is False
        wd._task_crash_count["t1"] = 2  # below MAX_CRASH_RESPAWNS (3)
        assert wd.respawn_capped("t1") is False

    def test_respawn_capped_true_at_cap(self):
        wd = self._wd()
        wd._task_crash_count["t1"] = 3
        assert wd.respawn_capped("t1") is True

    def test_respawn_capped_true_when_escalated(self):
        wd = self._wd()
        wd._blocked_escalated.add("t1")  # move-failed-but-escalated case
        assert wd.respawn_capped("t1") is True

    def test_is_crash_recovering(self):
        wd = self._wd()
        assert wd.is_crash_recovering("t1") is False
        wd._task_crash_count["t1"] = 1
        assert wd.is_crash_recovering("t1") is True
        wd._task_crash_count.pop("t1")
        wd._blocked_escalated.add("t1")
        assert wd.is_crash_recovering("t1") is True


# ---------------------------------------------------------------------------
# _handle_in_progress tests (crash recovery)
# ---------------------------------------------------------------------------


class TestHandleInProgress:
    """Tests for _handle_in_progress crash recovery."""

    @pytest.mark.asyncio
    async def test_skips_when_agent_busy(self):
        sup = _make_supervisor(busy={"analyst"})
        ws = _make_ws()

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )

        task = {"id": "t1", "readable_id": "X", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)
        ws.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_requeues_in_place_when_agent_idle_no_ready_move(self):
        """Stuck in_progress + idle agent → re-spawn-in-place: the task is
        re-added to the executor's queue (status unchanged) and NO
        move_task is issued (``in_progress → ready`` is backend-rejected)."""
        sup = _make_supervisor()
        ws = _make_ws()
        disp = _make_dispatcher()

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup, dispatcher=disp,
        )

        task = {"id": "t1", "readable_id": "WR-001.T01", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        # NO move_task of any kind — recovery is a queue re-add.
        move_calls = [
            call for call in ws.request.call_args_list
            if call[0][0] == "move_task"
        ]
        assert len(move_calls) == 0

        disp.add_task.assert_awaited_once()
        queued = disp.add_task.call_args[0][0]
        assert queued["task_id"] == "t1"
        assert queued["assigned_agent"] == "analyst"
        assert queued["status"] == "in_progress"
        assert wd._task_crash_count["t1"] == 1
        # Grace window stamped so the next tick doesn't double-count.
        assert "t1" in wd._recently_dispatched

    @pytest.mark.asyncio
    async def test_moves_to_blocked_after_3_crashes(self):
        """3rd crash tick → exactly one ``in_progress → blocked`` move with
        the ESCALATED template + error_class annotation; no re-spawn."""
        sup = _make_supervisor()
        ws = _make_ws(detail={
            "brief": {"goal": "test"},
            "title": "T",
            "recent_activities": [
                {
                    "event_type": "error",
                    "details": {"error_class": "output_token_limit"},
                    "content": "boom",
                },
            ],
        })
        disp = _make_dispatcher()

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup, dispatcher=disp,
        )
        wd._task_crash_count["t1"] = 3

        task = {"id": "t1", "readable_id": "WR-001.T01", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        move_calls = [
            call for call in ws.request.call_args_list
            if call[0][0] == "move_task"
        ]
        assert len(move_calls) == 1
        params = move_calls[0][0][1]
        assert params["new_status"] == "blocked"
        assert params["comment"].startswith("ESCALATED (output_token_limit):")
        # No re-spawn alongside the escalation.
        disp.add_task.assert_not_awaited()
        # Marked escalated so subsequent ticks are silent.
        assert "t1" in wd._blocked_escalated

    @pytest.mark.asyncio
    async def test_no_further_moves_or_spawns_after_escalation(self):
        """4th tick (after the blocked move was issued) → no further
        spawns or moves while the move is still landing on the board."""
        sup = _make_supervisor()
        ws = _make_ws()
        disp = _make_dispatcher()

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup, dispatcher=disp,
        )
        wd._task_crash_count["t1"] = 3
        wd._blocked_escalated.add("t1")

        task = {"id": "t1", "readable_id": "WR-001.T01", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        move_calls = [
            call for call in ws.request.call_args_list
            if call[0][0] == "move_task"
        ]
        assert len(move_calls) == 0
        disp.add_task.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_escalation_uses_unknown_fatal_without_error_activity(self):
        """No `error` activity on the task → class defaults to unknown_fatal."""
        sup = _make_supervisor()
        ws = _make_ws()  # default detail has no activities

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )
        wd._task_crash_count["t1"] = 3

        task = {"id": "t1", "readable_id": "WR-001.T01", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        move_calls = [
            call for call in ws.request.call_args_list
            if call[0][0] == "move_task"
        ]
        assert len(move_calls) == 1
        assert move_calls[0][0][1]["comment"].startswith(
            "ESCALATED (unknown_fatal):"
        )

    @pytest.mark.asyncio
    async def test_skips_recently_dispatched(self):
        sup = _make_supervisor()
        ws = _make_ws()

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )
        wd._recently_dispatched["t1"] = time.monotonic()

        task = {"id": "t1", "readable_id": "X", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        # Should not move — recently dispatched
        move_calls = [
            call for call in ws.request.call_args_list
            if call[0][0] == "move_task"
        ]
        assert len(move_calls) == 0

    @pytest.mark.asyncio
    async def test_dispatched_ttl_expires(self):
        sup = _make_supervisor()
        ws = _make_ws()
        disp = _make_dispatcher()

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup, dispatcher=disp,
        )
        # Expired TTL
        wd._recently_dispatched["t1"] = time.monotonic() - RECENTLY_DISPATCHED_TTL - 1

        task = {"id": "t1", "readable_id": "WR-001.T01", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        # Should re-queue (re-spawn-in-place) after TTL expired.
        disp.add_task.assert_awaited_once()
        assert wd._task_crash_count["t1"] == 1

    @pytest.mark.asyncio
    async def test_skips_no_agent(self):
        sup = _make_supervisor()
        ws = _make_ws()

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )

        task = {"id": "t1", "readable_id": "X", "assigned_agent": ""}
        await wd._handle_in_progress(task)
        ws.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_move_failure_increments_counter(self):
        """A failed ``in_progress → blocked`` escalation increments
        ``_move_failed`` so the watchdog retries up to 3 times (the
        breaker no longer counts re-spawn re-queues here)."""
        sup = _make_supervisor()
        ws = AsyncMock()

        async def _failing_request(method, payload=None, **kwargs):
            if method == "move_task":
                return {"error": "forbidden"}
            return {"items": []}

        ws.request = AsyncMock(side_effect=_failing_request)

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )
        wd._task_crash_count["t1"] = 3

        task = {"id": "t1", "readable_id": "X", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        assert wd._move_failed.get("t1", 0) == 1
        # Not marked escalated — the move didn't land.
        assert "t1" not in wd._blocked_escalated


# ---------------------------------------------------------------------------
# _check_board tests
# ---------------------------------------------------------------------------


class TestCheckBoard:
    """Tests for the full _check_board cycle."""

    @pytest.mark.asyncio
    async def test_handles_empty_board(self):
        ws = _make_ws(board_items=[])
        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1",
        )
        await wd._check_board()

    @pytest.mark.asyncio
    async def test_handles_ws_error(self):
        ws = AsyncMock()
        ws.request = AsyncMock(side_effect=Exception("connection lost"))
        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1",
        )
        # Should not raise
        await wd._check_board()

    @pytest.mark.asyncio
    async def test_prunes_tracking_dicts(self):
        sup = _make_supervisor(busy={"analyst"})
        ws = _make_ws(board_items=[
            {"id": "t1", "status": "in_progress", "assigned_agent": "analyst"},
        ])
        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )
        wd._task_crash_count["t-gone"] = 2
        wd._move_failed["t-gone"] = 1

        await wd._check_board()

        # t-gone should be pruned (not on board), t1 still there
        assert "t-gone" not in wd._task_crash_count
        assert "t-gone" not in wd._move_failed

    @pytest.mark.asyncio
    async def test_releases_escalation_marker_when_task_leaves_in_progress(self):
        """Once the blocked move lands (task no longer in_progress), the
        circuit-breaker marker + crash count reset so a future genuine
        retry gets a fresh budget."""
        sup = _make_supervisor()
        ws = _make_ws(board_items=[
            {"id": "t1", "status": "blocked", "assigned_agent": "analyst"},
        ])
        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )
        wd._blocked_escalated.add("t1")
        wd._task_crash_count["t1"] = 3

        await wd._check_board()

        assert "t1" not in wd._blocked_escalated
        assert "t1" not in wd._task_crash_count

    @pytest.mark.asyncio
    async def test_crash_budget_resets_when_task_completes_to_review(self):
        """Loop-2 regression: a task that crashed mid-attempt then COMPLETED
        (in_progress -> review) must get a FRESH crash budget. The per-attempt
        counter must not leak across the review -> rework round-trip and
        force-block the rework after fewer than the intended crashes. (The
        prior code pruned by active_ids, which kept the count alive because a
        review task is still on the board.)"""
        sup = _make_supervisor()
        ws = _make_ws(board_items=[
            {"id": "t1", "status": "review", "assigned_agent": "analyst"},
        ])
        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )
        wd._task_crash_count["t1"] = 2  # crashed twice during the first attempt
        wd._move_failed["t1"] = 1

        await wd._check_board()

        # No longer in_progress → budget reset, so the rework attempt starts
        # fresh (won't hit the 3-crash cap after a single rework crash).
        assert "t1" not in wd._task_crash_count
        assert "t1" not in wd._move_failed

    @pytest.mark.asyncio
    async def test_board_fetch_scoped_to_active_statuses(self):
        """Loop-2 (T1.1.1 robustness): the watchdog fetches only
        ready,in_progress with a high limit so the active set is never
        truncated by the default 100-row page (which would silently
        un-enforce the crash cap for in_progress tasks beyond page 1)."""
        ws = _make_ws(board_items=[])
        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1",
        )
        await wd._check_board()
        board_calls = [
            c for c in ws.request.call_args_list
            if c.args and c.args[0] == "get_board"
        ]
        assert board_calls, "watchdog did not fetch the board"
        payload = board_calls[0].args[1]
        assert payload.get("status") == "ready,in_progress"
        assert payload.get("limit", 0) >= 500
