"""Tests for TaskWatchdog — crash recovery for stuck in-progress tasks.

The watchdog now only handles:
- In-progress tasks with no active agent session (crash recovery)
Review and blocked tasks are handled by the Manager Assistant via per-agent queues.
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
    async def test_moves_to_ready_when_agent_idle(self):
        sup = _make_supervisor()
        ws = _make_ws()

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )

        task = {"id": "t1", "readable_id": "WR-001.T01", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        # Should have called move_task to ready
        move_calls = [
            call for call in ws.request.call_args_list
            if call[0][0] == "move_task"
        ]
        assert len(move_calls) == 1
        assert move_calls[0][0][1]["new_status"] == "ready"
        assert wd._task_crash_count["t1"] == 1

    @pytest.mark.asyncio
    async def test_moves_to_blocked_after_3_crashes(self):
        sup = _make_supervisor()
        ws = _make_ws()

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
        assert move_calls[0][0][1]["new_status"] == "blocked"

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

        wd = TaskWatchdog(
            ws=ws, executor=None, manager=_make_manager(),
            config_store=_make_config(), task_queue=None,
            office_id="off1", supervisor=sup,
        )
        # Expired TTL
        wd._recently_dispatched["t1"] = time.monotonic() - RECENTLY_DISPATCHED_TTL - 1

        task = {"id": "t1", "readable_id": "WR-001.T01", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        # Should move after TTL expired
        move_calls = [
            call for call in ws.request.call_args_list
            if call[0][0] == "move_task"
        ]
        assert len(move_calls) == 1

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
    async def test_move_failure_increments_counter(self):
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

        task = {"id": "t1", "readable_id": "X", "assigned_agent": "analyst"}
        await wd._handle_in_progress(task)

        assert wd._move_failed.get("t1", 0) == 1


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
