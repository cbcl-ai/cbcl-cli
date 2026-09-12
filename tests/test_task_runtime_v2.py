"""Synthetic regression coverage for task execution ownership and cleanup."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from src._handlers._tasks import route_task_kill, route_task_moved, route_task_updated
from src.docker import session_bridge, task_process_cleanup
from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.orchestrator.task_dispatcher import TaskDispatcher


def worker_record(task_id="old-task"):
    process = MagicMock()
    process.returncode = None
    process.wait = AsyncMock(return_value=0)
    return AgentProcess(
        agent_name="engineer",
        role="worker",
        state=AgentState.WORKING,
        current_task_id=task_id,
        execution_task_id=task_id,
        execution_mode="execute",
        execution_marker="a" * 64,
        process=process,
    )


def supervisor_record(callback=None):
    return AgentSupervisor(
        workspace_path=".",
        office_id="office",
        container_name="immutable-container",
        on_event=callback,
    )


@pytest.mark.parametrize("status", ["done", "archived"])
@pytest.mark.parametrize("handler", [route_task_updated, route_task_moved])
async def test_terminal_event_never_claims_idle_when_container_cleanup_fails(
    monkeypatch, status, handler
):
    supervisor = supervisor_record()
    worker = worker_record()
    supervisor._agents["engineer"] = worker
    queue = AsyncMock()
    router = AsyncMock()
    monkeypatch.setattr(
        task_process_cleanup,
        "terminate_worker_execution",
        AsyncMock(side_effect=RuntimeError("Docker unavailable")),
    )
    await handler(
        {"task_id": "old-task", "new_status": status, "task_data": {"status": status}},
        queue_manager=queue,
        dispatcher=MagicMock(),
        supervisor=supervisor,
        router=router,
    )
    queue.clear_active.assert_not_awaited()
    router.publish_event.assert_not_awaited()
    assert supervisor.is_agent_busy("engineer")
    assert "old-task" in supervisor._suppressed_tasks


@pytest.mark.parametrize("status", ["review", "blocked"])
async def test_handoff_waits_for_executor_cleanup(monkeypatch, status):
    supervisor = supervisor_record()
    supervisor._agents["engineer"] = worker_record()
    queue = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.dispatch_agent = AsyncMock()
    monkeypatch.setattr(
        task_process_cleanup,
        "terminate_worker_execution",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    await route_task_moved(
        {
            "task_id": "old-task",
            "new_status": status,
            "assigned_agent": "engineer",
            "reviewer": "reviewer",
        },
        queue_manager=queue,
        supervisor=supervisor,
        dispatcher=dispatcher,
        router=AsyncMock(),
    )
    queue.clear_active.assert_not_awaited()
    queue.add_task.assert_not_awaited()
    dispatcher.dispatch_agent.assert_not_awaited()
    assert supervisor.is_agent_busy("engineer")


@pytest.mark.parametrize("mode", ["review", "triage"])
async def test_executor_handoff_cannot_kill_successor_phase(mode):
    supervisor = supervisor_record()
    worker = worker_record()
    worker.execution_mode = mode
    supervisor._agents["engineer"] = worker
    assert not await supervisor.stop_task(
        "engineer", "old-task", expected_mode="execute"
    )
    worker.process.terminate.assert_not_called()


async def test_stale_handoff_cannot_stop_resumed_execution(monkeypatch):
    import httpx

    supervisor = supervisor_record()
    worker = worker_record()
    supervisor._agents["engineer"] = worker
    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "in_progress"}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = response
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    queue = AsyncMock()
    await route_task_moved(
        {"task_id": "old-task", "new_status": "blocked", "assigned_agent": "engineer"},
        queue_manager=queue,
        supervisor=supervisor,
        dispatcher=MagicMock(),
        router=AsyncMock(),
        platform_url="http://unused",
        office_id="office",
    )
    worker.process.terminate.assert_not_called()
    queue.clear_active.assert_not_awaited()


async def test_cancelled_spawn_waits_for_host_process_then_cleans_container(
    monkeypatch,
):
    supervisor = supervisor_record()
    started = asyncio.Event()
    release = asyncio.Event()
    process = MagicMock()
    process.returncode = None
    process.wait = AsyncMock(return_value=0)

    async def create_process(*args, **kwargs):
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    spawning = asyncio.create_task(
        supervisor.spawn_worker("engineer", {}, {"task_id": "old-task"})
    )
    await started.wait()
    spawning.cancel()
    await asyncio.sleep(0)
    assert supervisor.is_agent_busy("engineer")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await spawning
    process.terminate.assert_called_once()
    cleanup.assert_awaited_once()
    assert not supervisor.is_agent_busy("engineer")


async def test_correlated_stop_reports_unconfirmed_then_confirmed(monkeypatch):
    supervisor = supervisor_record()
    supervisor._agents["engineer"] = worker_record()
    queue = AsyncMock()
    queue.get_active.return_value = {"task_id": "old-task"}
    router = AsyncMock()
    cleanup = AsyncMock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    command = {"task_id": "old-task", "all_agents": True, "stop_request_id": "request"}
    await route_task_kill(
        command,
        queue_manager=queue,
        supervisor=supervisor,
        dispatcher=MagicMock(),
        router=router,
    )
    assert router.publish_event.await_args.args[0]["status"] == "unconfirmed"
    cleanup.side_effect = None
    await route_task_kill(
        command,
        queue_manager=queue,
        supervisor=supervisor,
        dispatcher=MagicMock(),
        router=router,
    )
    result = router.publish_event.await_args.args[0]
    assert result["status"] == "stopped"
    assert result["stop_request_id"] == "request"
    assert result["stopped_agents"] == ["engineer"]
    queue.clear_active.assert_awaited_once_with("engineer", "old-task")


async def test_legacy_uncorrelated_kill_cannot_suppress_live_review():
    supervisor = supervisor_record()
    worker = worker_record()
    worker.execution_mode = "review"
    supervisor._agents["engineer"] = worker
    queue = AsyncMock()
    await route_task_kill(
        {"task_id": "old-task", "agent_name": "engineer"},
        queue_manager=queue, supervisor=supervisor, dispatcher=MagicMock(),
        router=AsyncMock(),
    )
    worker.process.terminate.assert_not_called()
    queue.remove_task.assert_not_awaited()
    assert "old-task" not in supervisor._suppressed_tasks


async def test_stale_reader_cannot_finish_successor():
    callback = AsyncMock()
    supervisor = supervisor_record(callback)
    old = worker_record()
    successor = worker_record("new-task")
    supervisor._agents["engineer"] = successor
    stdout = asyncio.StreamReader()
    stdout.feed_data(
        json.dumps({"type": "task_complete", "task_id": "old-task"}).encode() + b"\n"
    )
    stdout.feed_eof()
    await supervisor._reader_loop("engineer", stdout, old)
    assert successor.current_task_id == "new-task"
    assert successor.state == AgentState.WORKING
    callback.assert_not_awaited()


async def test_completion_waits_for_container_cleanup(monkeypatch):
    callback = AsyncMock()
    supervisor = supervisor_record(callback)
    worker = worker_record()
    supervisor._agents["engineer"] = worker
    cleanup_started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup(*args):
        cleanup_started.set()
        await release.wait()

    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    stdout = asyncio.StreamReader()
    stdout.feed_data(b'{"type":"task_complete","task_id":"old-task"}\n')
    stdout.feed_eof()
    reading = asyncio.create_task(supervisor._reader_loop("engineer", stdout, worker))
    await cleanup_started.wait()
    assert supervisor.is_agent_busy("engineer")
    callback.assert_not_awaited()
    release.set()
    await reading
    callback.assert_awaited_once()
    assert not supervisor.is_agent_busy("engineer")


async def test_natural_crash_cannot_release_unconfirmed_container_execution(
    monkeypatch,
):
    callback = AsyncMock()
    supervisor = supervisor_record(callback)
    worker = worker_record()
    worker.process.wait.return_value = 1
    supervisor._agents["engineer"] = worker
    monkeypatch.setattr(
        task_process_cleanup,
        "terminate_worker_execution",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    await supervisor._monitor_exit("engineer", worker)
    assert supervisor.is_agent_busy("engineer")
    assert supervisor.reconcile_stuck_agents() == []
    callback.assert_not_awaited()


async def test_two_agents_cannot_admit_same_task_even_during_spawn(monkeypatch):
    supervisor = supervisor_record()
    worker = worker_record()
    worker.state = AgentState.SPAWNING
    supervisor._agents["engineer"] = worker
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert not await supervisor.spawn_worker(
        "other-engineer", {}, {"task_id": "old-task"}
    )
    spawn.assert_not_awaited()


async def test_terminal_event_during_spawn_prevents_assignment(monkeypatch):
    supervisor = supervisor_record()
    process = MagicMock()
    process.returncode = None
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    ready = asyncio.Event()
    release = asyncio.Event()

    async def wait_ready(*args):
        ready.set()
        await release.wait()

    monkeypatch.setattr(supervisor, "_wait_for_ready", wait_ready)
    monkeypatch.setattr(supervisor, "_reader_loop", AsyncMock())
    sender = AsyncMock()
    monkeypatch.setattr(supervisor, "_send_to_agent", sender)
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    spawning = asyncio.create_task(
        supervisor.spawn_worker("engineer", {}, {"task_id": "old-task"})
    )
    await ready.wait()
    supervisor.suppress_task("old-task")
    release.set()
    assert not await spawning
    sender.assert_not_awaited()
    cleanup.assert_awaited_once()
    assert not supervisor.is_agent_busy("engineer")


async def test_active_clear_compares_task_atomically():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        queue = AgentQueueManager(redis, "office")
        await queue.set_active(
            "engineer", "new-task", "NEW", "in_progress", "execute", 1
        )
        await queue.clear_active("engineer", "old-task")
        assert (await queue.get_active("engineer"))["task_id"] == "new-task"
        await queue.clear_active("engineer", "new-task")
        assert await queue.get_active("engineer") is None
    finally:
        await redis.aclose()


async def test_shutdown_preserves_unconfirmed_worker_record(monkeypatch):
    supervisor = supervisor_record()
    worker = worker_record()
    worker.process.returncode = 0
    supervisor._agents["engineer"] = worker
    monkeypatch.setattr(
        task_process_cleanup,
        "terminate_worker_execution",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    with pytest.raises(RuntimeError, match="unconfirmed worker executions"):
        await supervisor.shutdown(timeout=0)
    assert supervisor._agents["engineer"] is worker
    assert worker.cleanup_pending


async def test_reconcile_retries_requested_cleanup_without_replaying_work(monkeypatch):
    supervisor = supervisor_record()
    worker = worker_record()
    supervisor._agents["engineer"] = worker
    cleanup = AsyncMock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    with pytest.raises(RuntimeError):
        await supervisor.stop_task("engineer", "old-task")
    assert worker.cleanup_pending
    cleanup.side_effect = None
    await supervisor.retry_pending_cleanup()
    assert not worker.cleanup_pending
    assert not supervisor.is_agent_busy("engineer")
    assert cleanup.await_count == 2


async def test_reconcile_does_not_erase_ambiguous_completion(monkeypatch):
    supervisor = supervisor_record()
    worker = worker_record()
    worker.cleanup_pending = True
    supervisor._agents["engineer"] = worker
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    await supervisor.retry_pending_cleanup()
    cleanup.assert_not_awaited()
    assert supervisor.is_agent_busy("engineer")


async def test_reconcile_preserves_and_delivers_completion_after_cleanup_recovers(monkeypatch):
    callback = AsyncMock()
    supervisor = supervisor_record(callback)
    worker = worker_record()
    supervisor._agents["engineer"] = worker
    cleanup = AsyncMock(side_effect=RuntimeError("Docker unavailable"))
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    completion = {
        "type": "task_complete", "task_id": "old-task", "status": "review",
        "comment": "Implementation ready", "token_cost": 0.5,
    }
    stdout = asyncio.StreamReader()
    stdout.feed_data(json.dumps(completion).encode() + b"\n")
    stdout.feed_eof()
    await supervisor._reader_loop("engineer", stdout, worker)
    assert worker.pending_completion == completion
    assert supervisor.is_agent_busy("engineer")
    callback.assert_not_awaited()
    cleanup.side_effect = None
    await asyncio.gather(
        supervisor.retry_pending_cleanup(), supervisor.retry_pending_cleanup(),
    )
    callback.assert_awaited_once_with("engineer", completion)
    assert worker.pending_completion is None
    assert worker.completion_delivered
    assert not supervisor.is_agent_busy("engineer")
    assert cleanup.await_count == 2
    worker.process.terminate.assert_not_called()


async def test_duplicate_completion_never_replays_board_or_flow_callback(monkeypatch):
    callback = AsyncMock()
    supervisor = supervisor_record(callback)
    worker = worker_record()
    supervisor._agents["engineer"] = worker
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    stdout = asyncio.StreamReader()
    completion = b'{"type":"task_complete","task_id":"old-task","status":"review"}\n'
    stdout.feed_data(completion + completion)
    stdout.feed_eof()
    await supervisor._reader_loop("engineer", stdout, worker)
    callback.assert_awaited_once()
    cleanup.assert_awaited_once()


async def test_observed_crash_retries_cleanup_then_reports_crash_once(monkeypatch):
    callback = AsyncMock()
    supervisor = supervisor_record(callback)
    worker = worker_record()
    worker.process.wait.return_value = 1
    supervisor._agents["engineer"] = worker
    cleanup = AsyncMock(side_effect=RuntimeError("Docker unavailable"))
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    await supervisor._monitor_exit("engineer", worker)
    assert supervisor.is_agent_busy("engineer")
    callback.assert_not_awaited()
    cleanup.side_effect = None
    await asyncio.gather(
        supervisor.retry_pending_cleanup(), supervisor.retry_pending_cleanup(),
    )
    await supervisor.retry_pending_cleanup()
    callback.assert_awaited_once()
    assert callback.await_args.args[1]["task_id"] == "old-task"
    assert callback.await_args.args[1]["fatal"] is True
    assert worker.state == AgentState.CRASHED
    assert worker.process is None
    assert not supervisor.is_agent_busy("engineer")


async def test_stop_discards_retained_completion_without_replaying_it(monkeypatch):
    callback = AsyncMock()
    supervisor = supervisor_record(callback)
    worker = worker_record()
    worker.pending_completion = {"type": "task_complete", "task_id": "old-task"}
    worker.cleanup_pending = True
    supervisor._agents["engineer"] = worker
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())
    assert await supervisor.stop_task("engineer", "old-task")
    await supervisor.retry_pending_cleanup()
    callback.assert_not_awaited()
    assert worker.pending_completion is None
    assert not supervisor.is_agent_busy("engineer")


async def test_recovered_completion_callback_can_acquire_agent_stop_lock(monkeypatch):
    supervisor = supervisor_record()
    worker = worker_record()
    worker.pending_completion = {"type": "task_complete", "task_id": "old-task"}
    worker.cleanup_pending = True
    supervisor._agents["engineer"] = worker
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())

    async def callback(agent_name, completion):
        assert not await supervisor.stop_task(agent_name, completion["task_id"])

    supervisor._on_event = AsyncMock(side_effect=callback)
    await asyncio.wait_for(supervisor.retry_pending_cleanup(), timeout=1)
    supervisor._on_event.assert_awaited_once()
    assert not supervisor.is_agent_busy("engineer")


async def test_stop_during_completion_cleanup_suppresses_late_completion(monkeypatch):
    callback = AsyncMock()
    supervisor = supervisor_record(callback)
    worker = worker_record()
    supervisor._agents["engineer"] = worker
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup(*args):
        started.set()
        await release.wait()

    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    completion = asyncio.create_task(supervisor._complete_worker(
        worker, {"type": "task_complete", "task_id": "old-task", "status": "review"},
    ))
    await started.wait()
    stopping = asyncio.create_task(supervisor.stop_task("engineer", "old-task"))
    await asyncio.sleep(0)
    assert worker.stop_requested
    release.set()
    await completion
    assert await stopping
    callback.assert_not_awaited()
    assert worker.pending_completion is None
    assert not supervisor.is_agent_busy("engineer")


async def test_cleanup_recovery_requeues_same_agent_triage_after_stale_active_marker(monkeypatch):
    supervisor = supervisor_record()
    worker = worker_record()
    worker.agent_name = "manager-assistant"
    supervisor._agents["manager-assistant"] = worker
    cleanup = AsyncMock(side_effect=RuntimeError("Docker unavailable"))
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        queue = AgentQueueManager(redis, "office")
        await queue.set_active(
            "manager-assistant", "old-task", "OLD", "in_progress", "execute", 1,
        )
        with pytest.raises(RuntimeError):
            await supervisor.stop_task("manager-assistant", "old-task")
        cleanup.side_effect = None
        dispatcher = TaskDispatcher(
            redis=redis, office_id="office", supervisor=supervisor,
            config_store=MagicMock(), queue_manager=queue,
        )
        dispatcher._fetch_board_tasks = AsyncMock(return_value=[{
            "task_id": "old-task", "status": "blocked", "assigned_agent": "manager-assistant",
        }])
        await dispatcher._reconcile_once()
        assert await queue.get_active("manager-assistant") is None
        assert await queue.get_queue_task_ids("manager-assistant") == {"old-task"}
        assert not supervisor.is_agent_busy("manager-assistant")
    finally:
        await redis.aclose()


@pytest.mark.parametrize("state", ["working", "cleanup_pending", "execution_marker", "current_task"])
async def test_active_reconciliation_never_clears_unconfirmed_execution(state):
    supervisor = supervisor_record()
    worker = AgentProcess(agent_name="engineer", role="worker")
    if state == "working":
        worker.state = AgentState.WORKING
    elif state == "cleanup_pending":
        worker.cleanup_pending = True
    elif state == "execution_marker":
        worker.execution_marker = "a" * 64
    else:
        worker.current_task_id = "old-task"
    supervisor._agents["engineer"] = worker
    queue = AsyncMock()
    queue.get_all_active.return_value = {"engineer": {"task_id": "old-task"}}
    dispatcher = TaskDispatcher(
        redis=MagicMock(), office_id="office", supervisor=supervisor,
        config_store=MagicMock(), queue_manager=queue,
    )
    await dispatcher._clear_stale_active_tasks()
    queue.clear_active.assert_not_awaited()


async def test_active_reconciliation_checks_runtime_after_acquiring_admission_lock():
    supervisor = supervisor_record()
    supervisor._agents["engineer"] = AgentProcess(agent_name="engineer", role="worker")
    queue = AsyncMock()
    queue.get_all_active.return_value = {"engineer": {"task_id": "old-task"}}
    dispatcher = TaskDispatcher(
        redis=MagicMock(), office_id="office", supervisor=supervisor,
        config_store=MagicMock(), queue_manager=queue,
    )
    async with supervisor._get_lock("engineer"):
        reconciling = asyncio.create_task(dispatcher._clear_stale_active_tasks())
        await asyncio.sleep(0)
        queue.clear_active.assert_not_awaited()
        supervisor._agents["engineer"] = worker_record("new-task")
    await reconciling
    queue.clear_active.assert_not_awaited()


async def test_cli_generator_close_waits_for_container_cleanup(monkeypatch):
    process = MagicMock()
    process.returncode = None
    process.stdout.readline = AsyncMock(return_value=b'{"type":"assistant"}\n')
    process.stderr.read = AsyncMock(return_value=b"")
    process.stdin.drain = AsyncMock()
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup unconfirmed"))
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    stream = session_bridge.stream_cli_session(
        container_name="immutable-container",
        model="test",
        system_prompt="",
        prompt="test",
    )
    await anext(stream)
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        await stream.aclose()
    cleanup.assert_awaited_once()
    assert cleanup.await_args.args[0] == "immutable-container"
    assert len(cleanup.await_args.args[1]) == 64


async def test_pickup_checks_owner_without_overwriting_assignment(monkeypatch):
    import httpx

    client = AsyncMock()
    client.__aenter__.return_value = client
    response = MagicMock(status_code=200)
    response.json.return_value = {}
    client.post.return_value = response
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    dispatcher = TaskDispatcher(
        redis=MagicMock(), office_id="office", supervisor=MagicMock(),
        config_store=MagicMock(), queue_manager=MagicMock(), backend_url="http://unused",
    )
    assert await dispatcher._move_and_assign("task", "engineer", "in_progress")
    assert client.post.await_count == 1
    payload = client.post.await_args.kwargs["json"]
    assert payload["action"] == "move_task"
    assert payload["params"]["expected_assigned_agent"] == "engineer"
    assert "assigned_agent" not in payload["params"]


async def test_orphan_claim_requires_current_null_assignment(monkeypatch):
    import httpx

    client = AsyncMock()
    client.__aenter__.return_value = client
    response = MagicMock(status_code=409)
    client.post.return_value = response
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    dispatcher = TaskDispatcher(
        redis=MagicMock(), office_id="office", supervisor=MagicMock(),
        config_store=MagicMock(), queue_manager=MagicMock(), backend_url="http://unused",
    )
    assert not await dispatcher._assign_only("task", "manager-assistant")
    payload = client.post.await_args.kwargs["json"]
    assert payload["params"]["expected_assigned_agent"] is None


async def test_handoff_refetch_cannot_kill_replacement_execution(monkeypatch):
    import httpx

    supervisor = supervisor_record()
    supervisor._agents["engineer"] = worker_record()
    replacement = worker_record()
    replacement.execution_marker = "b" * 64
    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "blocked"}
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def refresh(*args, **kwargs):
        supervisor._agents["engineer"] = replacement
        return response

    client.get.side_effect = refresh
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    queue = AsyncMock()
    queue.get_active.return_value = None
    dispatcher = MagicMock()
    dispatcher.dispatch_agent = AsyncMock()
    await route_task_moved(
        {"task_id": "old-task", "new_status": "blocked", "assigned_agent": "engineer"},
        queue_manager=queue, supervisor=supervisor, dispatcher=dispatcher,
        router=AsyncMock(), platform_url="http://unused", office_id="office",
    )
    replacement.process.terminate.assert_not_called()
    queue.clear_active.assert_not_awaited()
