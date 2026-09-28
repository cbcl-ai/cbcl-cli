"""Tests for the dispatcher's and watchdog's state-log throttles.

Pins the contract introduced to fix the user-reported log spam where
``Task X has unmet dependencies, re-queuing`` fired every 2s for the
lifetime of any task waiting on a dependency.

Contract:
- The FIRST occurrence of a state-log key emits at INFO.
- Subsequent calls with the SAME key within
  ``STATE_LOG_INTERVAL_SECONDS`` emit at DEBUG.
- After the interval elapses, the next call re-emits at INFO.
- A DIFFERENT key bypasses the throttle (state change = new line).

``time.monotonic()`` counts from host boot, so these tests pin the clock
instead of relying on the machine's uptime. The boot-window cases are the
regression for a daemon started less than one interval after boot: the
old ``0.0`` "never logged" sentinel demoted every first occurrence to
DEBUG there (the lane failed whenever the test VM had just booted).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import watchdog as watchdog_mod
from src.orchestrator import task_dispatcher as td_mod
from src.orchestrator.task_dispatcher import TaskDispatcher

# Monotonic readings the throttle must handle identically: shortly after
# host boot (below one throttle interval) and long after boot.
_CLOCK_STARTS = pytest.mark.parametrize(
    "clock_start",
    [5.0, td_mod.STATE_LOG_INTERVAL_SECONDS - 1.0, 100_000.0],
    ids=["seconds-after-boot", "just-under-one-interval", "long-uptime"],
)


class _FakeClock:
    """Deterministic stand-in for the ``time`` module's monotonic clock."""

    def __init__(self, start: float) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def dispatcher() -> TaskDispatcher:
    """Construct a TaskDispatcher with only the attributes
    ``_log_state`` actually touches. The other deps (Redis,
    supervisor, queue manager, config store) are unused for this
    test surface."""
    return TaskDispatcher(
        redis=MagicMock(),
        office_id="office-1",
        supervisor=MagicMock(),
        config_store=MagicMock(),
        queue_manager=MagicMock(),
    )


def _pin_dispatcher_clock(monkeypatch, start: float) -> _FakeClock:
    clock = _FakeClock(start)
    # Replace only the dispatcher module's ``time`` reference so the
    # interpreter-wide clock (and any event loop) is untouched.
    monkeypatch.setattr(td_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


def _levels(caplog, logger_name: str) -> list[int]:
    return [r.levelno for r in caplog.records if r.name == logger_name]


@_CLOCK_STARTS
def test_first_call_logs_at_info(dispatcher, caplog, monkeypatch, clock_start):
    _pin_dispatcher_clock(monkeypatch, clock_start)
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    dispatcher._log_state("deps:T1", "Task %s has unmet deps", "T1")
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert info_records[0].getMessage() == "Task T1 has unmet deps"


@_CLOCK_STARTS
def test_repeated_call_within_window_drops_to_debug(
    dispatcher, caplog, monkeypatch, clock_start,
):
    clock = _pin_dispatcher_clock(monkeypatch, clock_start)
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    # Two calls with the same key, one second apart.
    dispatcher._log_state("deps:T1", "Task %s has unmet deps", "T1")
    clock.now += 1.0
    dispatcher._log_state("deps:T1", "Task %s has unmet deps", "T1")

    assert _levels(caplog, "cbcl.dispatcher") == [logging.INFO, logging.DEBUG], (
        "Only the first call should hit INFO; the repeat drops to DEBUG"
    )


@_CLOCK_STARTS
def test_different_keys_log_independently(
    dispatcher, caplog, monkeypatch, clock_start,
):
    """A state change for the same task (e.g. ``deps`` →
    ``ma-cooldown``) is a fresh log key and bypasses the throttle."""
    _pin_dispatcher_clock(monkeypatch, clock_start)
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    dispatcher._log_state("deps:T1", "msg A %s", "T1")
    dispatcher._log_state("ma-cooldown:T1", "msg B %s", "T1")
    assert _levels(caplog, "cbcl.dispatcher") == [logging.INFO, logging.INFO]


@_CLOCK_STARTS
def test_window_expiry_re_arms_info(dispatcher, caplog, monkeypatch, clock_start):
    """After STATE_LOG_INTERVAL_SECONDS elapses, the same key logs
    at INFO again so an operator can still see "task is still
    stuck after 5 min"."""
    clock = _pin_dispatcher_clock(monkeypatch, clock_start)
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")

    dispatcher._log_state("deps:T1", "stuck %s", "T1")  # fresh   → INFO
    clock.now = clock_start + 100.0
    dispatcher._log_state("deps:T1", "stuck %s", "T1")  # +100s   → DEBUG
    clock.now = clock_start + td_mod.STATE_LOG_INTERVAL_SECONDS + 1.0
    dispatcher._log_state("deps:T1", "stuck %s", "T1")  # past 5m → INFO

    assert _levels(caplog, "cbcl.dispatcher") == [
        logging.INFO, logging.DEBUG, logging.INFO,
    ], "First and post-window calls log at INFO; the mid-window call at DEBUG"


# ---------------------------------------------------------------------------
# Watchdog "waking dispatcher" throttle — same contract, same boot hazard.
# ---------------------------------------------------------------------------

_READY_TASK = {
    "id": "task-1",
    "readable_id": "WS-001.T01",
    "status": "ready",
    "assigned_agent": "analyst",
}


def _watchdog() -> watchdog_mod.TaskWatchdog:
    ws = AsyncMock()
    ws.request = AsyncMock(return_value={"items": [dict(_READY_TASK)]})
    supervisor = MagicMock()
    supervisor.execution_policy = None  # legacy (non-dynamic) admission
    supervisor.is_agent_busy.return_value = False
    dispatcher = MagicMock()
    dispatcher.wake = MagicMock()
    return watchdog_mod.TaskWatchdog(
        ws=ws, executor=None, manager=MagicMock(), config_store=MagicMock(),
        task_queue=None, office_id="office-1",
        supervisor=supervisor, dispatcher=dispatcher,
    )


@_CLOCK_STARTS
async def test_watchdog_ready_wake_log_throttle(caplog, monkeypatch, clock_start):
    clock = _FakeClock(clock_start)
    monkeypatch.setattr(
        watchdog_mod, "time", SimpleNamespace(monotonic=clock.monotonic),
    )
    caplog.set_level(logging.DEBUG, logger="cbcl.watchdog")
    wd = _watchdog()

    def wake_levels() -> list[int]:
        return [
            r.levelno for r in caplog.records
            if r.name == "cbcl.watchdog" and "waking dispatcher" in r.getMessage()
        ]

    await wd._check_board()  # first sighting of the stuck set → INFO
    clock.now += 30.0
    await wd._check_board()  # same set within the window → DEBUG
    clock.now = clock_start + watchdog_mod.WATCHDOG_STATE_LOG_INTERVAL + 1.0
    await wd._check_board()  # window elapsed → INFO again

    assert wake_levels() == [logging.INFO, logging.DEBUG, logging.INFO]
    # Only the log line is throttled; the dispatcher is woken every tick.
    assert wd._dispatcher.wake.call_count == 3
