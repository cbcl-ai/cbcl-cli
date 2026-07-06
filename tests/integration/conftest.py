"""Integration test fixtures.

Provides:
- Real Redis connection (from docker compose)
- AgentSupervisor configured to use mock agent processes
- TaskDispatcher connected to Redis
- ConfigStore with mock agent configurations
- Automatic Redis cleanup after each test
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
import redis.asyncio as aioredis


REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")
MOCK_AGENT_SCRIPT = str(
    Path(__file__).resolve().parents[1] / "mocks" / "mock_agent_process.py"
)
TEST_OFFICE_ID = "test-office-integration"


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[aioredis.Redis, None]:
    """Provide a real Redis connection, cleaned up after each test."""
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    await client.ping()

    yield client

    # Cleanup: delete all keys for this test office
    keys = await client.keys(f"office:{TEST_OFFICE_ID}:*")
    if keys:
        await client.delete(*keys)
    # Also clean benchmark office keys
    bench_keys = await client.keys("office:bench-office:*")
    if bench_keys:
        await client.delete(*bench_keys)
    await client.aclose()


@pytest.fixture
def mock_agent_config() -> dict:
    """Agent configuration for a mock worker agent."""
    return {
        "name": "mock-worker",
        "display_name": "Mock Worker",
        "agent_type": "custom",
        "model": "claude-sonnet-4-6",
        "system_prompt": "You are a mock agent for testing.",
        "allowed_tools": ["Read", "Write"],
        "is_active": True,
    }


@pytest.fixture
def mock_task_data() -> dict:
    """Task data for a mock task."""
    return {
        "task_id": "test-task-001",
        "readable_id": "TS-001.T01",
        "title": "Test task",
        "assigned_agent": "mock-worker",
        "priority": "medium",
        "brief": {
            "goal": "Test the system",
            "context": "Integration test",
            "inputs": "None",
            "output_format": "Text",
            "acceptance_criteria": ["Test passes"],
            "allowed_tools": ["Read"],
            "required_skills": [],
            "risks_and_edge_cases": "None",
            "verification_steps": "Check output",
        },
        "labels": [],
        "workstream_name": "Test Stream",
    }


@pytest.fixture
def config_store(mock_agent_config: dict):
    """ConfigStore with a mock agent registered."""
    from src.config_sync.sync_service import ConfigStore

    store = ConfigStore()
    # ConfigStore.agents is a list[dict]; get_agent() searches by name
    store.agents = [mock_agent_config]
    return store


@pytest_asyncio.fixture
async def supervisor(tmp_path: Path) -> AsyncGenerator:
    """AgentSupervisor configured to spawn mock agent processes."""
    from src.orchestrator.agent_supervisor import AgentSupervisor

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    sup = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        backend_url="http://localhost:8000",
        container_name="cbcl-office-test",
        _agent_command=[sys.executable, MOCK_AGENT_SCRIPT],
    )

    yield sup

    # Cleanup: kill all remaining processes
    await sup.shutdown(timeout=5)


def stub_dispatcher_backend_calls(dispatcher) -> None:
    """Stub the three private dispatcher methods that round-trip to the
    backend, which the integration tests don't run.

    Integration tests construct ``TaskDispatcher`` either via the
    ``dispatcher`` fixture (which calls this for you) or directly
    (in which case the test must call this helper itself —
    ``test_concurrent_agents`` / ``test_crash_recovery`` /
    ``test_performance`` all do their own construction because they
    need custom supervisor configs).

    Stubbed methods:
    * ``_fetch_board_tasks`` → ``[]`` — startup full-sync becomes a
      no-op, preserving queue state pre-populated via ``add_task``.
    * ``_fetch_task_status`` → the in-memory ``_status`` map (default
      ``"ready"``) — models the backend status so the dispatcher's
      fresh-status pre-check stays consistent across ticks. FX-24.T08:
      the ready→in_progress move now PRECEDES the spawn and records the
      new status here, so a spawn-failure re-queue (AS in_progress)
      re-dispatches cleanly instead of being dropped as "stale".
    * ``_check_dependencies`` → ``True`` — no depends_on enforcement
      for the mock task data.
    * ``_move_and_assign`` → records ``_status`` + ``True`` /
      ``_assign_only`` → no-op — the real methods POST to the backend's
      ``/tool-call`` endpoint. FX-24.T08 made the ready path commit this
      move BEFORE spawning; a move failure now re-queues with no worker
      to kill (the old T1.1.2 kill-the-rogue-worker path is gone), and a
      spawn-failure-after-move re-queues AS in_progress for an immediate
      respawn-in-place.
    """
    # FX-24.T08: model the backend status so the dispatcher's fresh-status
    # pre-check stays consistent across dispatch ticks. The ready→in_progress
    # move now PRECEDES the spawn, and a spawn-failure re-queues the task AS
    # in_progress — _fetch_task_status must report that (not a hardcoded
    # "ready"), or the re-dispatch is dropped as a "stale" status mismatch.
    _status: dict[str, str] = {}

    async def _stub_fetch_board_tasks() -> list[dict]:
        return []

    async def _stub_fetch_task_status(task_id: str) -> str | None:
        return _status.get(task_id, "ready")

    async def _stub_check_dependencies(task_id: str) -> bool:
        return True

    async def _stub_move_and_assign(
        task_id: str, agent_name: str, new_status: str,
    ) -> bool:
        _status[task_id] = new_status
        return True

    async def _stub_assign_only(task_id: str, agent_name: str) -> None:
        return None

    dispatcher._fetch_board_tasks = _stub_fetch_board_tasks  # type: ignore[method-assign]
    dispatcher._fetch_task_status = _stub_fetch_task_status  # type: ignore[method-assign]
    dispatcher._check_dependencies = _stub_check_dependencies  # type: ignore[method-assign]
    dispatcher._move_and_assign = _stub_move_and_assign  # type: ignore[method-assign]
    dispatcher._assign_only = _stub_assign_only  # type: ignore[method-assign]


@pytest_asyncio.fixture
async def dispatcher(redis_client, supervisor, config_store):
    """TaskDispatcher connected to Redis and the mock supervisor.

    Stubs backend round-trips via :func:`stub_dispatcher_backend_calls`
    so the mock tasks dispatch end-to-end without a live platform.
    """
    from src.orchestrator.task_dispatcher import TaskDispatcher
    from src.orchestrator.agent_queue import AgentQueueManager

    queue_manager = AgentQueueManager(redis_client, TEST_OFFICE_ID)
    disp = TaskDispatcher(
        redis=redis_client,
        office_id=TEST_OFFICE_ID,
        supervisor=supervisor,
        config_store=config_store,
        queue_manager=queue_manager,
    )
    stub_dispatcher_backend_calls(disp)

    yield disp

    await disp.stop()
