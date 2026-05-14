"""Integration tests for concurrent agent execution.

Tests:
3. Concurrent agents: 5 agents running simultaneously -> all complete
9. Agent spawn failure: spawn fails -> task re-queued -> dispatched on retry
"""

import asyncio
import os
import sys

import pytest

from tests.integration.conftest import TEST_OFFICE_ID, MOCK_AGENT_SCRIPT


@pytest.mark.asyncio
async def test_five_concurrent_agents(
    redis_client, config_store, tmp_path,
):
    """Scenario 3: 5 agents running simultaneously, all complete."""
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.orchestrator.task_dispatcher import TaskDispatcher
    from src.orchestrator.agent_queue import AgentQueueManager

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    # Register 5 distinct agents in config
    agents = []
    for i in range(5):
        name = f"worker-{i}"
        agents.append({
            "name": name,
            "display_name": f"Worker {i}",
            "agent_type": "custom",
            "model": "claude-sonnet-4-6",
            "system_prompt": "Mock",
            "allowed_tools": ["Read"],
            "is_active": True,
        })
    config_store.agents = agents

    supervisor = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        max_agents=10,
        _agent_command=[sys.executable, MOCK_AGENT_SCRIPT],
    )

    queue_manager = AgentQueueManager(redis_client, TEST_OFFICE_ID)
    dispatcher = TaskDispatcher(
        redis=redis_client,
        office_id=TEST_OFFICE_ID,
        supervisor=supervisor,
        config_store=config_store,
        queue_manager=queue_manager,
    )

    completed_tasks = []

    async def on_event(agent_name: str, msg: dict) -> None:
        if msg.get("type") == "task_complete":
            completed_tasks.append(msg["task_id"])

    supervisor._on_event = on_event

    # Configure fast mock
    os.environ["MOCK_DELAY"] = "1"

    try:
        # Add 5 tasks, one per agent
        for i in range(5):
            await dispatcher.add_task({
                "task_id": f"concurrent-{i}",
                "readable_id": f"CC-001.T{i:02d}",
                "title": f"Concurrent task {i}",
                "assigned_agent": f"worker-{i}",
                "priority": "medium",
                "brief": {"goal": "test"},
                "labels": [],
                "workstream_name": "Test",
            })

        dispatch_task = asyncio.create_task(dispatcher.run())

        # All 5 should complete in parallel (1s work each, not 5s serial)
        for _ in range(80):  # 8 second timeout (generous for 1s parallel work)
            await asyncio.sleep(0.1)
            if len(completed_tasks) >= 5:
                break

        assert len(completed_tasks) == 5, \
            f"Expected 5 completions, got {len(completed_tasks)}: {completed_tasks}"

        # Verify all task IDs are present
        expected_ids = {f"concurrent-{i}" for i in range(5)}
        actual_ids = set(completed_tasks)
        assert actual_ids == expected_ids, \
            f"Missing tasks: {expected_ids - actual_ids}"

        # Verify no agents are busy
        for i in range(5):
            await asyncio.sleep(0.1)
        for i in range(5):
            assert not supervisor.is_agent_busy(f"worker-{i}")

    finally:
        os.environ.pop("MOCK_DELAY", None)
        await dispatcher.stop()
        dispatch_task.cancel()
        await supervisor.shutdown(timeout=5)
        try:
            await dispatch_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_spawn_failure_requeues_task(
    redis_client, config_store, mock_task_data, tmp_path,
):
    """Scenario 9: Spawn fails -> task re-queued -> succeeds on retry."""
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.orchestrator.task_dispatcher import TaskDispatcher

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    # Create supervisor with a BAD command first (will fail to spawn)
    supervisor = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        _agent_command=[sys.executable, "/nonexistent/script.py"],
    )

    from src.orchestrator.agent_queue import AgentQueueManager

    queue_manager = AgentQueueManager(redis_client, TEST_OFFICE_ID)
    dispatcher = TaskDispatcher(
        redis=redis_client,
        office_id=TEST_OFFICE_ID,
        supervisor=supervisor,
        config_store=config_store,
        queue_manager=queue_manager,
    )

    # Add task
    await dispatcher.add_task(mock_task_data)
    assert await dispatcher.get_queue_size() >= 1

    # Run one dispatch cycle -- should fail to spawn and re-queue
    await dispatcher.dispatch_all_idle()

    # Task should still be in queue (re-queued after spawn failure)
    # Note: add_task is called again on spawn failure which re-adds it
    queue_size = await dispatcher.get_queue_size()
    assert queue_size >= 1, "Task should be re-queued after spawn failure"

    # Fix the supervisor command
    supervisor._agent_command = [sys.executable, MOCK_AGENT_SCRIPT]

    events = []

    async def on_event(agent_name: str, msg: dict) -> None:
        events.append(msg)

    supervisor._on_event = on_event

    # Run another dispatch cycle -- should succeed now
    dispatch_task = asyncio.create_task(dispatcher.run())

    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            if any(e.get("type") == "task_complete" for e in events):
                break

        assert any(e.get("type") == "task_complete" for e in events), \
            "Task should complete after fixing spawn command"

    finally:
        await dispatcher.stop()
        dispatch_task.cancel()
        await supervisor.shutdown(timeout=5)
        try:
            await dispatch_task
        except asyncio.CancelledError:
            pass
