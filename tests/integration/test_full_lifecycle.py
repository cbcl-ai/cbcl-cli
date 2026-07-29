"""Integration tests for the full task lifecycle.

Tests:
1. Happy path: task queued -> dispatched -> agent executes -> completes -> review
4. Manager chat: user sends message -> Manager responds with streaming
5. Rework flow: task returned from review -> re-queued -> agent picks up with feedback
8. Priority ordering: multiple tasks queued -> dispatched in priority order
"""

import asyncio
import os

import pytest

from tests.integration.conftest import TEST_OFFICE_ID


@pytest.mark.asyncio
async def test_happy_path_task_lifecycle(
    supervisor, dispatcher, config_store, mock_task_data, redis_client,
):
    """Scenario 1: Task queued -> dispatched -> executes -> completes."""
    events_received = []

    async def on_event(agent_name: str, msg: dict) -> None:
        events_received.append(msg)

    supervisor._on_event = on_event

    # Start the dispatcher loop
    dispatch_task = asyncio.create_task(dispatcher.run())

    try:
        # Add task to queue
        await dispatcher.add_task(mock_task_data)

        # Wait for completion (mock agent takes ~2 seconds)
        for _ in range(50):  # 5 second timeout
            await asyncio.sleep(0.1)
            completed = [e for e in events_received if e.get("type") == "task_complete"]
            if completed:
                break

        # Verify events
        assert any(e.get("type") == "progress" for e in events_received), \
            "Expected at least one progress event"
        assert any(e.get("type") == "task_complete" for e in events_received), \
            "Expected task_complete event"

        # Verify task_complete content
        complete_event = [e for e in events_received if e.get("type") == "task_complete"][0]
        assert complete_event["task_id"] == mock_task_data["task_id"]
        assert complete_event["status"] == "review"
        assert complete_event["token_cost"] >= 0

        # Verify agent is free after completion
        await asyncio.sleep(0.5)  # Allow cleanup
        assert not supervisor.is_agent_busy("mock-worker")

        # Verify task removed from queue
        queue_size = await dispatcher.get_queue_size()
        assert queue_size == 0

    finally:
        await dispatcher.stop()
        dispatch_task.cancel()
        try:
            await dispatch_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_manager_chat_streaming(
    supervisor, config_store,
):
    """Scenario 4: Manager chat streaming response."""
    events_received = []

    async def on_event(agent_name: str, msg: dict) -> None:
        events_received.append(msg)

    supervisor._on_event = on_event

    # Use fast mock
    os.environ["MOCK_DELAY"] = "0.5"
    os.environ["MOCK_RESPONSE_CHUNKS"] = "3"

    try:
        # Spawn manager
        manager_config = {
            "name": "manager",
            "display_name": "Manager",
            "agent_type": "system",
            "model": "claude-sonnet-4-6",
            "system_prompt": "You are the AI Manager.",
            "allowed_tools": [],
            "is_active": True,
        }
        success = await supervisor.spawn_manager(manager_config)
        assert success, "Failed to spawn manager"

        # Send chat message
        await supervisor.send_chat_to_manager({
            "context_key": "general_chat",
            "content": "Hello, Manager!",
            "conversation_id": "test-conv-001",
            "context_data": {},
        })

        # Wait for response chunks and final
        for _ in range(50):
            await asyncio.sleep(0.1)
            finals = [e for e in events_received if e.get("type") == "response_final"]
            if finals:
                break

        # Verify streaming chunks were received
        chunks = [e for e in events_received if e.get("type") == "response_chunk"]
        assert len(chunks) >= 1, f"Expected response chunks, got {len(chunks)}"

        # Verify final message
        finals = [e for e in events_received if e.get("type") == "response_final"]
        assert len(finals) == 1, "Expected exactly one response_final"
        assert finals[0]["token_cost"] >= 0
        assert finals[0]["conversation_id"] == "test-conv-001"

    finally:
        os.environ.pop("MOCK_DELAY", None)
        os.environ.pop("MOCK_RESPONSE_CHUNKS", None)


@pytest.mark.asyncio
async def test_rework_flow(
    supervisor, dispatcher, config_store, mock_task_data, redis_client,
):
    """Scenario 5: Task completed -> returned from review -> re-queued with feedback."""
    completions = []

    async def on_event(agent_name: str, msg: dict) -> None:
        if msg.get("type") == "task_complete":
            completions.append(msg)
            # Release the active-task marker so the same task_id can be
            # re-dispatched (mirrors handlers._on_agent_event, which calls
            # ``dispatcher.on_agent_complete(agent_name)`` — the old
            # ``on_task_complete(task_id)`` API no longer exists).
            await dispatcher.on_agent_complete(agent_name)

    supervisor._on_event = on_event

    os.environ["MOCK_DELAY"] = "0.5"

    dispatch_task = asyncio.create_task(dispatcher.run())

    try:
        # First execution
        await dispatcher.add_task(mock_task_data)

        for _ in range(50):
            await asyncio.sleep(0.1)
            if len(completions) >= 1:
                break

        assert len(completions) == 1, "First execution did not complete"

        # Wait for the worker process to fully exit and the monitor task to
        # transition the agent to IDLE so it can be re-spawned.
        for _ in range(30):
            await asyncio.sleep(0.1)
            if not supervisor.is_agent_busy("mock-worker"):
                break
        assert not supervisor.is_agent_busy("mock-worker"), \
            "Agent should be free after first task completes"

        # Simulate rework: the reviewer's ``review → ready`` return lands
        # on the (modeled) backend first — without it the dispatcher's
        # fresh-status pre-check drops the re-add as a stale
        # ``ready → in_progress`` mismatch (FX-24.T08 commits the move
        # BEFORE the spawn, so the stub map still says in_progress).
        dispatcher._stub_status[mock_task_data["task_id"]] = "ready"
        rework_data = {
            **mock_task_data,
            "rework_feedback": "Please fix the formatting issues.",
            "rework_count": 1,
        }
        await dispatcher.add_task(rework_data)

        for _ in range(100):  # 10 second timeout
            await asyncio.sleep(0.1)
            if len(completions) >= 2:
                break

        assert len(completions) == 2, "Rework execution did not complete"
        assert completions[1]["task_id"] == mock_task_data["task_id"]

    finally:
        os.environ.pop("MOCK_DELAY", None)
        await dispatcher.stop()
        dispatch_task.cancel()
        try:
            await dispatch_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_priority_ordering(
    supervisor, dispatcher, config_store, redis_client,
):
    """Scenario 8: Tasks dispatched in priority order."""
    completed_order = []

    async def on_event(agent_name: str, msg: dict) -> None:
        if msg.get("type") == "task_complete":
            completed_order.append(msg["task_id"])

    supervisor._on_event = on_event

    # Add tasks in reverse priority order (low first, urgent last)
    tasks = [
        {"task_id": "low-1", "readable_id": "T.T01", "title": "Low",
         "assigned_agent": "mock-worker", "priority": "low",
         "brief": {"goal": "test"}, "labels": [], "workstream_name": "Test"},
        {"task_id": "urgent-1", "readable_id": "T.T02", "title": "Urgent",
         "assigned_agent": "mock-worker", "priority": "urgent",
         "brief": {"goal": "test"}, "labels": [], "workstream_name": "Test"},
        {"task_id": "high-1", "readable_id": "T.T03", "title": "High",
         "assigned_agent": "mock-worker", "priority": "high",
         "brief": {"goal": "test"}, "labels": [], "workstream_name": "Test"},
    ]

    # Queue all tasks before starting dispatcher
    for task in tasks:
        await dispatcher.add_task(task)

    # Verify queue has all 3
    assert await dispatcher.get_queue_size() == 3

    # Start dispatcher with MOCK_DELAY=0.5 for fast execution
    os.environ["MOCK_DELAY"] = "0.5"

    dispatch_task = asyncio.create_task(dispatcher.run())

    try:
        # Wait for all 3 tasks to complete (sequential: agent handles one at a time)
        for _ in range(150):  # 15 second timeout
            await asyncio.sleep(0.1)
            if len(completed_order) >= 3:
                break

        assert len(completed_order) == 3, f"Expected 3 completions, got {len(completed_order)}"

        # Verify priority ordering: urgent first, then high, then low
        assert completed_order[0] == "urgent-1", f"Expected urgent first, got {completed_order[0]}"
        assert completed_order[1] == "high-1", f"Expected high second, got {completed_order[1]}"
        assert completed_order[2] == "low-1", f"Expected low third, got {completed_order[2]}"

    finally:
        os.environ.pop("MOCK_DELAY", None)
        await dispatcher.stop()
        dispatch_task.cancel()
        try:
            await dispatch_task
        except asyncio.CancelledError:
            pass
