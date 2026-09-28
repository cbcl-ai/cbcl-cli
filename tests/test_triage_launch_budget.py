"""C3a-G1: a blocked task whose triage cannot be launched must not loop.

A failed triage launch is deferred to reconciliation (no re-queue), so the
Manager Assistant's other queued work still starts; past the per-cycle
launch-failure budget the dispatcher stops claiming and escalates once.
"""

import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest_asyncio

from src.config_sync.sync_service import ConfigStore
from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.agent_supervisor import AgentSupervisor
from src.orchestrator.task_dispatcher import TaskDispatcher
from src.runtime_state import RuntimeState
from src.triage_launch_state import TRIAGE_LAUNCH_FAILURE_BUDGET

BLOCKED = {
    "task_id": "blocked-task",
    "readable_id": "WS-001.T01",
    "status": "blocked",
    "assigned_agent": "engineer",
    "priority": "low",
    "execution_cycle": 2,
    "created_at": "2026-09-01T00:00:00+00:00",
}
READY = {
    "task_id": "ready-task",
    "readable_id": "WS-001.T02",
    "status": "ready",
    "assigned_agent": "manager-assistant",
    "priority": "urgent",
    "execution_cycle": 0,
    "created_at": "2026-09-01T00:00:00+00:00",
}


@pytest_asyncio.fixture
async def dispatcher(tmp_path):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    config = ConfigStore()
    config.agents = [
        {"name": name, "is_active": True} for name in ("engineer", "manager-assistant")
    ]
    supervisor = AgentSupervisor(".", "office")
    supervisor.spawn_worker = AsyncMock(
        side_effect=lambda _agent, _config, task, **_kw: task["status"] != "blocked"
    )
    queues = AgentQueueManager(redis, "office")
    result = TaskDispatcher(
        redis, "office", supervisor, config, queues, backend_url="http://backend"
    )
    statuses = {BLOCKED["task_id"]: "blocked", READY["task_id"]: "ready"}
    result._fetch_task_status = AsyncMock(side_effect=lambda task_id: statuses[task_id])
    result._is_blocked_triage_in_cooldown = AsyncMock(return_value=False)
    result._move_and_assign = AsyncMock(return_value=True)
    result.set_runtime_state(RuntimeState(tmp_path / "runtime.sqlite", "office"))
    try:
        yield result
    finally:
        await redis.aclose()


async def test_failed_triage_launch_is_not_requeued_and_ma_ready_work_starts(
    dispatcher,
):
    await dispatcher.add_task(dict(BLOCKED))
    await dispatcher.add_task(dict(READY))
    queued = await dispatcher._qm.get_queue_task_ids("manager-assistant")
    assert queued == {"blocked-task", "ready-task"}

    # The blocked entry outranks Ready work; its launch fails.
    assert not await dispatcher.dispatch_agent("manager-assistant")
    first = dispatcher._supervisor.spawn_worker.await_args_list[0].args[2]
    assert first["task_id"] == "blocked-task"
    # Not re-queued: the next tick admits the urgent Ready task.
    assert await dispatcher._qm.get_queue_task_ids("manager-assistant") == {
        "ready-task"
    }
    assert await dispatcher.dispatch_agent("manager-assistant")
    second = dispatcher._supervisor.spawn_worker.await_args_list[1].args[2]
    assert second["task_id"] == "ready-task"
    assert dispatcher._supervisor.spawn_worker.await_count == 2


def _mock_tool_call(monkeypatch, response):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return response

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(handler), **kw),
    )
    return calls


async def test_exhausted_triage_budget_escalates_once_instead_of_claiming(
    dispatcher, monkeypatch
):
    state = dispatcher._runtime_state
    for attempt in range(TRIAGE_LAUNCH_FAILURE_BUDGET):
        state.record_triage_launch_failure(
            "blocked-task",
            2,
            f"attempt-{attempt}",
            "Worker process could not be launched",
        )
    calls = _mock_tool_call(
        monkeypatch,
        httpx.Response(200, json={"action_request_id": "ar-1", "status": "pending"}),
    )
    await dispatcher.add_task(dict(BLOCKED))
    assert not await dispatcher.dispatch_agent("manager-assistant")

    dispatcher._supervisor.spawn_worker.assert_not_awaited()
    assert len(calls) == 1
    params = calls[0]["params"]
    assert calls[0]["action"] == "propose_action"
    assert params["request_type"] == "escalate_blocker"
    assert params["source_task_id"] == "blocked-task"
    assert params["requesting_agent"] == "system-dispatcher"
    # R13: the host dispatcher holds no execution attempt; the backend
    # accepts the escalation for an executed task only from the system actor.
    assert params["actor"] == "system"
    assert "_caller" not in calls[0] and "_caller" not in params
    assert (
        "Worker process could not be launched" in params["payload"]["blocker_summary"]
    )
    # Filed: the pending request now suppresses routing; a rejection gets a
    # fresh budget instead of an unrecoverable skip.
    assert state.triage_launch_failures("blocked-task", 2) == (0, "")


async def test_unaccepted_escalation_keeps_triage_paused_and_retries_later(
    dispatcher, monkeypatch
):
    state = dispatcher._runtime_state
    for attempt in range(TRIAGE_LAUNCH_FAILURE_BUDGET):
        state.record_triage_launch_failure(
            "blocked-task", 2, f"a{attempt}", "boot failed"
        )
    calls = _mock_tool_call(monkeypatch, httpx.Response(503, text="offline"))
    await dispatcher.add_task(dict(BLOCKED))
    assert not await dispatcher.dispatch_agent("manager-assistant")
    dispatcher._supervisor.spawn_worker.assert_not_awaited()
    assert len(calls) == 1
    assert (
        state.triage_launch_failures("blocked-task", 2)[0]
        == TRIAGE_LAUNCH_FAILURE_BUDGET
    )
    assert not await dispatcher._qm.get_queue_task_ids("manager-assistant")


async def test_refused_escalation_logs_the_backend_reason(
    dispatcher, monkeypatch, caplog
):
    """R13: a 200 carrying ``{error, code}`` is a refusal; name it in the log."""
    state = dispatcher._runtime_state
    for attempt in range(TRIAGE_LAUNCH_FAILURE_BUDGET):
        state.record_triage_launch_failure(
            "blocked-task", 2, f"a{attempt}", "boot failed"
        )
    _mock_tool_call(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "error": "Execution identity is required for this task.",
                "code": "stale_execution",
            },
        ),
    )
    await dispatcher.add_task(dict(BLOCKED))
    with caplog.at_level("WARNING"):
        assert not await dispatcher.dispatch_agent("manager-assistant")
    refused = [r.getMessage() for r in caplog.records if "not accepted" in r.getMessage()]
    assert refused, caplog.text
    assert "Execution identity is required for this task." in refused[0]
    assert "stale_execution" in refused[0]
    assert (
        state.triage_launch_failures("blocked-task", 2)[0]
        == TRIAGE_LAUNCH_FAILURE_BUDGET
    )


async def test_new_cycle_starts_fresh_triage_launch_budget(dispatcher):
    state = dispatcher._runtime_state
    for attempt in range(TRIAGE_LAUNCH_FAILURE_BUDGET):
        state.record_triage_launch_failure(
            "blocked-task", 1, f"a{attempt}", "old cycle"
        )
    await dispatcher.add_task(dict(BLOCKED))
    assert not await dispatcher.dispatch_agent("manager-assistant")
    dispatcher._supervisor.spawn_worker.assert_awaited_once()


async def test_failures_below_budget_in_this_cycle_still_launch_triage(
    dispatcher, monkeypatch
):
    """One failure short of the budget in the dispatched cycle still launches
    (R28): pins the ``>=`` boundary, so a check that stops one launch early
    (``>= budget - 1``) or at the first failure (``> 0``) fails here. That
    launch's failure reaches the budget, so the next dispatch escalates
    instead of launching again (``> budget`` fails here)."""
    state = dispatcher._runtime_state
    for attempt in range(TRIAGE_LAUNCH_FAILURE_BUDGET - 1):
        state.record_triage_launch_failure(
            "blocked-task", 2, f"a{attempt}", "boot failed"
        )

    def failed_launch(_agent, _config, task, **_kw):
        # The supervisor meters a triage session that never started;
        # that counting is pinned in test_dynamic_agent_supervisor.py.
        state.record_triage_launch_failure(
            task["task_id"], 2, "last", "boot failed"
        )
        return False

    dispatcher._supervisor.spawn_worker.side_effect = failed_launch
    calls = _mock_tool_call(
        monkeypatch,
        httpx.Response(200, json={"action_request_id": "ar-1", "status": "pending"}),
    )
    await dispatcher.add_task(dict(BLOCKED))
    assert not await dispatcher.dispatch_agent("manager-assistant")  # launch fails
    dispatcher._supervisor.spawn_worker.assert_awaited_once()
    assert calls == []  # no escalation below the budget
    assert (
        state.triage_launch_failures("blocked-task", 2)[0]
        == TRIAGE_LAUNCH_FAILURE_BUDGET
    )

    # Reconciliation re-queues the task; the budget is now reached.
    await dispatcher.add_task(dict(BLOCKED))
    assert not await dispatcher.dispatch_agent("manager-assistant")
    dispatcher._supervisor.spawn_worker.assert_awaited_once()  # not relaunched
    assert [call["params"]["request_type"] for call in calls] == ["escalate_blocker"]
