"""C3a-G1: a blocked task whose triage cannot be launched must not loop.

A failed triage launch is deferred to reconciliation (no re-queue), so the
Manager Assistant's other queued work still starts; past the per-cycle
launch-failure budget the dispatcher stops claiming and escalates once.
"""

import json
from unittest.mock import AsyncMock, MagicMock

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


# ── Block notices (S3): a triage session that ended with an error ────────────


def _triage_completion(**overrides):
    event = {
        "type": "task_complete",
        "task_id": "blocked-task",
        "status": "blocked",
        "comment": (
            "ESCALATED (api_overloaded): retries exhausted.\n\n"
            "Original error: overloaded"
        ),
        "details": {"error_class": "api_overloaded", "escalation_message": "retries exhausted"},
        "_caller": {
            "agent_name": "manager-assistant", "task_id": "blocked-task",
            "attempt_id": "attempt-1", "execution_cycle": 2,
            "execution_generation": 5, "task_mode": "triage",
        },
    }
    event.update(overrides)
    return event


def test_triage_error_completion_is_reported_not_moved():
    from src.execution_completion import completion_disposition, triage_failure_report

    task = {"status": "blocked", "execution_cycle": 2, "execution_generation": 5}
    event = _triage_completion()
    # The blocked -> blocked move is never sent: the disposition skips it.
    assert completion_disposition(task, event, active_scripts=False) == "already_transitioned"
    report = triage_failure_report(task, event)
    assert report["cycle"] == 2 and report["attempt_id"] == "attempt-1"
    assert "ended with an error (api_overloaded); the task stays Blocked" in report["comment"]
    assert "ESCALATED (api_overloaded): retries exhausted." in report["comment"]
    assert report["cause"] == "the triage session ended with an error (api_overloaded): retries exhausted"


def test_only_a_failed_triage_on_a_blocked_task_is_reported():
    from src.execution_completion import triage_failure_report

    task = {"status": "blocked", "execution_cycle": 2}
    assert triage_failure_report(task, _triage_completion(details={})) is None
    assert triage_failure_report(
        task, _triage_completion(details={"error_class": "cancelled"})
    ) is None
    execute = _triage_completion()
    execute["_caller"] = {**execute["_caller"], "task_mode": "execute"}
    assert triage_failure_report(task, execute) is None
    assert triage_failure_report({"status": "ready", "execution_cycle": 2}, _triage_completion()) is None
    assert triage_failure_report(task, _triage_completion(is_review_completion=True)) is None
    # A report is capped.
    long = triage_failure_report(task, _triage_completion(comment="x" * 9000))
    assert len(long["comment"]) < 4300


async def test_three_failed_triage_sessions_raise_the_escalation(dispatcher, monkeypatch):
    from src.execution_completion import record_triage_failure, triage_failure_report

    state = dispatcher._runtime_state
    published = []

    async def publish(event):
        published.append(event)

    task = {"status": "blocked", "execution_cycle": 2}
    for attempt in range(TRIAGE_LAUNCH_FAILURE_BUDGET):
        caller = {**_triage_completion()["_caller"], "attempt_id": f"attempt-{attempt}"}
        report = triage_failure_report(task, _triage_completion(_caller=caller))
        count = await record_triage_failure(publish, state, "blocked-task", report)
        assert count == attempt + 1
    # A replayed completion of the same attempt counts once.
    assert await record_triage_failure(publish, state, "blocked-task", report) == TRIAGE_LAUNCH_FAILURE_BUDGET

    # Each report is a system comment on the task, never an MA activity
    # (which would start the blocked-triage cooldown) and never a move.
    assert len(published) == TRIAGE_LAUNCH_FAILURE_BUDGET + 1
    for event in published:
        assert event["type"] == "task_activity"
        assert event["event_type"] == "comment"
        assert event["actor"] == "system"
        assert "_caller" not in event

    calls = _mock_tool_call(
        monkeypatch,
        httpx.Response(200, json={"action_request_id": "ar-1", "status": "pending"}),
    )
    await dispatcher.add_task(dict(BLOCKED))
    assert not await dispatcher.dispatch_agent("manager-assistant")
    dispatcher._supervisor.spawn_worker.assert_not_awaited()
    assert len(calls) == 1
    summary = calls[0]["params"]["payload"]["blocker_summary"]
    assert "could not be started or did not complete" in summary
    assert "the triage session ended with an error (api_overloaded)" in summary


async def test_report_failures_never_raise():
    from src.execution_completion import record_triage_failure

    async def broken(_event):
        raise ConnectionError("offline")

    state = MagicMock()
    state.record_triage_launch_failure.side_effect = RuntimeError("ledger")
    report = {"comment": "c", "cause": "x", "cycle": 1, "attempt_id": ""}
    assert await record_triage_failure(broken, state, "t" * 8, report) is None
    # An attempt without an id still counts (a fresh synthetic id).
    assert state.record_triage_launch_failure.call_args.args[2].startswith("completion-")


def test_completion_handler_reports_failed_triage():
    """The executor completion path hands a triage error to the report."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("src", "handlers.py").read_text()
    branch = source.split('if disposition == "already_transitioned":', 1)[1].split(
        'if disposition != "normal":', 1
    )[0]
    assert "triage_failure_report(completion_task, event)" in branch
    assert "record_triage_failure(" in branch
    assert "router.publish_event, runtime_state, task_id" in branch
