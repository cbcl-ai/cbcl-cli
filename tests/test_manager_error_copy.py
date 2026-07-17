"""FIX M1(a)/M2 (blink-resilience) — classified Manager error copy +
usage-limit reset UX.

Contract under test (``manager_controller``):

* ``_classified_error_copy`` returns actionable "your message was not
  lost" copy for the account/provider classes, names the parsed reset
  time for a usage limit, and returns ``None`` for unfamiliar classes
  (the raw error stays verbatim-debuggable);
* ``_schedule_usage_limit_wake`` schedules exactly one 'ready' wake per
  context for a near-future reset, skips missing/past/far resets, and
  replaces a prior wake on re-schedule.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.orchestrator.error_classifier import classify_error
from src.orchestrator.manager_controller import (
    ManagerController,
    _classified_error_copy,
)


# ---------------------------------------------------------------------------
# _classified_error_copy
# ---------------------------------------------------------------------------


def test_overload_copy_is_actionable():
    remedy = classify_error("API Error: 529 Overloaded")
    copy = _classified_error_copy(remedy, "raw")
    assert copy is not None
    assert "overloaded" in copy.lower()
    assert "not lost" in copy


def test_rate_limit_copy_is_actionable():
    remedy = classify_error("API Error 429 rate limit exceeded")
    copy = _classified_error_copy(remedy, "raw")
    assert "429" in copy
    assert "not lost" in copy


def test_connection_lost_copy_is_actionable():
    remedy = classify_error("connection reset by peer")
    assert "resend" in _classified_error_copy(remedy, "raw")


def test_auth_failed_copy_names_the_fix():
    remedy = classify_error("401 unauthorized")
    assert "auth" in _classified_error_copy(remedy, "raw").lower()


def test_usage_limit_copy_names_reset_time():
    remedy = classify_error(
        "Claude usage limit reached. Your limit resets in 2 hours",
    )
    copy = _classified_error_copy(remedy, "raw")
    assert "usage" in copy.lower()
    assert "UTC" in copy  # the parsed reset time is named


def test_usage_limit_copy_without_parseable_reset():
    remedy = classify_error("Claude usage limit reached.")
    copy = _classified_error_copy(remedy, "raw")
    assert "the next reset" in copy


def test_unknown_class_returns_none():
    remedy = classify_error("some totally novel explosion")
    assert _classified_error_copy(remedy, "raw") is None


# ---------------------------------------------------------------------------
# _schedule_usage_limit_wake
# ---------------------------------------------------------------------------


def _bare_controller() -> ManagerController:
    ctrl = ManagerController.__new__(ManagerController)
    ctrl._usage_limit_wake_tasks = {}
    ctrl._publish_manager_state = AsyncMock()
    ctrl._router = MagicMock()
    return ctrl


@pytest.mark.asyncio
async def test_wake_publishes_ready_at_reset(monkeypatch):
    ctrl = _bare_controller()
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    reset_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    ctrl._schedule_usage_limit_wake("workstream:ws-1", reset_at)
    task = ctrl._usage_limit_wake_tasks["workstream:ws-1"]
    await asyncio.wait_for(task, timeout=1.0)

    assert slept and 500 < slept[0] < 700  # ~10 min + 30s grace
    ctrl._publish_manager_state.assert_awaited_once()
    args = ctrl._publish_manager_state.call_args.args
    assert args[0] == "workstream:ws-1"
    assert args[1] == "ready"
    assert "reopened" in args[2]
    # Entry self-removed on completion.
    assert "workstream:ws-1" not in ctrl._usage_limit_wake_tasks


@pytest.mark.asyncio
async def test_no_wake_without_reset_time():
    ctrl = _bare_controller()
    ctrl._schedule_usage_limit_wake("general_chat", None)
    assert ctrl._usage_limit_wake_tasks == {}


@pytest.mark.asyncio
async def test_no_wake_for_far_future_reset():
    """A weekly cap days away is not worth holding a task for — the
    error bubble already names the time."""
    ctrl = _bare_controller()
    ctrl._schedule_usage_limit_wake(
        "general_chat",
        datetime.now(timezone.utc) + timedelta(days=3),
    )
    assert ctrl._usage_limit_wake_tasks == {}


@pytest.mark.asyncio
async def test_reschedule_replaces_prior_wake():
    ctrl = _bare_controller()
    reset_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    ctrl._schedule_usage_limit_wake("general_chat", reset_at)
    first = ctrl._usage_limit_wake_tasks["general_chat"]
    ctrl._schedule_usage_limit_wake(
        "general_chat", reset_at + timedelta(minutes=5),
    )
    second = ctrl._usage_limit_wake_tasks["general_chat"]
    assert second is not first
    await asyncio.sleep(0)
    assert first.cancelled() or first.done()
    # The cancelled predecessor's cleanup must not evict the replacement.
    assert ctrl._usage_limit_wake_tasks.get("general_chat") is second
    second.cancel()
    await asyncio.sleep(0)
