"""Integration tests for crash recovery and fault tolerance.

Tests:
2.  Crash recovery: agent crashes mid-task -> detected -> reported
6.  Graceful shutdown: agents finish within grace period
7.  Orchestrator restart: tasks in progress -> recovered after restart
10. Heartbeat timeout: agent stops sending messages -> detected
"""

import asyncio
import os
import sys

import pytest

from tests.integration.conftest import TEST_OFFICE_ID, MOCK_AGENT_SCRIPT


@pytest.mark.asyncio
async def test_agent_crash_detection(
    supervisor, dispatcher, config_store, mock_task_data, redis_client,
):
    """Scenario 2: Agent crashes mid-task -> detected -> error event."""
    events = []

    async def on_event(agent_name: str, msg: dict) -> None:
        events.append(msg)

    supervisor._on_event = on_event

    # Configure mock to crash after 1 second
    os.environ["MOCK_CRASH_AFTER"] = "1"

    dispatch_task = asyncio.create_task(dispatcher.run())

    try:
        await dispatcher.add_task(mock_task_data)

        # Wait for crash detection
        for _ in range(50):  # 5 second timeout
            await asyncio.sleep(0.1)
            errors = [e for e in events if e.get("type") == "error" and e.get("fatal")]
            if errors:
                break

        # Verify crash was detected
        assert any(
            e.get("type") == "error" and e.get("fatal") for e in events
        ), "Expected fatal error event from crash"

        # Verify agent is no longer busy
        await asyncio.sleep(0.5)  # Allow cleanup
        assert not supervisor.is_agent_busy("mock-worker")

    finally:
        os.environ.pop("MOCK_CRASH_AFTER", None)
        await dispatcher.stop()
        dispatch_task.cancel()
        try:
            await dispatch_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_graceful_shutdown_waits_for_agents(
    supervisor, config_store, mock_task_data,
):
    """Scenario 6: Shutdown waits for agents to finish within grace period."""
    events = []

    async def on_event(agent_name: str, msg: dict) -> None:
        events.append(msg)

    supervisor._on_event = on_event

    # Configure mock to take 2 seconds
    os.environ["MOCK_DELAY"] = "2"

    try:
        # Spawn and assign task
        agent_config = config_store.get_agent("mock-worker")
        success = await supervisor.spawn_worker("mock-worker", agent_config, mock_task_data)
        assert success, "Failed to spawn worker"

        # Immediately start shutdown with 10s grace
        await asyncio.sleep(0.5)  # Let the agent start working
        await supervisor.shutdown(timeout=10)

        # Agent should have completed (2s work < 10s grace)
        completions = [e for e in events if e.get("type") == "task_complete"]
        assert len(completions) >= 1, \
            "Agent should have completed within grace period"

    finally:
        os.environ.pop("MOCK_DELAY", None)


@pytest.mark.asyncio
async def test_graceful_shutdown_kills_after_timeout(
    supervisor, config_store, mock_task_data,
):
    """Scenario 6 (negative): Agents killed after grace period expires."""
    # Configure mock to hang (no completion)
    os.environ["MOCK_HANG"] = "1"

    try:
        agent_config = config_store.get_agent("mock-worker")
        success = await supervisor.spawn_worker("mock-worker", agent_config, mock_task_data)
        assert success, "Failed to spawn worker"

        assert supervisor.is_agent_busy("mock-worker")

        # Shutdown with very short grace period (2s)
        await supervisor.shutdown(timeout=2)

        # Agent should be killed
        assert not supervisor.is_agent_busy("mock-worker")
        assert supervisor.active_count == 0

    finally:
        os.environ.pop("MOCK_HANG", None)


@pytest.mark.asyncio
async def test_task_queue_survives_restart(
    redis_client, config_store, mock_task_data, tmp_path,
):
    """Scenario 7: Tasks in Redis queue survive Orchestrator restart."""
    from src.orchestrator.task_dispatcher import TaskDispatcher
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.orchestrator.agent_queue import AgentQueueManager

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    # Phase 1: Create dispatcher, add task, then "crash" (stop without draining)
    supervisor1 = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        _agent_command=[sys.executable, MOCK_AGENT_SCRIPT],
    )
    queue_manager1 = AgentQueueManager(redis_client, TEST_OFFICE_ID)
    dispatcher1 = TaskDispatcher(
        redis=redis_client,
        office_id=TEST_OFFICE_ID,
        supervisor=supervisor1,
        config_store=config_store,
        queue_manager=queue_manager1,
    )
    from tests.integration.conftest import stub_dispatcher_backend_calls
    stub_dispatcher_backend_calls(dispatcher1)

    await dispatcher1.add_task(mock_task_data)
    queue_size = await dispatcher1.get_queue_size()
    assert queue_size == 1, "Task should be in queue"

    # Simulate crash: stop without draining
    await dispatcher1.stop()
    await supervisor1.shutdown(timeout=2)

    # Phase 2: New supervisor + dispatcher (simulating restart)
    supervisor2 = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        _agent_command=[sys.executable, MOCK_AGENT_SCRIPT],
    )
    queue_manager2 = AgentQueueManager(redis_client, TEST_OFFICE_ID)
    dispatcher2 = TaskDispatcher(
        redis=redis_client,
        office_id=TEST_OFFICE_ID,
        supervisor=supervisor2,
        config_store=config_store,
        queue_manager=queue_manager2,
    )
    stub_dispatcher_backend_calls(dispatcher2)

    # Task should still be in Redis
    queue_size = await dispatcher2.get_queue_size()
    assert queue_size == 1, "Task should survive restart"

    # Dispatch should work
    events = []

    async def on_event(agent_name: str, msg: dict) -> None:
        events.append(msg)

    supervisor2._on_event = on_event

    dispatch_task = asyncio.create_task(dispatcher2.run())
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            if any(e.get("type") == "task_complete" for e in events):
                break

        assert any(e.get("type") == "task_complete" for e in events), \
            "Task should complete after restart"
    finally:
        await dispatcher2.stop()
        dispatch_task.cancel()
        await supervisor2.shutdown(timeout=5)
        try:
            await dispatch_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_heartbeat_timeout(
    tmp_path, config_store, mock_task_data,
):
    """Scenario 10: Agent hangs -> heartbeat timeout -> detected and killed."""
    from src.orchestrator.agent_supervisor import AgentSupervisor
    import src.orchestrator.agent_supervisor as sup_mod

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    events = []

    async def on_event(agent_name: str, msg: dict) -> None:
        events.append(msg)

    # Override heartbeat config for fast test.
    # The heartbeat loop sleeps HEARTBEAT_INTERVAL_SECONDS before the first
    # check, then checks if (now - last_message_at) > HEARTBEAT_TIMEOUT_SECONDS.
    # With interval=1s and timeout=3s, the first check at t+1s sees the agent
    # sent "ready" at ~t+0s, so elapsed ~1s < 3s. The next check at t+2s sees
    # ~2s < 3s. The third check at t+3s sees ~3s >= 3s -> timeout detected.
    orig_interval = sup_mod.HEARTBEAT_INTERVAL_SECONDS
    orig_timeout = sup_mod.HEARTBEAT_TIMEOUT_SECONDS
    sup_mod.HEARTBEAT_INTERVAL_SECONDS = 1
    sup_mod.HEARTBEAT_TIMEOUT_SECONDS = 3

    os.environ["MOCK_HANG"] = "1"

    supervisor = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        on_event=on_event,
        _agent_command=[sys.executable, MOCK_AGENT_SCRIPT],
    )

    try:
        agent_config = config_store.get_agent("mock-worker")
        success = await supervisor.spawn_worker("mock-worker", agent_config, mock_task_data)
        assert success, "Failed to spawn worker"

        # Wait for heartbeat timeout detection.
        # Timeline: ready at ~t+0, first ping at t+1 (sends PING but mock
        # hangs so no PONG), check at t+1 sees ~1s elapsed (OK), next ping
        # at t+2, check sees ~2s (OK), next ping at t+3, check sees ~3s >= 3s
        # -> kill. Adding generous buffer for process scheduling.
        for _ in range(150):  # 15 second timeout
            await asyncio.sleep(0.1)
            errors = [e for e in events if e.get("type") == "error" and e.get("fatal")]
            if errors:
                break

        # Verify heartbeat timeout was detected.
        # The heartbeat loop kills the process (SIGTERM -> exit code -15),
        # which causes _monitor_exit to emit a "fatal error" event. The
        # heartbeat error event may also be emitted, but the process exit
        # event typically arrives first. Accept either message.
        fatal_errors = [e for e in events if e.get("type") == "error" and e.get("fatal")]
        assert len(fatal_errors) >= 1, "Expected fatal error from heartbeat timeout"

    finally:
        os.environ.pop("MOCK_HANG", None)
        sup_mod.HEARTBEAT_INTERVAL_SECONDS = orig_interval
        sup_mod.HEARTBEAT_TIMEOUT_SECONDS = orig_timeout
        await supervisor.shutdown(timeout=3)
