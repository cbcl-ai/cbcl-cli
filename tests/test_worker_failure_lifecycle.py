"""Failure finalization must preserve process ownership and recovery liveness."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.docker import task_process_cleanup
from src.orchestrator.agent_supervisor import (
    AgentProcess,
    AgentState,
    AgentSupervisor,
    HEARTBEAT_TIMEOUT_SECONDS,
)
from src.orchestrator.task_dispatcher import TaskDispatcher
from src.watchdog import TaskWatchdog


def make_supervisor(callback=None, task_id="task", agent_name="engineer"):
    process = MagicMock()
    process.returncode = None
    process.wait = AsyncMock(return_value=0)
    worker = AgentProcess(
        agent_name=agent_name,
        role="worker",
        state=AgentState.WORKING,
        current_task_id=task_id,
        execution_task_id=task_id,
        execution_mode="execute",
        execution_marker="a" * 64,
        process=process,
        last_pong_at=time.monotonic() - HEARTBEAT_TIMEOUT_SECONDS - 1,
    )
    supervisor = AgentSupervisor(
        workspace_path=".", office_id="office", container_name="office-container",
        on_event=callback,
    )
    supervisor._agents[agent_name] = worker
    return supervisor, worker


@pytest.mark.parametrize("task_id,agent_name", [
    ("task", "engineer"),
    ("planner-consult", "planner"),
    ("flow-consult-request", "flow-architect"),
])
async def test_clean_exit_without_completion_reports_recoverable_failure(
    monkeypatch, task_id, agent_name,
):
    callback = AsyncMock()
    supervisor, worker = make_supervisor(callback, task_id, agent_name)
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    await supervisor._monitor_exit(agent_name, worker)
    callback.assert_awaited_once()
    assert callback.await_args.args[1]["reason"] == "missing_completion"
    assert callback.await_args.args[1]["task_id"] == task_id
    assert callback.await_args.args[1]["fatal"] is True
    assert worker.state == AgentState.CRASHED
    assert worker.current_task_id is None
    assert worker.process is None
    assert not supervisor.is_agent_busy(agent_name)
    cleanup.assert_awaited_once()


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_exit_drains_completion_before_deciding_failure(monkeypatch, exit_code):
    callback = AsyncMock()
    supervisor, worker = make_supervisor(callback)
    worker.process.wait.return_value = exit_code
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())

    async def finish():
        await asyncio.sleep(0)
        await supervisor._complete_worker(worker, {
            "type": "task_complete", "task_id": "task", "status": "review",
        })

    worker.reader_task = asyncio.create_task(finish())
    await supervisor._monitor_exit("engineer", worker)
    callback.assert_awaited_once()
    assert callback.await_args.args[1]["type"] == "task_complete"
    assert worker.current_task_id is None
    assert worker.process is None


async def test_reader_failure_does_not_bypass_exit_cleanup(monkeypatch):
    callback = AsyncMock()
    supervisor, worker = make_supervisor(callback)
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)

    async def fail_reader():
        raise ValueError("malformed worker event")

    worker.reader_task = asyncio.create_task(fail_reader())
    await supervisor._monitor_exit("engineer", worker)
    cleanup.assert_awaited_once()
    callback.assert_awaited_once()
    assert worker.process is None


@pytest.mark.parametrize("failure_mode", ["error", "timeout", "cancel"])
async def test_failure_callback_retains_receipt_until_confirmed_retry(monkeypatch, failure_mode):
    started = asyncio.Event()

    async def callback(*args):
        started.set()
        if failure_mode == "error":
            raise RuntimeError("backend unavailable")
        await asyncio.Event().wait()

    callback_mock = AsyncMock(side_effect=callback)
    supervisor, worker = make_supervisor(callback_mock)
    worker.process.wait.return_value = 1
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())
    original_wait_for = asyncio.wait_for

    async def short_timeout(awaitable, timeout):
        return await original_wait_for(awaitable, timeout=0.01 if timeout == 30 else timeout)

    if failure_mode == "timeout":
        monkeypatch.setattr(asyncio, "wait_for", short_timeout)
    monitor = asyncio.create_task(supervisor._monitor_exit("engineer", worker))
    await started.wait()
    if failure_mode == "cancel":
        monitor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await monitor
    else:
        await monitor
    callback_mock.assert_awaited_once()
    assert worker.process is None
    assert worker.current_task_id is None
    assert supervisor.is_agent_busy("engineer")
    assert worker.pending_failure is not None
    callback_mock.side_effect = None
    await supervisor.retry_pending_cleanup()
    assert callback_mock.await_count == 2
    assert worker.pending_failure is None
    assert not supervisor.is_agent_busy("engineer")


async def test_completion_callback_failure_retains_outcome_and_never_respawns(monkeypatch):
    callback = AsyncMock(side_effect=RuntimeError("platform unavailable"))
    supervisor, worker = make_supervisor(callback)
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())
    completion = {"type": "task_complete", "task_id": "task", "status": "review"}
    await supervisor._complete_worker(worker, completion)
    assert worker.pending_completion == completion
    assert not worker.completion_delivered
    assert supervisor.get_all_statuses()["engineer"]["execution_finalization_pending"] is True
    assert supervisor.is_agent_busy("engineer")
    assert not await supervisor.spawn_worker("engineer", {}, {"task_id": "task"})
    callback.side_effect = None
    await supervisor.retry_pending_cleanup()
    assert callback.await_count == 2
    assert worker.completion_delivered
    assert worker.pending_completion is None
    assert not worker.completion_failed
    assert not supervisor.is_agent_busy("engineer")


async def test_heartbeat_kills_before_backend_notification_and_holds_slot(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    supervisor, worker = make_supervisor()
    process = worker.process
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    monkeypatch.setattr(
        "src.orchestrator.agent_supervisor.HEARTBEAT_INTERVAL_SECONDS", 0,
    )

    async def callback(*args):
        process.terminate.assert_called_once()
        cleanup.assert_awaited_once()
        assert not worker.cleanup_pending
        assert not worker.execution_marker
        started.set()
        await release.wait()

    supervisor._on_event = AsyncMock(side_effect=callback)
    heartbeat = asyncio.create_task(supervisor._heartbeat_loop("engineer", worker))
    await started.wait()
    assert supervisor.is_agent_busy("engineer")
    assert supervisor.reconcile_stuck_agents() == []
    release.set()
    await heartbeat
    supervisor._on_event.assert_awaited_once()
    assert not supervisor.is_agent_busy("engineer")


async def test_heartbeat_cleanup_failure_is_retained_then_recovers_once(monkeypatch):
    callback = AsyncMock()
    supervisor, worker = make_supervisor(callback)
    cleanup = AsyncMock(side_effect=RuntimeError("Docker unavailable"))
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    monkeypatch.setattr(
        "src.orchestrator.agent_supervisor.HEARTBEAT_INTERVAL_SECONDS", 0,
    )
    await supervisor._heartbeat_loop("engineer", worker)
    callback.assert_not_awaited()
    assert supervisor.is_agent_busy("engineer")
    cleanup.side_effect = None
    await asyncio.gather(
        supervisor.retry_pending_cleanup(), supervisor.retry_pending_cleanup(),
    )
    callback.assert_awaited_once()
    assert callback.await_args.args[1]["reason"] == "heartbeat_timeout"
    assert worker.pending_failure is None
    assert not supervisor.is_agent_busy("engineer")


async def test_exit_during_heartbeat_kill_waits_for_container_cleanup(monkeypatch):
    callback = AsyncMock()
    supervisor, worker = make_supervisor(callback)
    worker.process.wait.return_value = -15
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cleanup(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    monkeypatch.setattr(
        "src.orchestrator.agent_supervisor.HEARTBEAT_INTERVAL_SECONDS", 0,
    )
    heartbeat = asyncio.create_task(supervisor._heartbeat_loop("engineer", worker))
    worker.heartbeat_task = heartbeat
    await entered.wait()
    monitor = asyncio.create_task(supervisor._monitor_exit("engineer", worker))
    await asyncio.sleep(0)
    callback.assert_not_awaited()
    assert not heartbeat.cancelled()
    assert supervisor.is_agent_busy("engineer")
    release.set()
    await asyncio.gather(heartbeat, monitor, return_exceptions=True)
    callback.assert_awaited_once()
    assert callback.await_args.args[1]["reason"] == "heartbeat_timeout"
    assert not worker.cleanup_pending
    assert not supervisor.is_agent_busy("engineer")


async def test_completion_callback_cannot_be_reset_by_dead_process_reconciler(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def callback(*args):
        started.set()
        await release.wait()

    supervisor, worker = make_supervisor(callback)
    worker.process.returncode = 0
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())
    completion = asyncio.create_task(supervisor._complete_worker(worker, {
        "type": "task_complete", "task_id": "task", "status": "review",
    }))
    await started.wait()
    assert supervisor.reconcile_stuck_agents() == []
    assert supervisor.is_agent_busy("engineer")
    release.set()
    await completion
    assert not supervisor.is_agent_busy("engineer")


async def test_one_agent_dispatch_failure_does_not_starve_other_agents():
    dispatcher = TaskDispatcher(
        redis=MagicMock(), office_id="office", supervisor=MagicMock(),
        config_store=MagicMock(), queue_manager=MagicMock(),
    )
    dispatcher._get_all_agent_names = MagicMock(return_value=["broken", "healthy"])
    dispatcher.dispatch_agent = AsyncMock(side_effect=[RuntimeError("spawn failed"), True])
    assert await dispatcher.dispatch_all_idle() == 1
    assert [call.args[0] for call in dispatcher.dispatch_agent.await_args_list] == [
        "broken", "healthy",
    ]


async def test_dispatch_shutdown_cancellation_is_not_swallowed():
    dispatcher = TaskDispatcher(
        redis=MagicMock(), office_id="office", supervisor=MagicMock(),
        config_store=MagicMock(), queue_manager=MagicMock(),
    )
    dispatcher._get_all_agent_names = MagicMock(return_value=["cancelled", "other"])
    dispatcher.dispatch_agent = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.dispatch_all_idle()
    dispatcher.dispatch_agent.assert_awaited_once_with("cancelled")


def make_watchdog(supervisor):
    board_client = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.add_task = AsyncMock()
    watchdog = TaskWatchdog(
        ws=board_client, executor=None, manager=MagicMock(), config_store=MagicMock(),
        task_queue=None, office_id="office", supervisor=supervisor, dispatcher=dispatcher,
    )
    return watchdog, board_client, dispatcher


async def test_confirmed_failures_hit_budget_between_watchdog_ticks_despite_backend_error(
    monkeypatch,
):
    supervisor, _ = make_supervisor(AsyncMock(side_effect=RuntimeError("backend down")))
    watchdog, board_client, dispatcher = make_watchdog(supervisor)
    supervisor.set_failure_observer(watchdog.record_process_failure)
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())
    for _attempt in range(3):
        _, worker = make_supervisor()
        supervisor._agents["engineer"] = worker
        await supervisor._monitor_exit("engineer", worker)
        await supervisor._report_failure(worker, {"task_id": "task"})
    assert watchdog._task_crash_count["task"] == 3
    assert watchdog.respawn_capped("task")
    task = {"id": "task", "status": "in_progress", "assigned_agent": "engineer"}

    async def request(action, params, **kwargs):
        if action == "get_board":
            return {"items": [task]}
        return {"ok": True}

    board_client.request.side_effect = request
    await watchdog._check_board()
    assert not [call for call in board_client.request.await_args_list if call.args[0] == "move_task"]
    supervisor._on_event.side_effect = None
    await supervisor.retry_pending_cleanup()
    await watchdog._check_board()
    blocked_moves = [
        call for call in board_client.request.await_args_list if call.args[0] == "move_task"
    ]
    assert len(blocked_moves) == 1
    assert blocked_moves[0].args[1]["new_status"] == "blocked"
    dispatcher.add_task.assert_not_awaited()


async def test_observed_failure_and_orphan_poll_do_not_double_count():
    supervisor, worker = make_supervisor()
    worker.state = AgentState.IDLE
    worker.current_task_id = None
    watchdog, _, dispatcher = make_watchdog(supervisor)
    watchdog.record_process_failure("task", "attempt")
    watchdog.record_process_failure("task", "attempt")
    await watchdog._handle_in_progress({
        "id": "task", "assigned_agent": "engineer", "status": "in_progress",
    })
    assert watchdog._task_crash_count["task"] == 1
    dispatcher.add_task.assert_awaited_once()


@pytest.mark.parametrize("board", [{"error": "backend down"}, {}, None])
async def test_failed_board_read_preserves_attempt_budget(board):
    watchdog, board_client, _ = make_watchdog(MagicMock())
    watchdog.record_process_failure("task", "attempt")
    board_client.request.return_value = board
    await watchdog._check_board()
    assert watchdog._task_crash_count["task"] == 1
    assert watchdog._confirmed_failed_attempts["task"] == {"attempt"}


async def test_valid_terminal_board_resets_attempt_budget_for_later_rework():
    watchdog, board_client, _ = make_watchdog(MagicMock())
    watchdog.record_process_failure("task", "attempt")
    board_client.request.return_value = {"items": []}
    await watchdog._check_board()
    assert not watchdog.respawn_capped("task")
    assert "task" not in watchdog._confirmed_failed_attempts
    assert "task" not in watchdog._reported_failure_pending
    watchdog.record_process_failure("task", "rework-attempt")
    assert watchdog._task_crash_count["task"] == 1


async def test_board_fetch_cannot_clear_failure_recorded_after_snapshot_started():
    watchdog, board_client, _ = make_watchdog(MagicMock())

    async def request(*args, **kwargs):
        watchdog.record_process_failure("task", "attempt")
        return {"items": []}

    board_client.request.side_effect = request
    await watchdog._check_board()
    assert watchdog._task_crash_count["task"] == 1
    assert watchdog._confirmed_failed_attempts["task"] == {"attempt"}


async def test_spawn_failure_counts_without_waiting_for_idle_poll(monkeypatch):
    supervisor, _ = make_supervisor()
    supervisor._agents.clear()
    watchdog, _, _ = make_watchdog(supervisor)
    supervisor.set_failure_observer(watchdog.record_process_failure)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("no executable")),
    )
    assert not await supervisor.spawn_worker("engineer", {}, {"task_id": "task"})
    assert watchdog._task_crash_count["task"] == 1


@pytest.mark.parametrize("mode", ["review", "triage", "stop"])
async def test_executor_budget_excludes_other_phases_and_user_stop(monkeypatch, mode):
    supervisor, worker = make_supervisor(AsyncMock())
    watchdog, _, _ = make_watchdog(supervisor)
    supervisor.set_failure_observer(watchdog.record_process_failure)
    if mode == "stop":
        worker.stop_requested = True
    else:
        worker.execution_mode = mode
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())
    await supervisor._monitor_exit("engineer", worker)
    assert watchdog._task_crash_count == {}


def test_finalization_status_retains_task_identity():
    supervisor, worker = make_supervisor()
    worker.current_task_id = None
    worker.pending_failure = {"task_id": "task"}
    status = supervisor.get_all_statuses()["engineer"]
    assert status["current_task"] == "task"
    assert status["execution_finalization_pending"] is True


async def test_cleanup_health_flag_reports_failure_not_normal_finishing(monkeypatch):
    supervisor, worker = make_supervisor()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cleanup(*args):
        entered.set()
        await release.wait()
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    cleaning = asyncio.create_task(supervisor._cleanup_execution(worker))
    await entered.wait()
    status = supervisor.get_all_statuses()["engineer"]
    assert status["execution_cleanup_pending"] is True
    assert status["execution_cleanup_failed"] is False
    assert status["execution_finalization_pending"] is False
    release.set()
    with pytest.raises(RuntimeError):
        await cleaning
    assert supervisor.get_all_statuses()["engineer"]["execution_cleanup_failed"] is True
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", AsyncMock())
    await supervisor._cleanup_execution(worker)
    status = supervisor.get_all_statuses()["engineer"]
    assert status["execution_cleanup_failed"] is False
    assert status["execution_cleanup_pending"] is False


async def test_crash_cap_escalation_recovers_after_backend_outage_without_respawn(monkeypatch):
    supervisor, worker = make_supervisor()
    worker.state = AgentState.IDLE
    worker.current_task_id = None
    watchdog, board_client, dispatcher = make_watchdog(supervisor)
    watchdog._task_crash_count["task"] = 3
    task = {"id": "task", "assigned_agent": "engineer", "status": "in_progress"}
    board_client.request.return_value = {"error": "backend unavailable"}
    clock_value = [100.0]
    monkeypatch.setattr("src.watchdog.time.monotonic", lambda: clock_value[0])
    for _attempt in range(3):
        await watchdog._handle_in_progress(task)
    requests_before_backoff = board_client.request.await_count
    await watchdog._handle_in_progress(task)
    assert board_client.request.await_count == requests_before_backoff
    assert watchdog.respawn_capped("task")
    clock_value[0] = watchdog._move_retry_after["task"] + 1
    board_client.request.return_value = {"ok": True}
    await watchdog._handle_in_progress(task)
    assert "task" in watchdog._blocked_escalated
    assert "task" not in watchdog._move_retry_after
    dispatcher.add_task.assert_not_awaited()


async def test_spawn_rechecks_office_capacity_after_old_process_cleanup(monkeypatch):
    supervisor, worker = make_supervisor()
    supervisor._max_agents = 1
    worker.state = AgentState.IDLE
    worker.execution_marker = ""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def clean_old(*args, **kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(supervisor, "_kill_process", clean_old)
    spawning = asyncio.create_task(supervisor.spawn_worker(
        "engineer", {}, {"task_id": "new-task"},
    ))
    await entered.wait()
    supervisor._agents["other"] = AgentProcess(
        agent_name="other", role="worker", state=AgentState.WORKING,
    )
    release.set()
    assert not await spawning
    assert supervisor.active_count == 1


@pytest.mark.parametrize("stage", ["process_creation", "ready_handshake"])
async def test_manager_spawn_cancellation_retains_and_cleans_process(monkeypatch, stage):
    supervisor, worker = make_supervisor()
    supervisor._agents.clear()
    process = worker.process
    process.stdout = asyncio.StreamReader()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def create_process(*args, **kwargs):
        if stage == "process_creation":
            entered.set()
            await release.wait()
        return process

    async def wait_ready(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(supervisor, "_wait_for_ready", wait_ready)
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    spawning = asyncio.create_task(supervisor.spawn_manager({}))
    await entered.wait()
    spawning.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await spawning
    process.terminate.assert_called_once()
    cleanup.assert_awaited_once()
    assert not supervisor.is_agent_busy("manager")
    manager = supervisor._agents["manager"]
    if manager.reader_task is not None:
        process.stdout.feed_eof()
        await manager.reader_task


@pytest.mark.parametrize("role", ["worker", "manager"])
async def test_cancelled_spawn_timeout_retries_eventual_process_handle(monkeypatch, role):
    supervisor, worker = make_supervisor()
    supervisor._agents.clear()
    process = worker.process
    entered = asyncio.Event()
    release = asyncio.Event()

    async def create_process(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr("src.orchestrator.agent_supervisor.SPAWN_TIMEOUT_SECONDS", 0.01)
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    if role == "worker":
        spawning = asyncio.create_task(supervisor.spawn_worker(
            "engineer", {}, {"task_id": "task"},
        ))
        agent_name = "engineer"
    else:
        spawning = asyncio.create_task(supervisor.spawn_manager({}))
        agent_name = "manager"
    await entered.wait()
    spawning.cancel()
    with pytest.raises(asyncio.TimeoutError):
        await spawning
    agent = supervisor._agents[agent_name]
    assert agent.kill_initiated
    assert agent.cleanup_failed
    assert supervisor.is_agent_busy(agent_name)
    release.set()
    await agent.spawn_task
    await supervisor.retry_pending_cleanup()
    process.terminate.assert_called_once()
    cleanup.assert_awaited_once()
    assert not agent.cleanup_failed
    assert not supervisor.is_agent_busy(agent_name)


async def test_cancelled_creation_that_later_fails_does_not_leave_permanent_busy_slot(monkeypatch):
    supervisor, _ = make_supervisor()
    supervisor._agents.clear()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def create_process(*args, **kwargs):
        entered.set()
        await release.wait()
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    spawning = asyncio.create_task(supervisor.spawn_worker(
        "engineer", {}, {"task_id": "task"},
    ))
    await entered.wait()
    spawning.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await spawning
    cleanup.assert_awaited_once()
    assert not supervisor.is_agent_busy("engineer")
