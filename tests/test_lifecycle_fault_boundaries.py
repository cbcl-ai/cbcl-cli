"""Fault composition across real supervisor, durable runtime, and dispatch."""

import asyncio
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest

from src.config_sync.sync_service import ConfigStore
from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.orchestrator.task_dispatcher import TaskDispatcher
from src.review_completion import reconcile_review_completion
from src.runtime_state import RuntimeState


def review_task():
    return {
        "id": "review-task", "task_id": "review-task", "status": "review",
        "assigned_agent": "engineer", "reviewer": "auditor",
        "execution_cycle": 2, "execution_generation": 3, "review_retry_epoch": 0,
        "priority": "medium", "readable_id": "TEST.T1",
    }


async def test_failed_fatal_review_callback_survives_restart_without_another_attempt(tmp_path, monkeypatch):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    task = review_task()
    runtime.observe_cycle(task["id"], 2)
    for number in (1, 2):
        runtime.record_review_attempt(task["id"], 2, "auditor", str(uuid.UUID(int=number)))
    outage = True
    hold_requests = []
    original_client = httpx.AsyncClient

    def transport(request):
        if request.method == "POST" and request.url.path.endswith("/review-hold"):
            hold_requests.append(request)
            return httpx.Response(503 if outage else 200, json={
                "status": "pending", "action_request_id": "review-hold", "review_retry_epoch": 0,
            })
        if request.url.path.endswith("/action-requests"):
            return httpx.Response(200, json={"items": [], "total": 0})
        if request.url.path.endswith("/tasks/review-task"):
            return httpx.Response(200, json=task)
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(transport), **kw))

    def callback_for(state):
        async def callback(agent_name, event):
            return await reconcile_review_completion(
                task, event, agent_name, runtime_state=state,
                platform_url="http://platform", office_id="office", security_token="synthetic",
            )
        return callback

    old = AgentSupervisor(".", "office", on_event=callback_for(runtime))
    old.set_runtime_state(runtime)
    old.set_failure_observer(MagicMock())
    agent = AgentProcess(
        "auditor", "worker", state=AgentState.CRASHED,
        current_task_id=task["id"], execution_task_id=task["id"],
        execution_attempt_id=str(uuid.UUID(int=3)), execution_cycle=2,
        execution_generation=3, execution_assignee="engineer", execution_mode="review",
    )
    old._agents["auditor"] = agent
    await old._report_failure(agent, {
        "type": "error", "fatal": True, "task_id": task["id"], "error_class": "worker_process_failure",
    })
    assert old.is_agent_busy("auditor")
    assert runtime.review_state(task["id"], 2, "auditor")["failures"] == 3
    assert len(hold_requests) == 1

    # Reconstruct all process-local owners, as a daemon restart does. Only the
    # SQLite receipt may prevent the fourth review and redeliver finalization.
    reopened = RuntimeState(runtime.database_path, "office")
    supervisor = AgentSupervisor(".", "office", on_event=callback_for(reopened))
    supervisor.set_runtime_state(reopened)
    config = ConfigStore()
    config.agents = [{"name": "auditor", "is_active": True}]
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        queue = AgentQueueManager(redis, "office")
        dispatcher = TaskDispatcher(redis, "office", supervisor, config, queue, backend_url="http://platform")
        dispatcher.set_runtime_state(reopened)
        launch = AsyncMock(return_value=True)
        monkeypatch.setattr(supervisor, "_spawn_worker", launch)
        await dispatcher.add_task(task)
        assert not await dispatcher.dispatch_agent("auditor"), "Lost fatal receipt admits a fourth reviewer"
        launch.assert_not_awaited()
        outage = False
        await supervisor.retry_pending_cleanup()
        assert reopened.review_state(task["id"], 2, "auditor")["request_id"] == "review-hold"
        assert reopened.review_state(task["id"], 2, "auditor")["failures"] == 3
        assert not reopened.pending_completions()
        await queue.reconcile([task])
        assert not await dispatcher.dispatch_agent("auditor")
        launch.assert_not_awaited()
        assert len(hold_requests) == 2
    finally:
        await redis.aclose()


async def test_quota_pause_during_claim_prevents_process_launch(tmp_path, monkeypatch):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    supervisor = AgentSupervisor(".", "office")
    supervisor.set_runtime_state(runtime)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def claim(agent_name, task, attempt_id):
        entered.set()
        await release.wait()
        return {"attempt_id": attempt_id, "execution_cycle": 1,
                "execution_generation": 1, "expected_assigned_agent": agent_name}

    supervisor.set_execution_claimer(claim)
    launch = AsyncMock(side_effect=AssertionError("A quota-paused office must not launch another CLI worker"))
    monkeypatch.setattr("src.orchestrator.agent_supervisor.asyncio.create_subprocess_exec", launch)
    spawning = asyncio.create_task(supervisor.spawn_worker("engineer", {}, {"task_id": "task", "status": "in_progress"}))
    await entered.wait()
    runtime.pause_for_quota("Synthetic quota reached", "synthetic-model")
    release.set()
    assert not await spawning
    launch.assert_not_awaited()
    assert "engineer" not in supervisor._agents
    assert runtime.maintenance_status()["pending_admissions"] == 0


@pytest.mark.parametrize("stage", ["container_prepare", "ready_handshake"])
async def test_quota_pause_before_assignment_cleans_bootstrap_without_running_work(tmp_path, monkeypatch, stage):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    supervisor = AgentSupervisor(".", "office")
    supervisor.set_runtime_state(runtime)
    process = MagicMock()
    process.pid = 123
    process.returncode = None
    process.wait = AsyncMock(return_value=0)
    cleanup = AsyncMock()
    assign = AsyncMock()
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr("src.orchestrator.agent_supervisor.asyncio.create_subprocess_exec", launch)
    monkeypatch.setattr("src.docker.task_process_cleanup.terminate_worker_execution", cleanup)
    monkeypatch.setattr(supervisor, "_reader_loop", AsyncMock())
    monkeypatch.setattr(supervisor, "_send_to_agent", assign)

    async def pause(*args):
        runtime.pause_for_quota("Synthetic quota pause", "synthetic-model")
        return SimpleNamespace(container_id="synthetic-attempt-container")

    if stage == "container_prepare":
        containers = MagicMock()
        containers.available = AsyncMock(return_value=True)
        containers.task_available = AsyncMock(return_value=True)
        containers.prepare = AsyncMock(side_effect=pause)
        containers.stop_attempt = cleanup
        supervisor.set_execution_containers(containers)
    else:
        monkeypatch.setattr(supervisor, "_wait_for_ready", AsyncMock(side_effect=pause))
    assert not await supervisor.spawn_worker("engineer", {}, {"task_id": "task", "status": "in_progress"})
    assign.assert_not_awaited()
    cleanup.assert_awaited_once()
    if stage == "container_prepare":
        launch.assert_not_awaited()
    assert not supervisor.is_agent_busy("engineer")
    assert not runtime.pending_completions()
    assert runtime.failure_count("task") == 0


@pytest.mark.parametrize("managed", [False, True])
async def test_fatal_receipt_precedes_cleanup_and_restart_requires_death_proof(tmp_path, monkeypatch, managed):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    callback = AsyncMock()
    supervisor = AgentSupervisor(".", "office", container_name="synthetic-office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", "raise SystemExit(23)",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, env={},
    )
    agent = AgentProcess(
        "auditor", "worker", state=AgentState.WORKING,
        current_task_id="review-task", execution_task_id="review-task",
        execution_marker="exact-dead-attempt-marker", execution_container_managed=managed,
        execution_attempt_id=str(uuid.UUID(int=3)), execution_cycle=2,
        execution_generation=3, execution_assignee="engineer", execution_mode="review",
        process=process, pid=process.pid,
    )
    supervisor._agents["auditor"] = agent

    async def unconfirmed(*args):
        receipt = runtime.pending_completions()[0]
        assert receipt["payload"]["fatal"] is True
        assert receipt["execution_marker"] == "exact-dead-attempt-marker"
        assert bool(receipt["container_managed"]) is managed
        raise RuntimeError("Process cleanup not confirmed")

    cleanup = AsyncMock(side_effect=unconfirmed)
    monkeypatch.setattr("src.docker.task_process_cleanup.terminate_worker_execution", cleanup)
    containers = MagicMock()
    containers.stop_attempt = cleanup
    supervisor.set_execution_containers(containers)
    await supervisor._monitor_exit("auditor", agent)
    assert process.returncode == 23
    callback.assert_not_awaited()
    assert supervisor.is_agent_busy("auditor")

    reopened = RuntimeState(runtime.database_path, "office")
    restored = AgentSupervisor(".", "office", container_name="synthetic-office", on_event=callback)
    restored.set_runtime_state(reopened)
    restored.set_execution_containers(containers)
    await restored.retry_pending_cleanup()
    callback.assert_not_awaited()
    assert restored.is_agent_busy("auditor")
    assert reopened.has_pending_completion("review-task")
    cleanup.side_effect = None
    await restored.retry_pending_cleanup()
    if managed:
        cleanup.assert_awaited_with("review-task", str(uuid.UUID(int=3)))
    else:
        cleanup.assert_awaited_with("synthetic-office", "exact-dead-attempt-marker")
    callback.assert_awaited_once()
    assert callback.await_args.args[1]["type"] == "error"
    assert callback.await_args.args[1]["fatal"] is True
    assert not restored.is_agent_busy("auditor")
    assert not reopened.pending_completions()
    with reopened._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM worker_completion_cleanup").fetchone()[0] == 0


async def test_fatal_storage_failure_prevents_cleanup_and_owner_release(tmp_path, monkeypatch):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    callback = AsyncMock()
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    agent = AgentProcess(
        "engineer", "worker", current_task_id="task", execution_task_id="task",
        execution_cycle=1, execution_generation=1, execution_mode="execute",
        execution_marker="marker", exit_code=1,
    )
    supervisor._agents["engineer"] = agent
    cleanup = AsyncMock()
    monkeypatch.setattr("src.docker.task_process_cleanup.terminate_worker_execution", cleanup)
    retain = runtime.retain_completion
    monkeypatch.setattr(runtime, "retain_completion", MagicMock(side_effect=OSError("SQLite write failed")))
    with pytest.raises(OSError, match="SQLite write failed"):
        await supervisor._monitor_exit("engineer", agent)
    cleanup.assert_not_awaited()
    callback.assert_not_awaited()
    assert supervisor.is_agent_busy("engineer")
    await supervisor.retry_pending_cleanup()
    cleanup.assert_not_awaited()
    callback.assert_not_awaited()
    assert supervisor.is_agent_busy("engineer")
    monkeypatch.setattr(runtime, "retain_completion", retain)
    await supervisor.retry_pending_cleanup()
    cleanup.assert_awaited_once()
    callback.assert_awaited_once()
    assert not runtime.pending_completions()


async def test_review_process_launch_failures_reach_hold_before_fourth_launch(tmp_path, monkeypatch):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    task = review_task()
    task["execution_generation"] = 0
    hold_requests = []
    original_client = httpx.AsyncClient

    def transport(request):
        if request.method == "POST":
            assert request.url.path.endswith("/review-hold")
            hold_requests.append(request)
            return httpx.Response(200, json={"status": "pending", "action_request_id": "hold", "review_retry_epoch": 0})
        if request.url.path.endswith("/action-requests"):
            return httpx.Response(200, json={"items": [], "total": 0})
        return httpx.Response(200, json=task)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(transport), **kw))

    async def callback(agent_name, event):
        await reconcile_review_completion(
            task, {**event, "error_class": "worker_process_failure"}, agent_name,
            runtime_state=runtime, platform_url="http://platform", office_id="office", security_token="synthetic",
        )

    async def claim(agent_name, queued_task, attempt_id):
        task["execution_generation"] += 1
        return {"attempt_id": attempt_id, "execution_cycle": 2,
                "execution_generation": task["execution_generation"], "expected_assigned_agent": "engineer"}

    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    supervisor.set_failure_observer(MagicMock())
    supervisor.set_execution_claimer(claim)
    launch = AsyncMock(side_effect=OSError("Synthetic process launch failed"))
    monkeypatch.setattr("src.orchestrator.agent_supervisor.asyncio.create_subprocess_exec", launch)
    config = ConfigStore()
    config.agents = [{"name": "auditor", "is_active": True}]
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        queue = AgentQueueManager(redis, "office")
        dispatcher = TaskDispatcher(redis, "office", supervisor, config, queue, backend_url="http://platform")
        dispatcher.set_runtime_state(runtime)
        for _ in range(4):
            await queue.reconcile([task])
            assert not await dispatcher.dispatch_agent("auditor")
        assert launch.await_count == 3
        assert len(hold_requests) == 1
        assert runtime.review_state("review-task", 2, "auditor")["failures"] == 3
        assert runtime.review_state("review-task", 2, "auditor")["request_id"] == "hold"
    finally:
        await redis.aclose()


@pytest.mark.parametrize("first", ["completion", "failure"])
async def test_first_retained_outcome_wins_late_conflicting_event(tmp_path, first):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    callback = AsyncMock(side_effect=ConnectionError("Synthetic callback outage"))
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    agent = AgentProcess(
        "engineer", "worker", state=AgentState.WORKING,
        current_task_id="task", execution_task_id="task", execution_cycle=1,
        execution_generation=1, execution_mode="execute", execution_assignee="engineer",
    )
    supervisor._agents["engineer"] = agent
    completion = {"type": "task_complete", "task_id": "task", "status": "review"}
    failure = {"type": "error", "fatal": True, "task_id": "task", "reason": "late_failure"}
    if first == "completion":
        await supervisor._complete_worker(agent, completion)
        await supervisor._report_failure(agent, failure)
        assert agent.pending_failure is None
    else:
        await supervisor._report_failure(agent, failure)
        await supervisor._complete_worker(agent, completion)
        assert agent.pending_completion is None
    expected = "task_complete" if first == "completion" else "error"
    assert runtime.pending_completions()[0]["payload"]["type"] == expected
    assert callback.await_count == 1
    callback.side_effect = None
    await supervisor.retry_pending_cleanup()
    assert callback.await_count == 2
    assert callback.await_args.args[1]["type"] == expected
    assert not supervisor.is_agent_busy("engineer")
    assert not runtime.pending_completions()
    # Once acknowledged, a late conflicting event cannot invent a new outcome.
    if first == "completion":
        await supervisor._report_failure(agent, failure)
    else:
        await supervisor._complete_worker(agent, completion)
    assert callback.await_count == 2
    assert not runtime.pending_completions()


@pytest.mark.parametrize("attempt", ["", None])
async def test_unidentified_worker_failure_retains_owner_without_callback(tmp_path, attempt):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    callback = AsyncMock()
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    agent = AgentProcess(
        "engineer", "worker", execution_task_id="task", current_task_id="task",
        execution_cycle=1, execution_generation=1, execution_attempt_id=attempt,
    )
    supervisor._agents["engineer"] = agent
    with pytest.raises(ValueError, match="Completion identity"):
        await supervisor._report_failure(agent, {"type": "error", "fatal": True, "task_id": "task"})
    callback.assert_not_awaited()
    assert supervisor.is_agent_busy("engineer")
    assert not runtime.pending_completions()


@pytest.mark.parametrize("task_id", ["planner-consult", "flow-consult-research"])
async def test_synthetic_consult_failure_is_not_a_board_worker_receipt(tmp_path, task_id):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    callback = AsyncMock()
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    agent = AgentProcess("consultant", "worker", execution_task_id=task_id, current_task_id=task_id)
    supervisor._agents["consultant"] = agent
    await supervisor._report_failure(agent, {"type": "error", "fatal": True, "task_id": task_id})
    callback.assert_awaited_once()
    assert not runtime.pending_completions()


async def test_confirmed_stop_can_discard_fatal_callback_retained_after_observed_exit(tmp_path):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    callback = AsyncMock(side_effect=ConnectionError("Callback service unavailable"))
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    agent = AgentProcess(
        "engineer", "worker", state=AgentState.WORKING, exit_code=23,
        current_task_id="task", execution_task_id="task", execution_cycle=1,
        execution_generation=1, execution_mode="execute", execution_assignee="engineer",
    )
    supervisor._agents["engineer"] = agent
    await supervisor._monitor_exit("engineer", agent)
    assert runtime.has_pending_completion("task")
    assert supervisor.is_agent_busy("engineer")
    assert not await supervisor.stop_task("engineer", "unrelated-task")
    assert await supervisor.stop_task("engineer", "task")
    await supervisor.retry_pending_cleanup()
    callback.assert_awaited_once()
    assert not supervisor.is_agent_busy("engineer")
    assert not runtime.pending_completions()
