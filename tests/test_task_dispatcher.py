"""Tests for TaskDispatcher — per-agent queue dispatch.

Uses fakeredis for Redis operations and mock objects for the AgentSupervisor
and ConfigStore dependencies.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import fakeredis.aioredis

from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.task_dispatcher import TaskDispatcher


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    """Provide a fakeredis async client."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def office_id() -> str:
    return "test-office-001"


@pytest.fixture
def mock_supervisor() -> MagicMock:
    """Create a mock AgentSupervisor."""
    supervisor = MagicMock()
    supervisor.can_spawn.return_value = True
    supervisor.is_agent_busy.return_value = False
    supervisor.spawn_worker = AsyncMock(return_value=True)
    # The move-failure rollback path awaits _kill_process on the
    # spawned worker (T1.1.2 / G2 fix) — must be awaitable.
    supervisor._kill_process = AsyncMock()
    supervisor.get_agent_current_task.return_value = None
    supervisor.get_all_statuses.return_value = {
        "analyst": {"pid": 1234, "status": "working"},
    }
    return supervisor


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock ConfigStore."""
    config = MagicMock()
    config.get_agent.return_value = {
        "name": "analyst",
        "display_name": "Analyst",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are an analyst.",
        "allowed_tools": ["Read", "Write"],
    }
    config.agents = [
        {"name": "analyst", "is_active": True},
        {"name": "auditor", "is_active": True},
        {"name": "manager-assistant", "is_active": True},
    ]
    return config


@pytest_asyncio.fixture
async def queue_manager(fake_redis, office_id) -> AgentQueueManager:
    """Create an AgentQueueManager."""
    return AgentQueueManager(fake_redis, office_id)


@pytest.fixture
def dispatcher(
    fake_redis, office_id, mock_supervisor, mock_config, queue_manager,
) -> TaskDispatcher:
    """Create a TaskDispatcher with mock dependencies.

    Stubs ``_fetch_task_status`` to mirror the popped task's
    in-queue status (no real backend in tests) and
    ``_is_blocked_triage_in_cooldown`` to always return False so
    the freshness + cooldown checks are no-ops by default.
    Individual tests can override either when they care about the
    contracts.
    """
    d = TaskDispatcher(
        redis=fake_redis,
        office_id=office_id,
        supervisor=mock_supervisor,
        config_store=mock_config,
        queue_manager=queue_manager,
    )

    # The dispatcher refreshes the popped task's status from the
    # backend before spawning (defence against stale queue entries —
    # TO-007.T40 regression on 2026-05-14). In tests there's no
    # backend, so we honour the value the test stored on the
    # dispatcher (``_test_status_override``) or default to
    # ``"ready"`` for the common case.
    async def _fake_fetch_status(task_id: str) -> str | None:
        return getattr(d, "_test_status_override", None) or "ready"
    d._fetch_task_status = _fake_fetch_status  # type: ignore[method-assign]

    async def _no_cooldown(task_id: str) -> bool:
        return False
    d._is_blocked_triage_in_cooldown = _no_cooldown  # type: ignore[method-assign]

    # ``_move_and_assign`` does a synchronous HTTP POST against the
    # backend's ``/tool-call`` endpoint. There's no backend in
    # tests, so the v0.2.26 hardening (check status, rollback on
    # failure) would mark every dispatch as failed and clear the
    # active marker. Default to a success-returning stub so most
    # tests get the dispatched-happy-path; tests that exercise the
    # rollback override this stub explicitly.
    d._move_and_assign = AsyncMock(return_value=True)  # type: ignore[method-assign]

    return d


def make_task(
    task_id: str | None = None,
    agent: str = "analyst",
    priority: str = "medium",
    readable_id: str | None = None,
    title: str = "Test task",
    status: str = "ready",
) -> dict:
    """Build a minimal task_data dict."""
    import uuid as _uuid
    tid = task_id or str(_uuid.uuid4())
    return {
        "task_id": tid,
        "readable_id": readable_id or f"T-{tid[:8]}",
        "title": title,
        "assigned_agent": agent,
        "priority": priority,
        "status": status,
        "workstream_name": "Test Workstream",
        "brief": {"goal": "Test goal"},
        "labels": [],
        "rework_feedback": None,
    }


# ---------------------------------------------------------------------------
# add_task tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_task_enqueues_to_agent_queue(dispatcher, queue_manager):
    """add_task adds a task to the agent's per-agent queue."""
    task = make_task(priority="high", agent="analyst")
    await dispatcher.add_task(task)

    size = await queue_manager.get_queue_size("analyst")
    assert size == 1


@pytest.mark.asyncio
async def test_add_task_unassigned_review_goes_to_ma(dispatcher, queue_manager):
    """Unassigned review tasks route to manager-assistant."""
    task = make_task(agent="", status="review")
    await dispatcher.add_task(task)

    size = await queue_manager.get_queue_size("manager-assistant")
    assert size == 1


@pytest.mark.asyncio
async def test_add_task_skips_no_task_id(dispatcher, queue_manager):
    """Tasks without task_id are rejected."""
    task = {"assigned_agent": "analyst", "priority": "medium"}
    await dispatcher.add_task(task)

    size = await queue_manager.get_queue_size("analyst")
    assert size == 0


@pytest.mark.asyncio
async def test_add_task_skips_manager(dispatcher, queue_manager):
    """Tasks assigned to 'manager' are skipped."""
    task = make_task(agent="manager")
    await dispatcher.add_task(task)

    # No queue should have the task
    total = await dispatcher.get_queue_size()
    assert total == 0


@pytest.mark.asyncio
async def test_add_task_wakes_dispatcher(dispatcher):
    """add_task() sets the wake event after enqueuing."""
    dispatcher._wake_event.clear()
    await dispatcher.add_task(make_task())
    assert dispatcher._wake_event.is_set()


# ---------------------------------------------------------------------------
# remove_task tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_task(dispatcher, queue_manager):
    """Remove a specific task from all queues."""
    t1 = make_task(task_id="task-1", agent="analyst")
    t2 = make_task(task_id="task-2", agent="analyst")
    await dispatcher.add_task(t1)
    await dispatcher.add_task(t2)

    await dispatcher.remove_task("task-1")
    size = await queue_manager.get_queue_size("analyst")
    assert size == 1


# ---------------------------------------------------------------------------
# dispatch_agent tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_agent_spawns_worker(dispatcher, mock_supervisor, queue_manager):
    """dispatch_agent spawns a worker for a queued task."""
    task = make_task(agent="analyst")
    await queue_manager.add_task("analyst", task)

    result = await dispatcher.dispatch_agent("analyst")
    assert result is True
    mock_supervisor.spawn_worker.assert_called_once()

    call_args = mock_supervisor.spawn_worker.call_args
    assert call_args[0][0] == "analyst"


@pytest.mark.asyncio
async def test_dispatch_agent_removes_from_queue(dispatcher, queue_manager):
    """After dispatch, the task is removed from the queue."""
    task = make_task(agent="analyst")
    await queue_manager.add_task("analyst", task)

    await dispatcher.dispatch_agent("analyst")
    size = await queue_manager.get_queue_size("analyst")
    assert size == 0


@pytest.mark.asyncio
async def test_dispatch_agent_skips_busy(dispatcher, mock_supervisor, queue_manager):
    """Busy agents are not dispatched to."""
    mock_supervisor.is_agent_busy.return_value = True

    await queue_manager.add_task("analyst", make_task(agent="analyst"))
    result = await dispatcher.dispatch_agent("analyst")

    assert result is False
    mock_supervisor.spawn_worker.assert_not_called()
    # Task stays in queue
    assert await queue_manager.get_queue_size("analyst") == 1


@pytest.mark.asyncio
async def test_dispatch_agent_skips_unknown(dispatcher, mock_config, queue_manager):
    """Tasks for unknown agents are not dispatched."""
    mock_config.get_agent.return_value = None

    await queue_manager.add_task("unknown", make_task(agent="unknown"))
    result = await dispatcher.dispatch_agent("unknown")

    assert result is False


@pytest.mark.asyncio
async def test_dispatch_agent_respects_spawn_limit(
    dispatcher, mock_supervisor, queue_manager,
):
    """When supervisor.can_spawn() returns False, nothing is dispatched."""
    mock_supervisor.can_spawn.return_value = False

    await queue_manager.add_task("analyst", make_task(agent="analyst"))
    result = await dispatcher.dispatch_agent("analyst")

    assert result is False
    mock_supervisor.spawn_worker.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_agent_requeues_on_spawn_failure(
    dispatcher, mock_supervisor, queue_manager,
):
    """If spawn fails, the task is re-queued."""
    mock_supervisor.spawn_worker = AsyncMock(return_value=False)

    task = make_task(agent="analyst")
    await queue_manager.add_task("analyst", task)

    result = await dispatcher.dispatch_agent("analyst")
    assert result is False

    # Task should be back in the queue
    assert await queue_manager.get_queue_size("analyst") == 1


@pytest.mark.asyncio
async def test_dispatch_agent_sets_active(dispatcher, queue_manager, mock_supervisor):
    """dispatch_agent sets the active task in queue manager."""
    task = make_task(agent="analyst", task_id="active-test")
    await queue_manager.add_task("analyst", task)

    await dispatcher.dispatch_agent("analyst")

    active = await queue_manager.get_active("analyst")
    assert active is not None
    assert active["task_id"] == "active-test"


# ---------------------------------------------------------------------------
# T1.1.2 (G2) — failed ready→in_progress move AFTER a successful spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_move_failure_kills_worker_clears_active_and_requeues(
    dispatcher, mock_supervisor, queue_manager,
):
    """When the ready→in_progress move fails AFTER spawn_worker
    succeeded, the dispatcher must kill the spawned worker (nothing
    else reaps a live, healthy process — the watchdog only catches
    stale/silent sessions), clear the active marker, AND re-queue the
    entry. Pre-fix the worker was left running and executed the full
    task invisibly while the reconciler re-dispatched the same task —
    double execution."""
    task = make_task(agent="analyst", task_id="move-fail-1")
    await queue_manager.add_task("analyst", task)

    dispatcher._move_and_assign = AsyncMock(return_value=False)  # type: ignore[method-assign]

    result = await dispatcher.dispatch_agent("analyst")
    assert result is False

    # The rogue spawned worker was killed.
    mock_supervisor._kill_process.assert_awaited_once_with("analyst")
    # Active marker cleared so the agent is assignable again.
    assert not await queue_manager.is_busy("analyst")
    # The entry was RE-QUEUED, not dropped on the floor.
    assert await queue_manager.get_queue_size("analyst") == 1


@pytest.mark.asyncio
async def test_dispatch_move_failure_no_second_spawn_until_repop(
    dispatcher, mock_supervisor, queue_manager,
):
    """The failed attempt spawns exactly ONCE; the re-queued entry is
    only spawned again when a later dispatch tick re-pops it."""
    task = make_task(agent="analyst", task_id="move-fail-2")
    await queue_manager.add_task("analyst", task)

    dispatcher._move_and_assign = AsyncMock(return_value=False)  # type: ignore[method-assign]
    assert await dispatcher.dispatch_agent("analyst") is False
    assert mock_supervisor.spawn_worker.await_count == 1

    # Next tick: the move succeeds — the SAME re-queued entry is
    # re-popped and dispatched cleanly (second spawn happens only now).
    dispatcher._move_and_assign = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert await dispatcher.dispatch_agent("analyst") is True
    assert mock_supervisor.spawn_worker.await_count == 2

    active = await queue_manager.get_active("analyst")
    assert active is not None
    assert active["task_id"] == "move-fail-2"
    assert await queue_manager.get_queue_size("analyst") == 0


@pytest.mark.asyncio
async def test_dispatch_move_failure_requeue_capped_after_three(
    dispatcher, mock_supervisor, queue_manager, caplog,
):
    """Round-2 LOW: the kill+clear+requeue rollback is bounded per task.
    Failures 1-3 re-queue the entry; the 4th DROPS it (no re-queue) with
    one WARNING — the 60s reconciler + the backend stuck-ready sweeper
    own it from there."""
    task = make_task(agent="analyst", task_id="move-fail-cap")
    await queue_manager.add_task("analyst", task)

    dispatcher._move_and_assign = AsyncMock(return_value=False)  # type: ignore[method-assign]

    for _ in range(3):
        assert await dispatcher.dispatch_agent("analyst") is False
        # Re-queued each time.
        assert await queue_manager.get_queue_size("analyst") == 1

    # 4th failure: dropped, not re-queued — one drop WARNING.
    with caplog.at_level("WARNING", logger="cbcl.dispatcher"):
        assert await dispatcher.dispatch_agent("analyst") is False
    assert await queue_manager.get_queue_size("analyst") == 0
    drop_lines = [
        r for r in caplog.records
        if "dropping the queue entry" in r.message
    ]
    assert len(drop_lines) == 1
    # The rogue worker was still killed on every attempt (4 total) and
    # the counter was dropped with the entry.
    assert mock_supervisor._kill_process.await_count == 4
    assert "move-fail-cap" not in dispatcher._move_rollback_failures


@pytest.mark.asyncio
async def test_dispatch_move_success_prunes_rollback_counter(
    dispatcher, mock_supervisor, queue_manager,
):
    """A successful ready→in_progress move prunes the per-task rollback
    counter so a later transient failure gets a fresh budget."""
    task = make_task(agent="analyst", task_id="move-fail-prune")
    await queue_manager.add_task("analyst", task)

    dispatcher._move_and_assign = AsyncMock(return_value=False)  # type: ignore[method-assign]
    assert await dispatcher.dispatch_agent("analyst") is False
    assert dispatcher._move_rollback_failures["move-fail-prune"] == 1

    dispatcher._move_and_assign = AsyncMock(return_value=True)  # type: ignore[method-assign]
    assert await dispatcher.dispatch_agent("analyst") is True
    assert "move-fail-prune" not in dispatcher._move_rollback_failures


# ---------------------------------------------------------------------------
# dispatch_all_idle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_all_idle(dispatcher, mock_supervisor, queue_manager):
    """dispatch_all_idle dispatches tasks for all idle agents."""
    t1 = make_task(agent="analyst", task_id="t1")
    t2 = make_task(agent="auditor", task_id="t2")
    await queue_manager.add_task("analyst", t1)
    await queue_manager.add_task("auditor", t2)

    dispatched = await dispatcher.dispatch_all_idle()
    assert dispatched == 2


# ---------------------------------------------------------------------------
# on_agent_complete tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_agent_complete_clears_active(dispatcher, queue_manager):
    """on_agent_complete clears the active task."""
    await queue_manager.set_active("analyst", "t1", "T-t1", "in_progress", "execute", 1234)
    assert await queue_manager.is_busy("analyst")

    await dispatcher.on_agent_complete("analyst")
    assert not await queue_manager.is_busy("analyst")


@pytest.mark.asyncio
async def test_on_agent_complete_dispatches_next(
    dispatcher, queue_manager, mock_supervisor,
):
    """on_agent_complete clears active and dispatches the next task."""
    await queue_manager.set_active("analyst", "t1", "T-t1", "in_progress", "execute", 1234)
    next_task = make_task(agent="analyst", task_id="t2")
    await queue_manager.add_task("analyst", next_task)

    await dispatcher.on_agent_complete("analyst")

    # Should have spawned the next task immediately
    mock_supervisor.spawn_worker.assert_called_once()
    # Agent should now be active with t2 (not t1)
    active = await queue_manager.get_active("analyst")
    assert active is not None
    assert active["task_id"] == "t2"


# ---------------------------------------------------------------------------
# Run/stop tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_stops_on_stop(dispatcher):
    """The run loop exits when stop() is called."""
    run_task = asyncio.create_task(dispatcher.run())
    await asyncio.sleep(0.1)
    await dispatcher.stop()

    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except asyncio.TimeoutError:
        run_task.cancel()
        pytest.fail("Dispatcher did not stop within 2 seconds")


@pytest.mark.asyncio
async def test_wake_triggers_check(dispatcher):
    """Calling wake() triggers the wake event."""
    dispatcher._wake_event.clear()
    dispatcher.wake()
    assert dispatcher._wake_event.is_set()


# ---------------------------------------------------------------------------
# Scope gating tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_skips_task_in_non_executing_scope(
    dispatcher, queue_manager, mock_supervisor
):
    """Tasks belonging to a non-executing scope are NOT dispatched.

    The dispatcher verifies scope state (either cached or via backend)
    and skips dispatch without re-queuing the task. A later scope-activation
    event will re-trigger dispatch.
    """
    # Enqueue a task whose cached scope_state is "preparing"
    task = make_task(agent="analyst", status="ready")
    task["scope_id"] = "scope-123"
    task["scope_state"] = "preparing"
    await queue_manager.add_task("analyst", task)

    # Patch the backend fetch to confirm "preparing"
    async def fake_fetch_scope_state(scope_id):
        return "preparing"

    dispatcher._fetch_scope_state = fake_fetch_scope_state  # type: ignore[method-assign]

    dispatched = await dispatcher.dispatch_agent("analyst")
    assert dispatched is False
    # Spawn should NOT have been called.
    mock_supervisor.spawn_worker.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_proceeds_when_scope_is_executing(
    dispatcher, queue_manager, mock_supervisor
):
    task = make_task(agent="analyst", status="ready")
    task["scope_id"] = "scope-123"
    task["scope_state"] = "executing"
    await queue_manager.add_task("analyst", task)

    # Stub _move_and_assign to avoid HTTP call
    dispatcher._move_and_assign = AsyncMock()  # type: ignore[method-assign]
    dispatched = await dispatcher.dispatch_agent("analyst")
    assert dispatched is True
    mock_supervisor.spawn_worker.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_allows_task_without_scope(
    dispatcher, queue_manager, mock_supervisor
):
    """Legacy tasks (scope_id=None) are always dispatchable."""
    task = make_task(agent="analyst", status="ready")
    # No scope_id set at all
    await queue_manager.add_task("analyst", task)

    dispatcher._move_and_assign = AsyncMock()  # type: ignore[method-assign]
    dispatched = await dispatcher.dispatch_agent("analyst")
    assert dispatched is True
    mock_supervisor.spawn_worker.assert_called_once()


# ---------------------------------------------------------------------------
# Blocked task dispatch — triage mode (no status flip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_blocked_task_uses_assign_only_no_status_flip(
    dispatcher, queue_manager, mock_supervisor,
):
    """C3: dispatching a `blocked` task to the MA must NOT flip its
    status. The MA arrives at a task that is still `blocked` so its
    playbook ('blocked-mode is DOCUMENT-AND-ESCALATE only') operates
    on a truthful column state. The bounce cap and cooldown lock rely
    on this — pre-flipping the task burned one bounce per dispatch and
    contradicted the 'NEVER call move_task(blocked → ready)' rule.
    """
    # Blocked tasks ONLY route to the Manager Assistant (original
    # spec: "No other agent picks up a task from the Blocked
    # column"). Dispatching to any other agent is now refused
    # outright — see ``test_dispatch_refuses_non_ma_agent_on_blocked_task``
    # below. The C3 "no status flip" contract is exercised using
    # the MA, which is the only agent that ever reaches this
    # dispatch path in production.
    task = make_task(agent="manager-assistant", status="blocked")
    await queue_manager.add_task("manager-assistant", task)
    # The dispatcher refreshes the popped task's status from the
    # backend before spawning. Tell the fixture's status-stub to
    # echo "blocked" so the freshness check matches the queue
    # entry and dispatch proceeds.
    dispatcher._test_status_override = "blocked"  # type: ignore[attr-defined]

    move_calls: list[dict] = []
    assign_calls: list[dict] = []

    async def spy_move_and_assign(
        task_id, agent_name, new_status, from_status="ready"
    ):
        move_calls.append(
            {"task_id": task_id, "new_status": new_status,
             "from_status": from_status}
        )

    async def spy_assign_only(task_id, agent_name):
        assign_calls.append({"task_id": task_id, "agent_name": agent_name})

    dispatcher._move_and_assign = spy_move_and_assign  # type: ignore[method-assign]
    dispatcher._assign_only = spy_assign_only  # type: ignore[method-assign]
    dispatched = await dispatcher.dispatch_agent("manager-assistant")
    assert dispatched is True
    assert move_calls == [], (
        "Blocked-task dispatch must NOT call _move_and_assign — that "
        "pre-flips blocked → ready → in_progress and consumes a bounce. "
        f"Got: {move_calls}"
    )
    assert len(assign_calls) == 1
    assert assign_calls[0]["agent_name"] == "manager-assistant"


@pytest.mark.asyncio
async def test_dispatch_ready_task_passes_ready_from_status(
    dispatcher, queue_manager, mock_supervisor,
):
    """Ready tasks don't need the extra hop."""
    task = make_task(agent="analyst", status="ready")
    await queue_manager.add_task("analyst", task)

    captured = {}

    async def spy_move_and_assign(
        task_id, agent_name, new_status, from_status="ready"
    ):
        captured["from_status"] = from_status

    dispatcher._move_and_assign = spy_move_and_assign  # type: ignore[method-assign]
    await dispatcher.dispatch_agent("analyst")
    assert captured["from_status"] == "ready"


# ─── Freshness + cooldown guards (TO-007.T40 regression, 2026-05-14) ──


@pytest.mark.asyncio
async def test_dispatch_drops_stale_queue_entry_when_status_changed(
    dispatcher, queue_manager, mock_supervisor,
):
    """The queue can hold a stale entry — e.g. a task that was in
    ``review`` when enqueued but has since been moved to
    ``blocked``. Dispatching against the stale entry drove the
    auditor to keep running in review-mode on a blocked task. The
    dispatcher MUST refresh the status before spawning and drop
    the entry on mismatch."""
    task = make_task(agent="auditor", status="review")
    await queue_manager.add_task("auditor", task)
    # Backend says the task is now in blocked.
    dispatcher._test_status_override = "blocked"  # type: ignore[attr-defined]

    dispatched = await dispatcher.dispatch_agent("auditor")
    assert dispatched is False, "stale queue entry must not spawn an agent"
    mock_supervisor.spawn_worker.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_drops_terminal_task(
    dispatcher, queue_manager, mock_supervisor,
):
    """A queue entry for a task that's since been archived or
    completed must NOT spawn an agent. Without this guard a stale
    'done' entry would dispatch endlessly."""
    task = make_task(agent="analyst", status="ready")
    await queue_manager.add_task("analyst", task)
    dispatcher._test_status_override = "done"  # type: ignore[attr-defined]

    dispatched = await dispatcher.dispatch_agent("analyst")
    assert dispatched is False
    mock_supervisor.spawn_worker.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_skips_ma_when_blocked_triage_in_cooldown(
    dispatcher, queue_manager, mock_supervisor,
):
    """The MA's cooldown lock must apply at dispatch time, not just
    at WS-routing time. The reconciler re-adds blocked tasks every
    60s; without a dispatch-side cooldown check the MA would still
    be spawned in a no-op loop. This is the canonical TO-007.T40
    regression guard."""
    task = make_task(agent="manager-assistant", status="blocked")
    await queue_manager.add_task("manager-assistant", task)
    dispatcher._test_status_override = "blocked"  # type: ignore[attr-defined]

    async def in_cooldown(task_id: str) -> bool:
        return True
    dispatcher._is_blocked_triage_in_cooldown = in_cooldown  # type: ignore[method-assign]

    dispatched = await dispatcher.dispatch_agent("manager-assistant")
    assert dispatched is False, (
        "MA dispatch on blocked task must be refused while the "
        "triage cooldown is active"
    )
    mock_supervisor.spawn_worker.assert_not_called()


@pytest.mark.asyncio
async def test_is_blocked_triage_in_cooldown_calls_real_helper_correctly(
    dispatcher,
):
    """Regression guard for the TO-007.T40 follow-up on 2026-05-14:
    ``_is_blocked_triage_in_cooldown`` was calling
    ``task_blocked_triage_within_cooldown`` without the required
    ``cooldown_seconds`` kwarg, raising TypeError, and the
    surrounding ``except Exception`` silently swallowed it —
    making the check return False forever. Verify the dispatcher
    invokes the helper with a signature the runtime accepts.

    Restores the real ``_is_blocked_triage_in_cooldown`` (the
    dispatcher fixture stubs it to no-op by default), monkey-
    patches the imported helper, and asserts it was awaited at
    least once. Any future regression where the helper signature
    drifts will surface as TypeError here instead of in production.
    """
    from unittest.mock import AsyncMock
    from src.orchestrator.task_dispatcher import TaskDispatcher

    # Restore the real method onto the test dispatcher.
    real_method = TaskDispatcher._is_blocked_triage_in_cooldown
    bound = real_method.__get__(dispatcher, TaskDispatcher)
    dispatcher._is_blocked_triage_in_cooldown = bound

    # Patch the underlying helper so the call doesn't try to hit
    # an actual backend; assert it's awaited with the dispatcher's
    # state.
    import src.backend_client as backend_client_mod
    spy = AsyncMock(return_value=True)
    original = backend_client_mod.task_should_skip_ma_routing
    backend_client_mod.task_should_skip_ma_routing = spy
    try:
        result = await dispatcher._is_blocked_triage_in_cooldown("task-1")
    finally:
        backend_client_mod.task_should_skip_ma_routing = original

    assert result is True
    spy.assert_awaited_once()
    # Kwargs match what task_should_skip_ma_routing's signature expects;
    # any drift surfaces here, not in production.
    _, kwargs = spy.call_args
    assert set(kwargs.keys()) == {
        "platform_url", "office_id", "task_id", "security_token",
    }


@pytest.mark.asyncio
async def test_dispatch_refuses_non_ma_agent_on_blocked_task(
    dispatcher, queue_manager, mock_supervisor,
):
    """Original spec: 'No other agent picks up a task from the
    Blocked column'. Even when a stale queue entry routes a
    blocked task to the executor (e.g. python-developer's queue),
    the dispatcher MUST refuse to spawn and drop the entry.
    Only the Manager Assistant triages blocked tasks."""
    task = make_task(agent="python-developer", status="blocked")
    await queue_manager.add_task("python-developer", task)
    dispatcher._test_status_override = "blocked"  # type: ignore[attr-defined]

    dispatched = await dispatcher.dispatch_agent("python-developer")
    assert dispatched is False, (
        "Only the Manager Assistant may dispatch on a blocked task"
    )
    mock_supervisor.spawn_worker.assert_not_called()


@pytest.mark.asyncio
async def test_full_sync_routes_blocked_tasks_to_ma_always():
    """Original spec verification: a blocked task with
    ``assigned_agent=python-developer`` must NOT be queued to
    python-developer. It goes to the MA queue regardless. The
    executor's assignment is preserved on the task row for when
    the task transitions back to ``ready``."""
    import fakeredis.aioredis
    from src.orchestrator.agent_queue import AgentQueueManager

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        qm = AgentQueueManager(r, "test-office")
        await qm.full_sync([
            {
                "id": "task-1",
                "task_id": "task-1",
                "assigned_agent": "python-developer",
                "status": "blocked",
                "priority": "high",
                "title": "Stuck task",
            }
        ])

        # MA's queue holds the task; python-developer's queue is empty.
        ma_size = await qm.get_queue_size("manager-assistant")
        dev_size = await qm.get_queue_size("python-developer")
        assert ma_size == 1, "blocked task must be queued to MA"
        assert dev_size == 0, "blocked task must NOT be queued to executor"
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_reconcile_routes_blocked_tasks_to_ma_always():
    """Same as the full_sync test but exercises the reconcile path
    (runs every 60s in production). Without this rule, every
    reconcile cycle would re-add the blocked task to the
    executor's queue."""
    import fakeredis.aioredis
    from src.orchestrator.agent_queue import AgentQueueManager

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        qm = AgentQueueManager(r, "test-office")
        await qm.reconcile([
            {
                "id": "task-1",
                "task_id": "task-1",
                "assigned_agent": "python-developer",
                "status": "blocked",
                "priority": "high",
                "title": "Stuck task",
            }
        ])

        ma_size = await qm.get_queue_size("manager-assistant")
        dev_size = await qm.get_queue_size("python-developer")
        assert ma_size == 1
        assert dev_size == 0
    finally:
        await r.aclose()
