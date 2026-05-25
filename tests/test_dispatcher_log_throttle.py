"""Tests for the dispatcher's state-log throttle.

Pins the contract introduced to fix the user-reported log spam where
``Task X has unmet dependencies, re-queuing`` fired every 2s for the
lifetime of any task waiting on a dependency.

Contract:
- The FIRST occurrence of a state-log key emits at INFO.
- Subsequent calls with the SAME key within
  ``STATE_LOG_INTERVAL_SECONDS`` emit at DEBUG.
- After the interval elapses, the next call re-emits at INFO.
- A DIFFERENT key bypasses the throttle (state change = new line).
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from src.orchestrator import task_dispatcher as td_mod
from src.orchestrator.task_dispatcher import TaskDispatcher


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


def test_first_call_logs_at_info(dispatcher, caplog):
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    dispatcher._log_state("deps:T1", "Task %s has unmet deps", "T1")
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert info_records[0].getMessage() == "Task T1 has unmet deps"


def test_repeated_call_within_window_drops_to_debug(
    dispatcher, caplog,
):
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    # Two calls back-to-back with the same key.
    dispatcher._log_state("deps:T1", "Task %s has unmet deps", "T1")
    dispatcher._log_state("deps:T1", "Task %s has unmet deps", "T1")

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(info_records) == 1, "Only the first call should hit INFO"
    assert len(debug_records) == 1, "Subsequent calls should drop to DEBUG"


def test_different_keys_log_independently(dispatcher, caplog):
    """A state change for the same task (e.g. ``deps`` →
    ``ma-cooldown``) is a fresh log key and bypasses the throttle."""
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    dispatcher._log_state("deps:T1", "msg A %s", "T1")
    dispatcher._log_state("ma-cooldown:T1", "msg B %s", "T1")
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 2


def test_window_expiry_re_arms_info(dispatcher, caplog, monkeypatch):
    """After STATE_LOG_INTERVAL_SECONDS elapses, the same key logs
    at INFO again so an operator can still see "task is still
    stuck after 5 min"."""
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")

    # Pin the monotonic clock so we can simulate elapsed time
    # without sleeping. Start at t=1000 (any value comfortably above
    # the throttle interval) so the first call's elapsed-since-0.0
    # comparison clears the throttle and hits INFO.
    base = 1000.0
    times = iter([
        base,
        base + 100.0,
        base + td_mod.STATE_LOG_INTERVAL_SECONDS + 1.0,
    ])
    monkeypatch.setattr(td_mod.time, "monotonic", lambda: next(times))

    dispatcher._log_state("deps:T1", "stuck %s", "T1")  # fresh   → INFO
    dispatcher._log_state("deps:T1", "stuck %s", "T1")  # +100s   → DEBUG
    dispatcher._log_state("deps:T1", "stuck %s", "T1")  # past 5m → INFO

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(info_records) == 2, (
        "First call AND post-window call should both log at INFO"
    )
    assert len(debug_records) == 1, (
        "Mid-window call should drop to DEBUG"
    )
