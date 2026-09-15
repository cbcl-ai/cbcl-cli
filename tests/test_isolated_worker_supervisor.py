"""The opt-in supervisor routes and cleans exact task-owned containers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src._handlers._tasks import route_task_kill
from src.docker.execution_ledger import ExecutionCapacityUnavailable
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    supervisor = AgentSupervisor(str(tmp_path), str(uuid4()), container_name="office-container")
    manager = SimpleNamespace(
        available=AsyncMock(return_value=True), task_available=AsyncMock(return_value=True),
        prepare=AsyncMock(return_value=SimpleNamespace(container_id="c" * 64)),
        stop_attempt=AsyncMock(), stop_task=AsyncMock(return_value=False), stop_all=AsyncMock(), close=AsyncMock(),
    )
    supervisor.set_execution_containers(manager)
    supervisor._wait_for_ready = AsyncMock()
    supervisor._send_to_agent = AsyncMock()
    supervisor._reader_loop = AsyncMock()
    supervisor._monitor_exit = AsyncMock()
    supervisor._heartbeat_loop = AsyncMock()
    process = MagicMock(pid=123, returncode=0)
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    marker_cleanup = AsyncMock()
    monkeypatch.setattr("src.docker.task_process_cleanup.terminate_worker_execution", marker_cleanup)
    return supervisor, manager, marker_cleanup


async def test_task_launch_and_cleanup_use_exact_isolated_id(isolated):
    supervisor, manager, marker_cleanup = isolated
    task_id = str(uuid4())
    assert await supervisor.spawn_worker("analyst", {}, {"task_id": task_id, "status": "in_progress"})
    agent = supervisor._agents["analyst"]
    assert agent.execution_container_managed
    assert supervisor._send_to_agent.await_args.args[1]["agent_config"]["_container_name"] == "c" * 64
    await supervisor.stop_task("analyst", task_id)
    manager.stop_attempt.assert_awaited_once_with(task_id, agent.execution_attempt_id)
    marker_cleanup.assert_not_awaited()
    await supervisor.shutdown()
    manager.stop_all.assert_awaited_once()
    manager.close.assert_awaited_once()


async def test_shutdown_reconciles_retained_executions_without_host_workers(isolated):
    supervisor, manager, _cleanup = isolated
    manager.stop_all.side_effect = RuntimeError("Retained startup remains unconfirmed")
    with pytest.raises(RuntimeError, match="isolated-executions"):
        await supervisor.shutdown()
    manager.stop_all.assert_awaited_once()
    manager.close.assert_not_awaited()


async def test_shutdown_attempts_retained_stop_despite_other_cleanup_failure(isolated):
    supervisor, manager, _cleanup = isolated
    supervisor._agents["analyst"] = AgentProcess("analyst", "worker")
    supervisor._kill_process = AsyncMock(side_effect=RuntimeError("Host cleanup unavailable"))
    with pytest.raises(RuntimeError, match="analyst"):
        await supervisor.shutdown()
    manager.stop_all.assert_awaited_once()
    manager.close.assert_not_awaited()


async def test_busy_pool_refuses_before_claim_or_container_launch(isolated):
    supervisor, manager, _cleanup = isolated
    manager.available.return_value = False
    claimer = AsyncMock()
    supervisor.set_execution_claimer(claimer)
    assert not await supervisor.spawn_worker("analyst", {}, {"task_id": str(uuid4())})
    claimer.assert_not_awaited()
    manager.prepare.assert_not_awaited()


async def test_retained_task_blocks_new_attempt_after_restart(isolated):
    supervisor, manager, _cleanup = isolated
    manager.task_available.return_value = False
    assert not await supervisor.spawn_worker("analyst", {}, {"task_id": str(uuid4())})
    manager.prepare.assert_not_awaited()


async def test_atomic_capacity_refusal_is_not_a_worker_crash(isolated):
    supervisor, manager, _cleanup = isolated
    manager.prepare.side_effect = ExecutionCapacityUnavailable("Pool became full")
    supervisor._record_failure = MagicMock()
    assert not await supervisor.spawn_worker("analyst", {}, {"task_id": str(uuid4())})
    supervisor._record_failure.assert_not_called()
    manager.stop_attempt.assert_awaited_once()
    assert not supervisor._agents["analyst"].execution_marker


@pytest.mark.parametrize("task_id", ["planner-synthetic", "flow-consult-synthetic"])
async def test_synthetic_consults_keep_the_office_runtime(isolated, task_id):
    supervisor, manager, _cleanup = isolated
    assert await supervisor.spawn_worker("planner", {}, {"task_id": task_id})
    manager.prepare.assert_not_awaited()
    assert supervisor._send_to_agent.await_args.args[1]["agent_config"]["_container_name"] == "office-container"
    await supervisor.shutdown()


async def test_failed_isolated_cleanup_retains_slot_and_marker(isolated):
    supervisor, manager, marker_cleanup = isolated
    agent = AgentProcess("analyst", "worker", state=AgentState.WORKING,
                         execution_marker="a" * 64, execution_container_managed=True,
                         execution_task_id=str(uuid4()))
    supervisor._agents[agent.agent_name] = agent
    manager.stop_attempt.side_effect = RuntimeError("Docker unavailable")
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        await supervisor._cleanup_execution(agent)
    assert agent.cleanup_pending and agent.cleanup_failed
    assert agent.execution_marker
    assert supervisor.is_agent_busy(agent.agent_name)
    marker_cleanup.assert_not_awaited()


@pytest.mark.parametrize("fails", [False, True])
async def test_stop_checks_retained_container_even_without_host_worker(isolated, fails):
    supervisor, manager, _cleanup = isolated
    task_id = str(uuid4())
    manager.stop_task.return_value = True
    if fails:
        manager.stop_task.side_effect = RuntimeError("Unsettled old launch")
    router = AsyncMock()
    await route_task_kill(
        {"task_id": task_id, "stop_request_id": str(uuid4()), "all_agents": True},
        queue_manager=AsyncMock(), dispatcher=MagicMock(), supervisor=supervisor, router=router,
    )
    manager.stop_task.assert_awaited_once_with(task_id)
    receipt = router.publish_event.await_args.args[0]
    assert receipt["status"] == ("unconfirmed" if fails else "stopped")
