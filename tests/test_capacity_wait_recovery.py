"""Independent recovery review: incomplete projections never erase live intent."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.operations.host_capacity import HostCapacity, HostCapacityUnavailable
from src.orchestrator.task_dispatcher import TaskDispatcher
from src.runtime_state import RuntimeState
from src.scripts.capacity_wait import CapacityWaitCoordinator


@pytest.fixture
def deferred(tmp_path):
    state = RuntimeState(tmp_path / "control.sqlite3", "office")
    budget = HostCapacity(
        tmp_path / "capacity.sqlite3",
        {
            "enabled": True,
            "budgets": {"cpu": {"limit": 1, "per_office": 1}},
            "default_costs": {"cpu": 1},
            "resource_costs": {},
        },
    )
    budget.reserve("holder", "other-office", [])
    runner = SimpleNamespace(_runtime_state=state, _operation_host_capacity=budget)
    coordinator = CapacityWaitCoordinator(runner, clock=lambda: float("inf"))

    def create(task_id="task", *, phase="execute", epoch=0):
        state.observe_cycle(task_id, 1)
        task = {
            "id": task_id,
            "status": "review" if phase == "review" else "in_progress",
            "assigned_agent": "analyst",
            "reviewer": "analyst",
            "active_execution_attempt_id": "original",
            "execution_cycle": 1,
            "execution_generation": 2,
            "review_retry_epoch": epoch,
        }
        caller = {
            "role": "worker",
            "agent_name": "analyst",
            "task_id": task_id,
            "task_mode": phase,
            "attempt_id": "original",
            "execution_cycle": 1,
            "execution_generation": 2,
            "review_retry_epoch": epoch,
        }
        record, _ = state.begin_operation(
            task_id=task_id,
            cycle=1,
            phase=phase,
            key="check",
            fingerprint="b" * 64,
            input_fingerprint="a" * 64,
            script_name="verify",
            attempt_id="original",
            mechanism="local",
            resources=[],
        )
        record = state.update_operation(
            record["operation_id"], state="queued", cleanup_confirmed=True
        )
        with pytest.raises(HostCapacityUnavailable):
            budget.reserve(record["operation_id"], "office", [])
        wait = state.register_capacity_wait(
            record, task, caller, action="start", had_variable_overrides=False
        )
        return SimpleNamespace(task=task, caller=caller, record=record, wait=wait)

    return SimpleNamespace(
        state=state, budget=budget, coordinator=coordinator, create=create
    )


def _assert_preserved(context, intent, *, state="waiting"):
    assert context.state.capacity_wait(intent.task["id"])["state"] == state
    assert (
        context.state.get_operation(intent.record["operation_id"])["state"] == "queued"
    )
    assert (
        context.budget.wait_eligibility(intent.record["operation_id"], "office")[
            "state"
        ]
        == "waiting"
    )


async def test_missing_paginated_projection_requires_authoritative_detail(deferred):
    intent = deferred.create()
    deferred.coordinator.fetch_task = AsyncMock(return_value=(True, dict(intent.task)))
    # Successful offset pages may omit a task after another task leaves a page.
    await deferred.coordinator.reconcile([])
    deferred.coordinator.fetch_task.assert_awaited_once_with("task")
    _assert_preserved(deferred, intent)


@pytest.mark.parametrize("result", [(False, None), (True, {}), (True, {"id": "task"})])
async def test_unknown_or_incomplete_detail_keeps_wait_and_operation(deferred, result):
    intent = deferred.create()
    deferred.coordinator.fetch_task = AsyncMock(return_value=result)
    await deferred.coordinator.reconcile([])
    _assert_preserved(deferred, intent)


async def test_failed_detail_read_keeps_wait_and_operation(deferred):
    intent = deferred.create()
    deferred.coordinator.fetch_task = AsyncMock(
        side_effect=TimeoutError("temporary outage")
    )
    await deferred.coordinator.reconcile([])
    _assert_preserved(deferred, intent)


@pytest.mark.parametrize(
    "field,value",
    [("execution_generation", 1), ("execution_cycle", 0), ("review_retry_epoch", 1)],
)
async def test_older_projection_cannot_retire_newer_review_wait(deferred, field, value):
    intent = deferred.create(phase="review", epoch=2)
    older = {**intent.task, field: value}
    deferred.coordinator.fetch_task = AsyncMock(return_value=(True, older))
    await deferred.coordinator.reconcile([older])
    assert deferred.coordinator.can_dispatch(older) is False
    _assert_preserved(deferred, intent)


async def test_backend_claim_ahead_of_local_binding_keeps_exact_pending_journal(
    deferred,
):
    intent = deferred.create()
    deferred.state.begin_capacity_resume_claim(
        intent.task, intent.wait["wait_id"], "resume"
    )
    deferred.state.begin_worker_claim("analyst", "task", {"attempt_id": "resume"})
    backend = {
        **intent.task,
        "execution_generation": 3,
        "active_execution_attempt_id": "resume",
    }
    deferred.coordinator.fetch_task = AsyncMock(return_value=(True, backend))
    await deferred.coordinator.reconcile([backend])
    _assert_preserved(deferred, intent, state="resuming")
    assert deferred.state.capacity_wait("task")["pending_resume_attempt_id"] == "resume"
    assert deferred.coordinator.can_dispatch(backend) is False


async def test_pre_journal_crash_recovers_wait_without_launch_or_retirement(deferred):
    intent = deferred.create()
    deferred.state.begin_capacity_resume_claim(
        intent.task, intent.wait["wait_id"], "never-posted"
    )
    await deferred.coordinator.reconcile([dict(intent.task)])
    _assert_preserved(deferred, intent)
    assert deferred.state.capacity_wait("task")["pending_resume_attempt_id"] is None


async def test_local_re_registration_during_detail_read_invalidates_retirement(
    deferred,
):
    intent = deferred.create()

    async def read(_task_id):
        newer_task = {
            **intent.task,
            "execution_generation": 3,
            "active_execution_attempt_id": "new-attempt",
        }
        newer_caller = {
            **intent.caller,
            "execution_generation": 3,
            "attempt_id": "new-attempt",
        }
        deferred.state.register_capacity_wait(
            intent.record,
            newer_task,
            newer_caller,
            action="start",
            had_variable_overrides=False,
        )
        return True, {**intent.task, "status": "done"}

    deferred.coordinator.fetch_task = read
    await deferred.coordinator.reconcile([])
    _assert_preserved(deferred, intent)
    current = deferred.state.capacity_wait("task")
    assert current["wait_id"] == intent.wait["wait_id"]
    assert current["generation"] == 3 and current["attempt_id"] == "new-attempt"


@pytest.mark.parametrize("deleted", [False, True])
async def test_authoritative_terminal_or_deleted_task_retires_only_never_started_intent(
    deferred, deleted
):
    intent = deferred.create()
    deferred.coordinator.fetch_task = AsyncMock(
        return_value=(True, None if deleted else {**intent.task, "status": "done"})
    )
    await deferred.coordinator.reconcile([])
    assert deferred.state.capacity_wait("task")["state"] == "retired"
    assert (
        deferred.state.get_operation(intent.record["operation_id"])["state"]
        == "cancelled"
    )
    assert (
        deferred.budget.wait_eligibility(intent.record["operation_id"], "office")[
            "state"
        ]
        == "released"
    )
    assert deferred.budget.list_reserved_operations("other-office") == ["holder"]


async def test_reconciliation_bounds_detail_concurrency_and_retains_keyset_progress(
    deferred,
):
    for number in range(101):
        deferred.create(f"task-{number:03}")
    active = peak = calls = 0

    async def read(_task_id):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return False, None

    deferred.coordinator.fetch_task = read
    await deferred.coordinator.reconcile([])
    assert calls == 100 and peak <= 4
    await deferred.coordinator.reconcile([])
    assert calls == 101 and peak <= 4


@pytest.mark.parametrize(
    "status,payload,known",
    [
        (404, {}, True),
        (403, {}, False),
        (500, {}, False),
        (200, {}, False),
        (200, [], False),
        (200, {"id": "wrong"}, False),
    ],
)
async def test_task_specific_read_never_treats_error_or_malformed_success_as_deletion(
    monkeypatch, status, payload, known
):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(status, json=payload)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    dispatcher = SimpleNamespace(
        _backend_url="https://test.invalid",
        _office_id="office",
        _security_token="test-only",
    )
    assert await TaskDispatcher._fetch_capacity_task_details(dispatcher, "task") == (
        known,
        None,
    )


@pytest.mark.parametrize(
    "change,known",
    [
        ({}, True),
        ({"status": "unexpected"}, False),
        ({"assigned_agent": {}}, False),
        ({"reviewer": []}, False),
        ({"execution_generation": True}, False),
        ({"active_execution_attempt_id": {}}, False),
    ],
)
async def test_task_detail_authority_requires_valid_lifecycle_shape(
    monkeypatch, change, known
):
    task = {
        "id": "task",
        "status": "in_progress",
        "assigned_agent": "analyst",
        "reviewer": None,
        "execution_cycle": 1,
        "execution_generation": 2,
        "review_retry_epoch": 0,
        "active_execution_attempt_id": "original",
        **change,
    }
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(200, json=task)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    dispatcher = SimpleNamespace(
        _backend_url="https://test.invalid",
        _office_id="office",
        _security_token="test-only",
    )
    assert await TaskDispatcher._fetch_capacity_task_details(dispatcher, "task") == (
        known,
        task if known else None,
    )
