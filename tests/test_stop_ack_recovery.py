"""Confirmed Stop retains ownership until its durable outcome is acknowledged."""

from unittest.mock import AsyncMock, Mock

import pytest

from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.runtime_state import RuntimeState


@pytest.mark.parametrize("outcome", ["fatal", "completion"])
@pytest.mark.parametrize("execution_marker", ["", "synthetic-exact-marker"])
async def test_stop_retries_storage_ack_after_cleanup_without_replaying_callback(
    tmp_path, monkeypatch, outcome, execution_marker,
):
    runtime = RuntimeState(tmp_path / "runtime.sqlite3", "office")
    callback = AsyncMock(side_effect=ConnectionError("Synthetic callback outage"))
    supervisor = AgentSupervisor(".", "office", on_event=callback)
    supervisor.set_runtime_state(runtime)
    agent = AgentProcess(
        "engineer", "worker", state=AgentState.WORKING,
        execution_task_id="task", execution_attempt_id="attempt",
        execution_cycle=2, execution_generation=3, execution_mode="execute",
        execution_assignee="engineer", execution_marker=execution_marker,
    )
    supervisor._agents["engineer"] = agent
    cleanup = AsyncMock()
    monkeypatch.setattr("src.docker.task_process_cleanup.terminate_worker_execution", cleanup)
    if outcome == "fatal":
        supervisor._retain_failure(agent, {"type": "error", "fatal": True, "task_id": "task"})
    else:
        await supervisor._complete_worker(agent, {"type": "task_complete", "task_id": "task"})
    receipt = runtime.pending_completions()[0]
    callback_calls = callback.await_count
    acknowledge = runtime.acknowledge_completion
    failing_ack = Mock(side_effect=OSError("Synthetic SQLite outage"))
    monkeypatch.setattr(runtime, "acknowledge_completion", failing_ack)

    assert not await supervisor.stop_task("engineer", "unrelated-task")
    assert not await supervisor.stop_task("engineer", "task", expected_mode="review")
    assert not await supervisor.stop_task("engineer", "task", expected_execution_marker="other-marker")
    failing_ack.assert_not_called()
    with pytest.raises(OSError, match="SQLite outage"):
        await supervisor.stop_task("engineer", "task")
    assert not agent.execution_marker
    assert not agent.cleanup_pending
    assert supervisor.is_agent_busy("engineer")

    # The periodic retry must try storage again even though exact cleanup has
    # already succeeded. Another failed write must not erase its owning task.
    await supervisor.retry_pending_cleanup()
    assert failing_ack.call_count == 2
    assert runtime.pending_completions() == [receipt]
    assert agent.pending_failure is not None or agent.pending_completion is not None
    assert supervisor.is_agent_busy("engineer")
    assert supervisor.reconcile_stuck_agents() == []
    assert supervisor.is_agent_busy("engineer")
    assert callback.await_count == callback_calls

    recovered_ack = Mock(wraps=acknowledge)
    monkeypatch.setattr(runtime, "acknowledge_completion", recovered_ack)
    await supervisor.retry_pending_cleanup()
    assert not runtime.pending_completions()
    assert not supervisor.is_agent_busy("engineer")
    assert agent.state == AgentState.IDLE
    assert callback.await_count == callback_calls
    recovered_ack.assert_called_once_with("attempt")
    await supervisor.retry_pending_cleanup()
    recovered_ack.assert_called_once_with("attempt")
    assert not await supervisor.stop_task("engineer", "task")
    assert not await supervisor.stop_task("engineer", "unrelated-task")
    assert cleanup.await_count == bool(execution_marker)
