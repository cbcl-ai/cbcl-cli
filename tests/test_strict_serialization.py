"""T4.2.1 (07/P2) — strict agent serialization.

An agent must not pick up a NEW ``ready`` task while it still holds another
task in ``in_progress``/``review`` — BUT review/triage dispatch stays always
dispatchable (the reviewer-cycle / MA-triage deadlock), the agent's OWN task
is always claimable (rework / respawn-in-place), and ``blocked`` releases the
executor (DECISION-2). A >15-min full-block escalates once, loudly.

Covers the six obligations from 07 §5:
(a) A↔B reviewer cycle dispatches (review-mode exempt);
(b) MA reviews another task while its own task is in review;
(c) rework return dispatches to a "busy" agent (own-task exemption);
(d) agent with an in_progress task does NOT pop a second ready task;
(e) agent with only a blocked task DOES pop (decision branch);
(f) synthetic full-block >15min → CRITICAL + escalate, exactly once.
"""
from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.task_dispatcher import (
    STRICT_DEADLOCK_SECONDS,
    TaskDispatcher,
)


@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def office_id() -> str:
    return "office-strict-serial"


@pytest.fixture
def mock_supervisor() -> MagicMock:
    s = MagicMock()
    s.can_spawn.return_value = True
    s.is_agent_busy.return_value = False
    s.spawn_worker = AsyncMock(return_value=True)
    s.get_all_statuses.return_value = {"analyst": {"pid": 99, "status": "working"}}
    return s


@pytest.fixture
def mock_config() -> MagicMock:
    c = MagicMock()
    c.get_agent.return_value = {
        "name": "analyst", "model": "m", "system_prompt": "x",
        "allowed_tools": ["Read"],
    }
    c.agents = [{"name": "analyst", "is_active": True}]
    c.get_workstream.return_value = None
    return c


@pytest_asyncio.fixture
async def queue_manager(fake_redis, office_id) -> AgentQueueManager:
    return AgentQueueManager(fake_redis, office_id)


@pytest.fixture
def dispatcher(
    fake_redis, office_id, mock_supervisor, mock_config, queue_manager,
) -> TaskDispatcher:
    d = TaskDispatcher(
        redis=fake_redis,
        office_id=office_id,
        supervisor=mock_supervisor,
        config_store=mock_config,
        queue_manager=queue_manager,
    )
    # No real backend — neutralise dependency / scope lookups.
    d._check_dependencies = AsyncMock(return_value=True)  # type: ignore
    d._fetch_scope_state = AsyncMock(return_value="executing")  # type: ignore
    d._move_and_assign = AsyncMock(return_value=True)  # type: ignore
    return d


# ── Predicate (cases c, d, e + review-counts-as-busy) ────────────────


def test_predicate_blocks_when_other_in_progress(dispatcher):
    # (d) analyst already executing T2 → blocked on a new ready T1.
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "in_progress"},
    ]
    assert dispatcher._agent_has_other_active_task("analyst", "t1") is True


def test_predicate_review_counts_as_busy(dispatcher):
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "review"},
    ]
    assert dispatcher._agent_has_other_active_task("analyst", "t1") is True


def test_predicate_own_task_exempt(dispatcher):
    # (c) the agent's OWN task (matched by any id key) never blocks it.
    dispatcher._last_board_snapshot = [
        {"task_id": "t1", "readable_id": "WR-001.T01",
         "assigned_agent": "analyst", "status": "review"},
    ]
    assert dispatcher._agent_has_other_active_task("analyst", "t1") is False
    assert dispatcher._agent_has_other_active_task("analyst", "WR-001.T01") is False


def test_predicate_blocked_does_not_count(dispatcher):
    # (e) a blocked task releases the executor — does not block a new pop.
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "blocked"},
    ]
    assert dispatcher._agent_has_other_active_task("analyst", "t1") is False


def test_predicate_other_agents_irrelevant(dispatcher):
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "writer", "status": "in_progress"},
    ]
    assert dispatcher._agent_has_other_active_task("analyst", "t1") is False


def test_predicate_empty_snapshot_fails_open(dispatcher):
    dispatcher._last_board_snapshot = []
    assert dispatcher._agent_has_other_active_task("analyst", "t1") is False


def test_blocked_by_in_progress_only_counts_in_progress(dispatcher):
    # Deadlock-suspect predicate: in_progress holder is suspect …
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "in_progress"},
    ]
    assert dispatcher._agent_blocked_by_in_progress("analyst", "t1") is True


def test_blocked_by_in_progress_ignores_review_holder(dispatcher):
    # … but a review holder is a BOUNDED wait, NOT a deadlock — the gate
    # still blocks the pop, but the deadlock timer must not arm (else it
    # false-fires on every >15min review while the executor waits).
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "review"},
    ]
    assert dispatcher._agent_has_other_active_task("analyst", "t1") is True
    assert dispatcher._agent_blocked_by_in_progress("analyst", "t1") is False


def test_blocked_by_in_progress_own_task_exempt(dispatcher):
    dispatcher._last_board_snapshot = [
        {"task_id": "t1", "readable_id": "WR-001.T01",
         "assigned_agent": "analyst", "status": "in_progress"},
    ]
    assert dispatcher._agent_blocked_by_in_progress("analyst", "t1") is False


# ── dispatch_agent integration (d execute-block, a/b review-exempt) ──


async def _enqueue(qm, agent, task_id, status):
    await qm.add_task(agent, {
        "task_id": task_id, "readable_id": task_id.upper(),
        "status": status, "priority": "medium",
    })


async def test_execute_pop_blocked_and_requeued(dispatcher, queue_manager):
    # (d) end-to-end: analyst has T2 in_progress; a ready T1 must NOT dispatch.
    dispatcher._fetch_task_status = AsyncMock(return_value="ready")  # type: ignore
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "in_progress"},
    ]
    await _enqueue(queue_manager, "analyst", "t1", "ready")

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is False
    dispatcher._supervisor.spawn_worker.assert_not_called()
    # The entry was re-queued, not dropped.
    assert await queue_manager.get_queue_size("analyst") == 1
    # And the agent is now marked strict-blocked.
    assert "analyst" in dispatcher._strict_block_since


async def test_review_block_does_not_arm_deadlock_timer(dispatcher, queue_manager):
    # An executor stays assigned through review (Rule #15). While its prior
    # task is in review it is idle and its next ready task is correctly held
    # — but this is a BOUNDED wait, not a deadlock. The timer must NOT arm,
    # or the detector would false-fire CRITICAL + a user escalation on every
    # long review.
    dispatcher._fetch_task_status = AsyncMock(return_value="ready")  # type: ignore
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "review"},
    ]
    await _enqueue(queue_manager, "analyst", "t1", "ready")

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is False
    dispatcher._supervisor.spawn_worker.assert_not_called()
    assert await queue_manager.get_queue_size("analyst") == 1
    # Pop blocked (P2) but NOT armed for deadlock escalation.
    assert "analyst" not in dispatcher._strict_block_since


async def test_busy_agent_clears_stale_strict_timer(dispatcher):
    # A strict-blocked agent that then becomes genuinely busy (running its
    # own task) is making progress — the stale timer must be dropped so the
    # detector can't false-fire after the agent goes WORKING.
    dispatcher._strict_block_since["analyst"] = time.monotonic() - (
        STRICT_DEADLOCK_SECONDS + 10
    )
    dispatcher._supervisor.is_agent_busy.return_value = True

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is False
    assert "analyst" not in dispatcher._strict_block_since
    # And so the detector reports no wedge.
    assert dispatcher._detect_strict_deadlock() == []


async def test_review_pop_exempt_dispatches(dispatcher, queue_manager):
    # (a)/(b): a review pop is NEVER gated even though analyst holds T2
    # in_progress — reviews must always dispatch.
    dispatcher._fetch_task_status = AsyncMock(return_value="review")  # type: ignore
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "in_progress"},
    ]
    await _enqueue(queue_manager, "analyst", "t1", "review")

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is True
    dispatcher._supervisor.spawn_worker.assert_called_once()


async def test_own_task_rework_dispatches(dispatcher, queue_manager):
    # (c) end-to-end: the only other active row IS this task → exempt.
    dispatcher._fetch_task_status = AsyncMock(return_value="ready")  # type: ignore
    dispatcher._last_board_snapshot = [
        {"task_id": "t1", "assigned_agent": "analyst", "status": "review"},
    ]
    await _enqueue(queue_manager, "analyst", "t1", "ready")

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is True
    dispatcher._supervisor.spawn_worker.assert_called_once()
    assert "analyst" not in dispatcher._strict_block_since


# ── T8/1.1: dispatcher honors the watchdog respawn cap ───────────────


async def test_respawn_capped_orphan_is_dropped_not_respawned(
    dispatcher, queue_manager,
):
    """An in_progress orphan whose crash-respawn cap is reached (per the
    watchdog) must NOT be re-spawned by the dispatcher's reconcile path —
    it's dropped so the watchdog owns the escalation to blocked. Without
    this guard the dispatcher leaks CLI spawns past the cap."""
    dispatcher._fetch_task_status = AsyncMock(return_value="in_progress")  # type: ignore
    wd = MagicMock()
    wd.respawn_capped.return_value = True
    wd.is_crash_recovering.return_value = True
    dispatcher.set_watchdog(wd)
    await _enqueue(queue_manager, "analyst", "t1", "in_progress")

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is False
    dispatcher._supervisor.spawn_worker.assert_not_called()
    # Dropped (not re-queued) — watchdog will land the blocked move.
    assert await queue_manager.get_queue_size("analyst") == 0
    wd.respawn_capped.assert_called_with("t1")


async def test_uncapped_orphan_still_respawns(dispatcher, queue_manager):
    """Below the cap, an in_progress orphan is still re-spawned in place."""
    dispatcher._fetch_task_status = AsyncMock(return_value="in_progress")  # type: ignore
    dispatcher._last_board_snapshot = []
    wd = MagicMock()
    wd.respawn_capped.return_value = False
    wd.is_crash_recovering.return_value = True
    dispatcher.set_watchdog(wd)
    await _enqueue(queue_manager, "analyst", "t1", "in_progress")

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is True
    dispatcher._supervisor.spawn_worker.assert_called_once()


# ── T8/2.1: deadlock timer does not arm against a recovering holder ───


async def test_recovering_in_progress_holder_does_not_arm_deadlock(
    dispatcher, queue_manager,
):
    """When the in_progress holder is under active crash recovery (the
    watchdog is respawning/escalating it), the ready-task block must NOT
    arm the deadlock detector — it's a transient recovery, not a wedge."""
    dispatcher._fetch_task_status = AsyncMock(return_value="ready")  # type: ignore
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "in_progress"},
    ]
    wd = MagicMock()
    wd.respawn_capped.return_value = False
    wd.is_crash_recovering.return_value = True  # t2 is crash-recovering
    dispatcher.set_watchdog(wd)
    await _enqueue(queue_manager, "analyst", "t1", "ready")

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is False  # pop still blocked (P2 serialization)
    # …but NOT armed, because the holder is recovering, not deadlocked.
    assert "analyst" not in dispatcher._strict_block_since
    wd.is_crash_recovering.assert_called_with("t2")


async def test_non_recovering_in_progress_holder_still_arms(
    dispatcher, queue_manager,
):
    """A genuine phantom in_progress holder (NOT under crash recovery)
    still arms the deadlock timer — the T8/2.1 guard must not disable the
    real detection path."""
    dispatcher._fetch_task_status = AsyncMock(return_value="ready")  # type: ignore
    dispatcher._last_board_snapshot = [
        {"task_id": "t2", "assigned_agent": "analyst", "status": "in_progress"},
    ]
    wd = MagicMock()
    wd.respawn_capped.return_value = False
    wd.is_crash_recovering.return_value = False  # genuinely wedged
    dispatcher.set_watchdog(wd)
    await _enqueue(queue_manager, "analyst", "t1", "ready")

    dispatched = await dispatcher.dispatch_agent("analyst")

    assert dispatched is False
    assert "analyst" in dispatcher._strict_block_since


# ── Deadlock detector (case f) ───────────────────────────────────────


def test_deadlock_detector_fires_once_and_loudly(dispatcher, caplog):
    # (f) agent strict-blocked past the threshold → CRITICAL + escalate once.
    dispatcher._strict_block_since = {
        "analyst": time.monotonic() - STRICT_DEADLOCK_SECONDS - 1,
    }
    with caplog.at_level(logging.CRITICAL):
        wedged = dispatcher._detect_strict_deadlock()
    assert wedged == ["analyst"]
    assert any("DEADLOCK" in r.message for r in caplog.records)
    # Second call is a no-op (one-shot per episode).
    assert dispatcher._detect_strict_deadlock() == []


def test_deadlock_detector_quiet_below_threshold(dispatcher):
    dispatcher._strict_block_since = {"analyst": time.monotonic()}
    assert dispatcher._detect_strict_deadlock() == []


def test_deadlock_one_shot_resets_on_own_dispatch(dispatcher):
    dispatcher._strict_block_since = {
        "analyst": time.monotonic() - STRICT_DEADLOCK_SECONDS - 1,
    }
    assert dispatcher._detect_strict_deadlock() == ["analyst"]
    assert "analyst" in dispatcher._strict_deadlock_escalated_agents
    # The wedged agent's OWN dispatch clears its timer + re-arms ITS one-shot.
    dispatcher._clear_strict_block("analyst")
    assert "analyst" not in dispatcher._strict_deadlock_escalated_agents
    assert "analyst" not in dispatcher._strict_block_since


def test_deadlock_one_shot_is_per_agent(dispatcher):
    # Bug fix: another agent's routine dispatch must NOT disarm a genuinely
    # wedged agent's escalation (the old global bool did exactly that).
    now = time.monotonic()
    dispatcher._strict_block_since = {
        "analyst": now - STRICT_DEADLOCK_SECONDS - 1,
        "writer": now - STRICT_DEADLOCK_SECONDS - 1,
    }
    assert dispatcher._detect_strict_deadlock() == ["analyst", "writer"]
    # writer dispatches (routine) — analyst stays escalated, not re-fired.
    dispatcher._clear_strict_block("writer")
    assert dispatcher._detect_strict_deadlock() == []  # analyst not re-escalated
    assert "analyst" in dispatcher._strict_deadlock_escalated_agents


async def test_run_loop_escalates_wedged_agents(dispatcher):
    # The detector's return value must be forwarded to the escalation POST.
    dispatcher._escalate_strict_deadlock = AsyncMock()  # type: ignore
    dispatcher._strict_block_since = {
        "analyst": time.monotonic() - STRICT_DEADLOCK_SECONDS - 1,
    }
    wedged = dispatcher._detect_strict_deadlock()
    if wedged:
        await dispatcher._escalate_strict_deadlock(wedged)
    dispatcher._escalate_strict_deadlock.assert_awaited_once_with(["analyst"])
