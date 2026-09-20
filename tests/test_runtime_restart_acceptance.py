"""Reopen durable control state without replaying unconfirmed business work."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio

from src.config import OfficeConfig, OfficeResourceLimits
from src.config_sync.sync_service import ConfigStore
from src.docker.limits_reconciler import ResourceLimitReconciler
from src.health.reporter import HealthReporter
from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.orchestrator.task_dispatcher import TaskDispatcher
from src.runtime_state import RuntimeState
from src.tool_proxy_identity import ProxySessionRegistry
from src.watchdog import TaskWatchdog


@pytest.fixture
def runtime(tmp_path):
    return RuntimeState(tmp_path / "runtime" / "control.sqlite3", "office")


@pytest_asyncio.fixture
async def queue():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield AgentQueueManager(client, "office"), client
    finally:
        await client.aclose()


def outcome():
    return {
        "type": "task_complete", "task_id": "previous", "status": "review",
        "_caller": {
            "agent_name": "engineer", "role": "worker", "task_id": "previous",
            "attempt_id": "attempt", "execution_cycle": 2, "execution_generation": 5,
            "expected_assigned_agent": "engineer", "task_mode": "execute",
        },
    }


def ready_task():
    return {
        "task_id": "next", "readable_id": "TEST.T2", "title": "Next task",
        "assigned_agent": "engineer", "status": "ready", "priority": "medium",
    }


def assemble_dispatcher(runtime, supervisor, queue):
    queue_manager, client = queue
    config = ConfigStore()
    config.agents = [{"name": "engineer", "is_active": True}, {"name": "auditor", "is_active": True}]
    dispatcher = TaskDispatcher(client, "office", supervisor, config, queue_manager)
    dispatcher.set_runtime_state(runtime)
    return dispatcher, config


async def test_restart_finalizes_while_paused_and_only_resume_admits_next_task(runtime, queue, monkeypatch):
    runtime.retain_completion("engineer", "attempt", "previous", outcome())
    runtime.set_maintenance(True)
    reopened = RuntimeState(runtime.database_path, "office")
    callback = AsyncMock(side_effect=RuntimeError("transport unavailable"))
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(reopened)
    dispatcher, config = assemble_dispatcher(reopened, supervisor, queue)
    launch = AsyncMock(return_value=True)
    monkeypatch.setattr(supervisor, "_spawn_worker", launch)
    monkeypatch.setattr(dispatcher, "_fetch_task_status", AsyncMock(return_value="ready"))
    monkeypatch.setattr(dispatcher, "_check_dependencies", AsyncMock(return_value=True))
    monkeypatch.setattr(dispatcher, "_move_and_assign", AsyncMock(return_value=True))
    await dispatcher.add_task(ready_task())
    reporter = HealthReporter(supervisor=supervisor, runtime_state=reopened)
    config.mark_resource_limits_applied(OfficeResourceLimits(cpus=4.0, memory="8g"))
    containers = MagicMock()
    containers.recreate_office = AsyncMock()
    limits = ResourceLimitReconciler(
        containers=containers, office=OfficeConfig(id="office", name="Office"),
        config_store=config, supervisor=supervisor,
    )

    assert not await dispatcher.dispatch_agent("engineer")
    assert await dispatcher.get_queue_size() == 1
    assert (await reporter._build_report())["maintenance"]["state"] == "draining"
    assert await limits.on_sync_config({"container_cpus": 8, "container_memory": "16g"}) == "deferred"
    await supervisor.retry_pending_cleanup()
    assert reopened.has_pending_completion("previous")
    assert not await dispatcher.dispatch_agent("engineer")
    callback.side_effect = None
    await supervisor.retry_pending_cleanup()
    assert callback.await_count == 2
    assert callback.await_args.args == ("engineer", outcome())
    assert not reopened.has_pending_completion("previous")
    assert (await reporter._build_report())["maintenance"]["state"] == "drained"
    assert await limits.recheck_pending() == "deferred"
    assert not await dispatcher.dispatch_agent("engineer")
    launch.assert_not_awaited()
    RuntimeState(runtime.database_path, "office").set_maintenance(False)
    assert await dispatcher.dispatch_agent("engineer")
    launch.assert_awaited_once()
    assert launch.await_args.args[2]["task_id"] == "next"
    assert reopened.maintenance_status()["pending_admissions"] == 0
    containers.recreate_office.assert_not_awaited()


async def test_restart_preserves_review_hold_while_deferring_derived_queue(runtime, queue, monkeypatch):
    runtime.observe_cycle("review-task", 2)
    runtime.record_review_attempt("review-task", 2, "auditor", "review-attempt")
    runtime.hold_review("review-task", 2, "auditor", "human-review-request")
    reopened = RuntimeState(runtime.database_path, "office")
    supervisor = AgentSupervisor(".", "office")
    supervisor.set_runtime_state(reopened)
    dispatcher, _config = assemble_dispatcher(reopened, supervisor, queue)
    detail = {
        "task_id": "review-task", "status": "review", "execution_cycle": 2,
        "reviewer": "auditor", "assigned_agent": "engineer",
    }
    response = httpx.Response(200, json=detail)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    launch = AsyncMock()
    monkeypatch.setattr(supervisor, "_spawn_worker", launch)
    await dispatcher.add_task(detail)
    assert not await dispatcher.dispatch_agent("auditor")
    assert await dispatcher.get_queue_size() == 0
    launch.assert_not_awaited()
    assert reopened.review_state("review-task", 2, "auditor")["request_id"] == "human-review-request"
    # Reconciliation restores the queue projection, but the durable hold still
    # blocks execution after a daemon restart; it was never erased to unblock it.
    await queue[0].reconcile([detail])
    assert await dispatcher.get_queue_size() == 1
    assert not await dispatcher.dispatch_agent("auditor")
    launch.assert_not_awaited()
    assert reopened.review_state("review-task", 2, "auditor")["request_id"] == "human-review-request"
    assert reopened.review_state("review-task", 2, "different-reviewer")["request_id"] is None


async def test_reopened_crash_cap_prevents_replacement_before_watchdog_tick(runtime, queue, monkeypatch):
    runtime.observe_cycle("next", 2)
    for attempt_id in ("first", "second", "third"):
        runtime.record_failure("next", attempt_id, 2)
    reopened = RuntimeState(runtime.database_path, "office")
    supervisor = AgentSupervisor(".", "office")
    supervisor.set_runtime_state(reopened)
    dispatcher, config = assemble_dispatcher(reopened, supervisor, queue)
    watchdog = TaskWatchdog(None, None, supervisor, dispatcher, config, "office", runtime_state=reopened)
    dispatcher.set_watchdog(watchdog)
    launch = AsyncMock()
    monkeypatch.setattr(supervisor, "_spawn_worker", launch)
    monkeypatch.setattr(dispatcher, "_fetch_task_status", AsyncMock(return_value="in_progress"))
    await dispatcher.add_task({**ready_task(), "status": "in_progress"})
    assert not await dispatcher.dispatch_agent("engineer")
    launch.assert_not_awaited()
    assert reopened.failure_count("next") == 3


@pytest.mark.parametrize("interruption", ["capacity", "stop"])
async def test_claim_response_cannot_override_intervening_admission_state(runtime, monkeypatch, interruption):
    supervisor = AgentSupervisor(".", "office", max_agents=1)
    supervisor.set_runtime_state(runtime)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def claim(agent_name, task_data, attempt_id):
        entered.set()
        await release.wait()
        return {
            "attempt_id": attempt_id, "execution_cycle": 2, "execution_generation": 5,
            "expected_assigned_agent": agent_name,
        }

    supervisor.set_execution_claimer(claim)
    launch = AsyncMock(side_effect=AssertionError("No process may launch after admission closes"))
    monkeypatch.setattr("src.orchestrator.agent_supervisor.asyncio.create_subprocess_exec", launch)
    spawning = asyncio.create_task(supervisor.spawn_worker("engineer", {}, ready_task()))
    await entered.wait()
    if interruption == "capacity":
        supervisor._agents["other"] = AgentProcess("other", "worker", state=AgentState.WORKING)
    else:
        supervisor.suppress_task("next")
    release.set()
    assert not await spawning
    launch.assert_not_awaited()
    assert "engineer" not in supervisor._agents
    assert runtime.maintenance_status()["pending_admissions"] == 0


@pytest.mark.parametrize("conflict", ["owner", "task", "payload"])
def test_conflicting_completion_cannot_replace_or_acknowledge_original(runtime, conflict):
    original = outcome()
    runtime.retain_completion("engineer", "attempt", "previous", original)
    reopened = RuntimeState(runtime.database_path, "office")
    agent_name = "another-engineer" if conflict == "owner" else "engineer"
    task_id = "another-task" if conflict == "task" else "previous"
    changed = {**original, "task_id": task_id}
    if conflict == "payload":
        changed["status"] = "done"
    with pytest.raises(RuntimeError, match="conflicts"):
        reopened.retain_completion(agent_name, "attempt", task_id, changed)
    assert reopened.pending_completions()[0]["payload"] == original
    reopened.retain_completion("engineer", "attempt", "previous", dict(reversed(list(original.items()))))
    assert len(reopened.pending_completions()) == 1


def test_script_receipts_retry_exact_identity_without_allowing_replacement(runtime):
    runtime.begin_script_invocation("invocation", "fingerprint")
    runtime.finish_script_invocation("invocation", "fingerprint", "execution")
    runtime.note_script_owner("execution", "attempt")
    reopened = RuntimeState(runtime.database_path, "office")
    reopened.finish_script_invocation("invocation", "fingerprint", "execution")
    reopened.note_script_owner("execution", "attempt")
    with pytest.raises(RuntimeError, match="another execution"):
        reopened.finish_script_invocation("invocation", "fingerprint", "different-execution")
    with pytest.raises(RuntimeError, match="another worker"):
        reopened.note_script_owner("execution", "different-attempt")
    assert reopened.begin_script_invocation("invocation", "fingerprint")["execution_id"] == "execution"


def test_reopen_and_new_cycle_do_not_prune_unresolved_or_idempotency_receipts(runtime):
    runtime.observe_cycle("previous", 2)
    reservation = runtime.reserve("generation", "previous")
    runtime.retain_completion("engineer", "attempt", "previous", outcome())
    runtime.begin_script_invocation("pending-invocation", "fingerprint")
    runtime.begin_script_invocation("confirmed-invocation", "confirmed-fingerprint")
    runtime.finish_script_invocation("confirmed-invocation", "confirmed-fingerprint", "execution")
    runtime.note_script("previous", "unresolved-execution", "running")
    reopened = RuntimeState(runtime.database_path, "office")
    reopened.observe_cycle("previous", 3)
    reopened.set_maintenance(True)
    reopened.snapshot(0, 0)
    assert reopened.owns_reservation(reservation, kind="generation")
    assert reopened.maintenance_status()["state"] == "reconciliation_required"
    assert reopened.has_pending_completion("previous")
    assert reopened.begin_script_invocation("pending-invocation", "fingerprint")["state"] == "pending"
    assert reopened.begin_script_invocation("confirmed-invocation", "confirmed-fingerprint")["execution_id"] == "execution"
    assert reopened.unresolved_scripts()[0]["cycle"] == 2


@pytest.mark.parametrize("failure_stage", ["spawn", "handshake", "completion"])
async def test_actual_spawn_credential_lifecycle_revokes_before_release(runtime, monkeypatch, failure_stage):
    registry = ProxySessionRegistry()
    callback = AsyncMock(side_effect=RuntimeError("completion delivery unavailable"))
    supervisor = AgentSupervisor(".", "office", container_name="isolated-office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    supervisor.set_tool_proxy("http://host-proxy", "host-only", "host-collections", sessions=registry)
    process = MagicMock()
    process.returncode = None
    process.pid = 12345
    process.wait = AsyncMock(return_value=0)
    process.stdin.drain = AsyncMock()
    captured = {}

    async def launch(*args, **kwargs):
        captured.update(kwargs["env"])
        assert len(registry._sessions) == 2
        assert captured["CUBICLE_TOOL_PROXY_TOKEN"] != "host-only"
        assert captured["CUBICLE_COLLECTIONS_TOKEN"] != "host-collections"
        assert registry.resolve(captured["CUBICLE_TOOL_PROXY_TOKEN"]) is None
        if failure_stage == "spawn":
            raise OSError("process creation failed")
        return process

    async def cleanup(*args):
        assert registry.resolve(captured["CUBICLE_TOOL_PROXY_TOKEN"]) is None
        assert registry.resolve(captured["CUBICLE_COLLECTIONS_TOKEN"], collections=True) is None

    monkeypatch.setattr("src.orchestrator.agent_supervisor.asyncio.create_subprocess_exec", launch)
    monkeypatch.setattr("src.docker.task_process_cleanup.terminate_worker_execution", cleanup)
    for method in ("_reader_loop", "_monitor_exit", "_heartbeat_loop", "_send_to_agent"):
        monkeypatch.setattr(supervisor, method, AsyncMock())
    ready = AsyncMock(side_effect=RuntimeError("handshake failed") if failure_stage == "handshake" else None)
    monkeypatch.setattr(supervisor, "_wait_for_ready", ready)
    spawned = await supervisor.spawn_worker(
        "engineer", {}, {**ready_task(), "execution_cycle": 2, "execution_generation": 5},
    )
    if failure_stage == "completion":
        assert spawned
        session = registry.resolve(captured["CUBICLE_TOOL_PROXY_TOKEN"])
        assert session is not None
        assert session.caller["task_id"] == "next"
        assert session.caller["role"] == "worker"
        agent = supervisor._agents["engineer"]
        await supervisor._complete_worker(agent, {"type": "task_complete", "task_id": "next", "status": "review"})
        assert runtime.has_pending_completion("next")
        assert supervisor.is_agent_busy("engineer")
    else:
        assert not spawned
        # Fatal bootstrap outcomes now use the same durable finalization
        # outbox; callback outage must not release the worker's owner slot.
        assert runtime.has_pending_completion("next")
        assert runtime.pending_completions()[0]["payload"]["fatal"] is True
        assert supervisor.is_agent_busy("engineer")
        if failure_stage == "handshake":
            assert supervisor._agents["engineer"].current_task_id is None
        assert not await supervisor.stop_task("engineer", "unrelated-task")
        assert await supervisor.stop_task("engineer", "next")
        await supervisor.retry_pending_cleanup()
        callback.assert_awaited_once()
        assert not runtime.has_pending_completion("next")
        assert not supervisor.is_agent_busy("engineer")
    assert not registry._sessions
    assert runtime.maintenance_status()["pending_admissions"] == 0
