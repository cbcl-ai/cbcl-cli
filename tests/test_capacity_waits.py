"""Capacity parks a fenced phase and resumes a model without replaying secrets."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from src.execution_completion import completion_disposition
from src.operation_state import OperationConflict
from src.operations.host_capacity import HostCapacity
from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.task_dispatcher import TaskDispatcher
from src.runtime_state import RuntimeState
from src.scripts.capacity_wait import CapacityWaitAccepted, CapacityWaitCoordinator
from tests.test_managed_operations import context, finish, launch, script  # noqa: F401


@pytest.fixture
def waiting_context(context):
    context.task = {
        "id": "task",
        "status": "in_progress",
        "execution_cycle": 1,
        "execution_generation": 2,
        "review_retry_epoch": 0,
        "assigned_agent": "analyst",
        "active_execution_attempt_id": "attempt",
        "execution_resources": [],
    }
    context.caller.update(
        execution_generation=2, review_retry_epoch=0, expected_assigned_agent="analyst"
    )
    context.runner._assert_task_runnable = AsyncMock(
        side_effect=lambda *_: dict(context.task)
    )
    context.budget = HostCapacity(
        context.workspace / "capacity.sqlite",
        {
            "enabled": True,
            "budgets": {"host": {"limit": 1, "per_office": 1}},
            "default_costs": {"host": 1},
        },
    )
    context.runner._operation_host_capacity = context.budget
    context.budget.reserve("occupier", "other-office", [])
    context.coordinator = CapacityWaitCoordinator(context.runner)
    script(context)
    return context


async def park(ctx, **kwargs):
    with pytest.raises(CapacityWaitAccepted) as caught:
        await launch(ctx, **kwargs)
    return caught.value.receipt


def due(ctx):
    ctx.state.delay_capacity_wait(
        ctx.state.capacity_wait("task")["wait_id"], seconds=-1
    )


def free(ctx):
    ctx.budget.release("occupier", "other-office", cleanup_confirmed=True)
    due(ctx)


def bind_resume(ctx, attempt="new-attempt"):
    wait = ctx.state.capacity_wait("task")
    ctx.state.begin_capacity_resume_claim(ctx.task, wait["wait_id"], attempt)
    ctx.state.begin_worker_claim("analyst", "task", {"attempt_id": attempt})
    claim = {
        "attempt_id": attempt,
        "agent_name": "analyst",
        "execution_cycle": 1,
        "execution_generation": ctx.task["execution_generation"] + 1,
        "review_retry_epoch": 0,
    }
    ctx.state.record_worker_claim(attempt, claim)
    ctx.task.update(
        execution_generation=claim["execution_generation"],
        active_execution_attempt_id=attempt,
    )
    ctx.caller.update(
        execution_generation=claim["execution_generation"], attempt_id=attempt
    )
    return claim


@pytest.mark.parametrize(
    "phase,status,owner",
    [
        ("execute", "in_progress", "analyst"),
        ("review", "review", "manager-assistant"),
        ("triage", "blocked", "manager-assistant"),
    ],
)
async def test_full_capacity_parks_exact_phase_without_preparation_or_missing_verdict(
    waiting_context, phase, status, owner
):
    ctx = waiting_context
    ctx.task["status"] = status
    ctx.caller.update(task_mode=phase, agent_name=owner)
    receipt = await park(ctx, variable_overrides={"TOKEN": "never-persist-this-secret"})
    assert receipt["accepted"] and receipt["wait"]["phase"] == phase
    assert "execution_id" not in receipt and not ctx.runner._active
    assert not list((ctx.workspace / ".scripts/local").glob("runs/*"))
    wait = ctx.state.capacity_wait("task")
    assert wait["resume_context"]["had_variable_overrides"] is True
    assert (
        "never-persist-this-secret"
        not in ctx.state.database_path.read_bytes().decode(errors="ignore")
    )
    event = {
        "_caller": ctx.caller,
        "status": "done" if phase == "review" else "review",
        "is_review_completion": phase == "review",
    }
    handoff = ctx.state.capacity_completion_handoff(ctx.task, event)
    assert handoff
    assert (
        completion_disposition(
            ctx.task, event, active_scripts=False, capacity_wait=handoff
        )
        == "capacity_handoff"
    )
    event["_caller"] = {**ctx.caller, "attempt_id": "stale"}
    assert not ctx.state.capacity_completion_handoff(ctx.task, event)
    assert ctx.state.review_state("task", 1, "manager-assistant")["failures"] == 0


async def test_restart_retry_preserves_intent_without_automatic_script_launch(
    waiting_context,
):
    ctx = waiting_context
    first = await park(ctx)
    ctx.state = RuntimeState(ctx.state.database_path, "office")
    ctx.runner.set_runtime_state(ctx.state)
    ctx.coordinator = CapacityWaitCoordinator(ctx.runner)
    due(ctx)
    for _ in range(3):
        assert not ctx.coordinator.can_dispatch(dict(ctx.task))
    assert not ctx.runner._active
    free(ctx)
    task = dict(ctx.task)
    assert ctx.coordinator.can_dispatch(task)
    assert task["capacity_wait_resume"]["operation_id"] == first["wait"]["operation_id"]
    assert not ctx.runner._active
    bind_resume(ctx)
    ctx.state.complete_capacity_resume("task", ctx.caller["attempt_id"])
    execution = await launch(ctx)
    result = await finish(ctx, execution)
    assert result["operation_id"] == first["wait"]["operation_id"]
    assert ctx.state.capacity_wait("task")["state"] == "retired"


async def test_capacity_race_reparks_new_attempt_with_same_operation(waiting_context):
    ctx = waiting_context
    first = await park(ctx)
    free(ctx)
    assert ctx.coordinator.can_dispatch(dict(ctx.task))
    bind_resume(ctx)
    ctx.state.complete_capacity_resume("task", ctx.caller["attempt_id"])
    # A long model boot can outlive never-started queue priority; another
    # accepted operation may then take the slot before this worker retries.
    with ctx.budget._connection() as connection:
        connection.execute(
            "UPDATE capacity_leases SET updated_at=0 WHERE state='waiting'"
        )
    ctx.budget.reserve("next-occupier", "other-office", [])
    second = await park(ctx)
    assert second["wait"]["operation_id"] == first["wait"]["operation_id"]
    wait = ctx.state.capacity_wait("task")
    assert wait["attempt_id"] == "new-attempt" and wait["generation"] == 3
    assert ctx.state.capacity_completion_handoff(ctx.task, {"_caller": ctx.caller})


async def test_accepted_wait_does_not_relax_changed_input_fingerprint(waiting_context):
    ctx = waiting_context
    await park(ctx, variable_overrides={"INPUT": "original"})
    free(ctx)
    with pytest.raises(OperationConflict, match="inputs changed"):
        await launch(ctx, variable_overrides={"INPUT": "guessed replacement"})
    assert not ctx.runner._active


async def test_claim_binding_boot_failure_keeps_phase_waiting(waiting_context):
    ctx = waiting_context
    await park(ctx)
    bind_resume(ctx)
    wait = ctx.state.capacity_wait("task")
    assert wait["state"] == "resuming" and wait["generation"] == 3
    assert wait["pending_resume_attempt_id"] is None
    assert not ctx.coordinator.can_dispatch(dict(ctx.task))
    ctx.state.forget_worker_execution("new-attempt")
    assert ctx.state.capacity_wait_for_task(ctx.task)["state"] == "waiting"
    assert not ctx.runner._active


async def test_claim_gap_recovery_and_duplicate_claim_fenced(waiting_context):
    ctx = waiting_context
    await park(ctx)
    wait = ctx.state.capacity_wait("task")
    ctx.state.begin_capacity_resume_claim(ctx.task, wait["wait_id"], "new")
    with pytest.raises(OperationConflict):
        ctx.state.begin_capacity_resume_claim(ctx.task, wait["wait_id"], "other")
    ctx.state.reconcile_capacity_claim_gaps()
    assert ctx.state.capacity_wait("task")["state"] == "waiting"
    ctx.state.begin_capacity_resume_claim(ctx.task, wait["wait_id"], "new")
    ctx.state.begin_worker_claim("analyst", "task", {"attempt_id": "new"})
    ctx.state.reconcile_capacity_claim_gaps()
    assert ctx.state.capacity_wait("task")["state"] == "resuming"


async def test_stop_retires_only_never_started_priority(waiting_context):
    ctx = waiting_context
    receipt = await park(ctx)
    ctx.runner.suppress_task("task")
    assert ctx.state.capacity_wait("task")["state"] == "retired"
    assert (
        ctx.state.get_operation(receipt["wait"]["operation_id"])["state"] == "cancelled"
    )
    assert ctx.budget.status()["counts"].get("reserved") == 1
    assert ctx.budget.status()["counts"].get("waiting", 0) == 0


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("triage", [False, True])
async def test_dispatcher_full_wait_uses_fresh_lineage_never_spawns_model(
    waiting_context, dynamic, triage
):
    ctx = waiting_context
    owner = "manager-assistant" if triage else "analyst"
    status = "blocked" if triage else "in_progress"
    ctx.task["status"] = status
    ctx.caller.update(task_mode="triage" if triage else "execute", agent_name=owner)
    await park(ctx)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = AgentQueueManager(redis, "office")
    supervisor = MagicMock()
    supervisor.execution_policy = {"enabled": dynamic}
    supervisor.is_agent_busy.return_value = False
    supervisor.is_task_busy.return_value = False
    supervisor.spawn_worker = AsyncMock(return_value=True)
    supervisor.get_task_agent.return_value = None
    supervisor.get_agent_current_task.return_value = None
    config = MagicMock()
    config.get_agent.return_value = {"name": owner}
    dispatcher = TaskDispatcher(
        redis=redis,
        office_id="office",
        supervisor=supervisor,
        config_store=config,
        queue_manager=queue,
    )
    dispatcher.set_runtime_state(ctx.state)
    dispatcher.set_capacity_waits(ctx.coordinator)
    dispatcher._watchdog = SimpleNamespace(respawn_capped=lambda _: True)
    dispatcher._check_dependencies = AsyncMock(return_value=True)
    dispatcher._is_blocked_triage_in_cooldown = AsyncMock(return_value=True)

    async def fresh(task_id):
        dispatcher._fresh_task_details[task_id] = dict(ctx.task)
        return ctx.task["status"]

    dispatcher._fetch_task_status = fresh
    try:
        for _ in range(2):
            await queue.add_task(
                owner,
                {
                    "task_id": "task",
                    "assigned_agent": "analyst",
                    "status": status,
                },
            )
            assert not await dispatcher.dispatch_agent(owner)
        supervisor.spawn_worker.assert_not_awaited()
        free(ctx)
        await queue.add_task(
            owner,
            {"task_id": "task", "assigned_agent": "analyst", "status": status},
        )
        assert await dispatcher.dispatch_agent(owner)
        supervisor.spawn_worker.assert_awaited_once()
        task = supervisor.spawn_worker.call_args.args[2]
        assert (
            task["capacity_wait_resume"]["operation_id"]
            == ctx.state.capacity_wait("task")["operation_id"]
        )
        assert task["assigned_agent"] == owner
        assert ctx.task["assigned_agent"] == "analyst"
        assert not ctx.runner._active
    finally:
        await redis.aclose()


async def test_lost_accepted_response_replays_same_wait_over_real_proxy(
    waiting_context,
):
    import uuid
    from src.tool_proxy_server import ToolProxyServer
    from tests.test_operation_proxy import _post

    ctx = waiting_context
    server = ToolProxyServer(
        MagicMock(connected=True), port=0, host="127.0.0.1", script_runner=ctx.runner
    )
    server.set_runtime_state(ctx.state)
    server.set_execution_validator(
        lambda caller, task: caller == ctx.caller and task == "task"
    )
    credentials = server.sessions.issue(ctx.caller, lambda: True)
    await server.start()
    request = {
        "script_name": "local",
        "invocation_id": str(uuid.uuid4()),
        "operation": {"key": "export", "input_fingerprint": "a" * 64},
    }
    try:
        first_status, first = await _post(
            server, "/script-execute-host", request, token=credentials.tool_token
        )
        second_status, second = await _post(
            server, "/script-execute-host", request, token=credentials.tool_token
        )
        assert first_status == second_status == 202
        assert first == second and first["accepted"] is True
        assert len(ctx.state.active_capacity_waits()) == 1
        assert len(ctx.state.list_operations("task")) == 1
        assert not ctx.runner._active
    finally:
        await server.stop()


@pytest.mark.parametrize("action", ["reconcile", "cancel"])
async def test_external_observer_wait_retains_original_remote_identity(
    waiting_context, action
):
    ctx = waiting_context
    ctx.runner._operation_host_capacity = None
    path = script(
        ctx,
        name="external",
        manifest="operation_mode: external\noperation_reconcile_entry_point: reconcile.py\noperation_cancel_entry_point: cancel.py\n",
        body="import os,json\nfrom pathlib import Path\nPath(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':{'service':'fake','run_id':'one'},'state':'running'}))\n",
    )
    (path / "reconcile.py").write_text("raise AssertionError('not launched')\n")
    (path / "cancel.py").write_text("raise AssertionError('not launched')\n")
    result = await finish(ctx, await launch(ctx, name="external"))
    ctx.runner._operation_host_capacity = ctx.budget
    with pytest.raises(CapacityWaitAccepted):
        await ctx.runner.control_operation(result["operation_id"], action, ctx.caller)
    wait = ctx.state.capacity_wait("task")
    assert wait["resume_context"]["action"] == action
    assert (
        ctx.state.get_operation(result["operation_id"])["external_ref"]
        == result["external_ref"]
    )
    ctx.runner.suppress_task("task")
    retained = ctx.state.get_operation(result["operation_id"])
    assert (
        retained["state"] == "unknown"
        and retained["external_ref"] == result["external_ref"]
    )
    assert ctx.budget.status()["counts"]["reserved"] == 1
