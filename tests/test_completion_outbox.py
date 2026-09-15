"""Restart recovery delivers retained outcomes, never restarts business work."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.docker import task_process_cleanup
from src.orchestrator.agent_supervisor import AgentProcess, AgentSupervisor
from src.runtime_state import RuntimeState
from src.execution_completion import completion_task_state
import httpx


@pytest.fixture
def runtime(tmp_path):
    return RuntimeState(tmp_path / "runtime.sqlite3", "office")


def completion_event():
    return {
        "type": "task_complete", "task_id": "task", "status": "review",
        "_caller": {
            "agent_name": "engineer", "role": "worker", "task_id": "task",
            "attempt_id": "attempt", "execution_cycle": 2, "execution_generation": 5,
            "expected_assigned_agent": "engineer", "task_mode": "execute",
        },
    }


async def test_restart_restores_finalization_not_a_worker_spawn(runtime, monkeypatch):
    runtime.retain_completion("engineer", "attempt", "task", completion_event())
    callback = AsyncMock()
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(RuntimeState(runtime.database_path, "office"))
    spawn = AsyncMock()
    monkeypatch.setattr("src.orchestrator.agent_supervisor.asyncio.create_subprocess_exec", spawn)
    assert supervisor.is_agent_busy("engineer")
    assert supervisor.get_all_statuses()["engineer"]["execution_finalization_pending"]
    assert not await supervisor.spawn_worker("engineer", {}, {"task_id": "task"})
    await supervisor.retry_pending_cleanup()
    callback.assert_awaited_once_with("engineer", completion_event())
    spawn.assert_not_awaited()
    assert not runtime.has_pending_completion("task")
    assert not supervisor.is_agent_busy("engineer")


async def test_completion_is_durable_before_cleanup_and_survives_callback_failure(runtime, monkeypatch):
    supervisor = AgentSupervisor(".", "office", on_event=AsyncMock(side_effect=RuntimeError("offline")))
    supervisor.set_runtime_state(runtime)
    event = completion_event()
    agent = AgentProcess(
        "engineer", "worker", current_task_id="task", execution_task_id="task",
        execution_marker="marker", execution_attempt_id="attempt", execution_cycle=2,
        execution_generation=5, execution_assignee="engineer", execution_mode="execute",
    )
    supervisor._agents["engineer"] = agent

    async def cleanup(*args):
        assert runtime.has_pending_completion("task")

    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    await supervisor._complete_worker(agent, event)
    assert runtime.pending_completions()[0]["payload"] == event
    assert agent.completion_failed
    assert not agent.execution_marker


async def test_receipt_storage_failure_prevents_cleanup_release(runtime, monkeypatch):
    supervisor = AgentSupervisor(".", "office", on_event=AsyncMock())
    supervisor.set_runtime_state(runtime)
    agent = AgentProcess("engineer", "worker", current_task_id="task", execution_task_id="task", execution_marker="marker")
    supervisor._agents["engineer"] = agent
    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    monkeypatch.setattr(runtime, "retain_completion", MagicMock(side_effect=OSError("storage failed")))
    with pytest.raises(OSError):
        await supervisor._complete_worker(agent, {"type": "task_complete", "task_id": "task"})
    cleanup.assert_not_awaited()
    assert supervisor.is_agent_busy("engineer")


async def test_stop_of_confirmed_dead_retained_completion_discards_callback(runtime):
    runtime.retain_completion("engineer", "attempt", "task", completion_event())
    callback = AsyncMock()
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    assert await supervisor.stop_task("engineer", "task")
    assert not runtime.has_pending_completion("task")
    await supervisor.retry_pending_cleanup()
    callback.assert_not_awaited()


def test_conflicting_retained_owner_fails_closed(runtime):
    runtime.retain_completion("engineer", "attempt", "task", completion_event())
    runtime.retain_completion("engineer", "another-attempt", "other-task", {"type": "task_complete", "task_id": "other-task"})
    supervisor = AgentSupervisor(".", "office")
    with pytest.raises(RuntimeError, match="Multiple retained"):
        supervisor.set_runtime_state(runtime)
    assert runtime.has_pending_completion("task")
    assert runtime.has_pending_completion("other-task")


def test_spoofed_completion_task_cannot_be_persisted(runtime):
    with pytest.raises(ValueError, match="identity"):
        runtime.retain_completion("engineer", "attempt", "unrelated-task", completion_event())


def test_deleted_task_is_terminal_but_unavailable_state_is_not():
    request = httpx.Request("GET", "http://platform/task")
    assert completion_task_state(httpx.Response(404, request=request)) is None
    for status in (401, 403, 500):
        with pytest.raises(httpx.HTTPStatusError):
            completion_task_state(httpx.Response(status, request=request))
