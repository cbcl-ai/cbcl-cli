"""Coverage for ``route_task_moved`` blocked-transition behaviour.

Regression: a task could be moved to "blocked" while the assigned
agent was actively executing it, and ``route_task_moved`` would do
NOTHING. The agent kept running and (worse) submitted the task for
review later — the board UI showed the task in the "Blocked" column
while the agent was still producing artefacts.

The fix mirrors the review path: force-kill the executor's
subprocess if it's busy on the now-blocked task.

These tests exercise the handler directly with mocked supervisor /
queue / dispatcher / router collaborators. They do NOT spin up a
real subprocess.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src._handlers._tasks import route_task_moved


def _stub_collaborators(
    *,
    agent_busy: bool,
    active_task_id: str | None,
    ma_active_task_id: str | None = None,
):
    """Build a fresh set of mock collaborators (queue_manager,
    dispatcher, supervisor, router) configured for one test.

    ``active_task_id`` is what the EXECUTOR (e.g. ``python-developer``)
    reports as its current task. ``ma_active_task_id`` is what
    Manager Assistant reports as ITS current task — used to verify
    the MA self-block guard. Default ``None`` for MA so the default
    test path always enqueues MA.
    """
    queue_manager = MagicMock()
    queue_manager.add_task = AsyncMock()
    queue_manager.remove_task_from_all = AsyncMock()
    queue_manager.clear_active = AsyncMock()

    async def _get_active(agent_name: str) -> dict | None:
        if agent_name == "manager-assistant":
            return (
                {"task_id": ma_active_task_id}
                if ma_active_task_id is not None else None
            )
        return {"task_id": active_task_id} if active_task_id else None

    queue_manager.get_active = AsyncMock(side_effect=_get_active)

    dispatcher = MagicMock()
    dispatcher.wake = MagicMock()
    dispatcher.dispatch_agent = AsyncMock()

    supervisor = MagicMock()
    supervisor.is_agent_busy = MagicMock(return_value=agent_busy)
    supervisor._kill_process = AsyncMock()
    supervisor.get_all_statuses = MagicMock(return_value={})

    router = MagicMock()
    router.publish_event = AsyncMock()

    return queue_manager, dispatcher, supervisor, router


@pytest.mark.asyncio
async def test_blocked_force_kills_busy_executor_on_same_task() -> None:
    """The canonical bug: agent X is executing task T, Manager moves
    T to blocked. Without the fix the agent kept running. With the
    fix we kill the subprocess and clear its active slot."""
    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=True, active_task_id="task-blocked-1",
    )

    await route_task_moved(
        {
            "task_id": "task-blocked-1",
            "new_status": "blocked",
            "assigned_agent": "python-developer",
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    sv._kill_process.assert_awaited_once_with("python-developer")
    qm.clear_active.assert_awaited_once_with("python-developer")
    dp.wake.assert_called_once()


@pytest.mark.asyncio
async def test_blocked_always_queues_to_manager_assistant() -> None:
    """Every blocked transition queues Manager Assistant for triage,
    regardless of whether the task has an assigned agent.

    User-requested flow: worker self-blocks with explanation → MA
    picks it up → MA reads activity → MA decides next step (answer
    a question, create a dependency task, post a comment, propose
    an action_request to the user). Before this fix MA was only
    triggered for orphan blocked tasks; assigned-agent blocked
    tasks sat in the column with nobody triaging them."""
    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=False, active_task_id=None,
    )

    await route_task_moved(
        {
            "task_id": "task-blocked-mw",
            "readable_id": "WS-001.T07",
            "new_status": "blocked",
            "assigned_agent": "python-developer",
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    # MA queued even though the task has an assigned worker.
    qm.add_task.assert_awaited_once()
    args = qm.add_task.await_args
    assert args.args[0] == "manager-assistant"
    payload = args.args[1]
    assert payload["task_id"] == "task-blocked-mw"
    assert payload["status"] == "blocked"
    assert payload["readable_id"] == "WS-001.T07"
    dp.dispatch_agent.assert_awaited_once_with("manager-assistant")


@pytest.mark.asyncio
async def test_blocked_does_not_kill_when_agent_idle() -> None:
    """If the agent is no longer busy (already finished + reported
    blocked itself), there's nothing to kill. The handler should be
    a no-op for the force-kill branch. MA is still queued."""
    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=False, active_task_id=None,
    )

    await route_task_moved(
        {
            "task_id": "task-blocked-2",
            "new_status": "blocked",
            "assigned_agent": "python-developer",
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    sv._kill_process.assert_not_called()
    # No kill so no clear of the agent's active slot.
    assert not any(
        call.args[0] == "python-developer"
        for call in qm.clear_active.await_args_list
    )
    # MA is still queued for triage.
    qm.add_task.assert_awaited_once()
    assert qm.add_task.await_args.args[0] == "manager-assistant"


@pytest.mark.asyncio
async def test_blocked_does_not_kill_when_agent_is_on_other_task() -> None:
    """The agent is busy but on a DIFFERENT task than the one being
    blocked (e.g. it already moved on after submitting this task).
    Killing here would abort unrelated work — don't do it.

    MA is still queued for the blocked task triage; the kill
    decision and the MA-queue decision are independent."""
    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=True, active_task_id="task-other",
    )

    await route_task_moved(
        {
            "task_id": "task-blocked-3",
            "new_status": "blocked",
            "assigned_agent": "python-developer",
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    sv._kill_process.assert_not_called()
    # No clear of python-developer's active slot (still on its
    # other task).
    assert not any(
        call.args[0] == "python-developer"
        for call in qm.clear_active.await_args_list
    )
    # MA still triages the blocked task.
    qm.add_task.assert_awaited_once()
    assert qm.add_task.await_args.args[0] == "manager-assistant"


@pytest.mark.asyncio
async def test_blocked_skips_ma_enqueue_when_ma_already_on_this_task() -> None:
    """Self-block guard: if Manager Assistant itself blocks a task
    that it's actively holding (e.g. while waiting on an
    action_request response), do NOT re-queue the same task into
    MA's queue. Without this guard a tight bounce loop is possible —
    block → re-queue → MA picks up → blocks again → re-queue → ...

    The matching ``active_task_id`` for MA short-circuits the
    enqueue; the force-kill side is still considered (separate
    branch)."""
    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=False,
        active_task_id=None,
        ma_active_task_id="task-ma-self-block",
    )

    await route_task_moved(
        {
            "task_id": "task-ma-self-block",
            "new_status": "blocked",
            "assigned_agent": "manager-assistant",
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    qm.add_task.assert_not_called()
    dp.dispatch_agent.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_unassigned_routes_to_manager_assistant() -> None:
    """Existing behaviour preserved: a blocked task with NO assigned
    agent gets queued to Manager Assistant for triage."""
    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=False, active_task_id=None,
    )

    await route_task_moved(
        {
            "task_id": "task-orphan-1",
            "new_status": "blocked",
            "assigned_agent": None,
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    qm.add_task.assert_awaited_once()
    args = qm.add_task.await_args
    assert args.args[0] == "manager-assistant"
    assert args.args[1]["status"] == "blocked"
    dp.dispatch_agent.assert_awaited_once_with("manager-assistant")
    sv._kill_process.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_kill_failure_is_logged_not_raised() -> None:
    """If the kill itself raises (e.g. the subprocess is already
    dead), we log and continue. The blocked transition must NOT
    fail the whole task_moved handler. MA queue still happens."""
    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=True, active_task_id="task-blocked-4",
    )
    sv._kill_process.side_effect = RuntimeError("already dead")

    # Must not raise.
    await route_task_moved(
        {
            "task_id": "task-blocked-4",
            "new_status": "blocked",
            "assigned_agent": "python-developer",
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    # Still cleared the active slot + woke the dispatcher so the
    # agent's queue isn't permanently stuck on the dead task.
    qm.clear_active.assert_any_await("python-developer")
    dp.wake.assert_called_once()
    # MA still triages the blocked task.
    assert qm.add_task.await_args.args[0] == "manager-assistant"


# ─── route_task_updated: only-MA-on-blocked enforcement ───────────────


@pytest.mark.asyncio
async def test_route_task_updated_blocked_routes_to_ma_not_executor():
    """A ``task_updated`` event with ``status=blocked`` AND a
    populated ``assigned_agent`` must route to the MA queue, NOT
    the executor's queue.

    Original spec: "No other agent picks up a task from the
    Blocked column". The c9ab43e fix landed this rule in
    ``full_sync``, ``reconcile``, and ``add_task``, but missed the
    event-driven ``route_task_updated`` path. Caught by the QA
    review on 2026-05-14 (HIGH-1).
    """
    from src._handlers._tasks import route_task_updated

    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=False, active_task_id=None,
    )

    await route_task_updated(
        {
            "task_data": {
                "task_id": "task-blocked-99",
                "status": "blocked",
                "assigned_agent": "python-developer",
                "priority": "high",
                "readable_id": "WS-001.T42",
            },
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    # The task must have been queued to MA, NOT python-developer.
    queued_agents = [
        call.args[0] for call in qm.add_task.await_args_list
    ]
    assert "manager-assistant" in queued_agents
    assert "python-developer" not in queued_agents, (
        "Blocked task with assigned_agent=python-developer must "
        "route to MA queue, not the executor's. Original spec rule."
    )

    # Dispatcher should have been told to dispatch MA.
    dispatched_agents = [
        call.args[0] for call in dp.dispatch_agent.await_args_list
    ]
    assert dispatched_agents == ["manager-assistant"]


@pytest.mark.asyncio
async def test_route_task_updated_blocked_unassigned_still_routes_to_ma():
    """Sanity: pre-existing behaviour for unassigned blocked tasks
    still holds — they route to MA (this is the path that was
    already correct; the regression was for assigned ones)."""
    from src._handlers._tasks import route_task_updated

    qm, dp, sv, rt = _stub_collaborators(
        agent_busy=False, active_task_id=None,
    )

    await route_task_updated(
        {
            "task_data": {
                "task_id": "task-blocked-orphan",
                "status": "blocked",
                "assigned_agent": "",
                "priority": "high",
                "readable_id": "WS-001.T43",
            },
        },
        queue_manager=qm,
        dispatcher=dp,
        supervisor=sv,
        router=rt,
    )

    queued_agents = [
        call.args[0] for call in qm.add_task.await_args_list
    ]
    assert queued_agents == ["manager-assistant"]
