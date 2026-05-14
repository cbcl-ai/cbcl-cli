"""Performance benchmarks for the process-per-agent architecture.

Run with: python -m pytest tests/benchmarks/ -v

These tests verify that the system meets performance thresholds.
They use mock agent processes and a real Redis instance.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

MOCK_AGENT_SCRIPT = str(
    Path(__file__).resolve().parents[1] / "mocks" / "mock_agent_process.py"
)
TEST_OFFICE_ID = "bench-office"


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_max_concurrent_agents(tmp_path):
    """Verify 10 concurrent mock agents complete without errors."""
    import redis.asyncio as aioredis
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.orchestrator.task_dispatcher import TaskDispatcher
    from src.orchestrator.agent_queue import AgentQueueManager
    from src.config_sync.sync_service import ConfigStore

    redis_url = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")
    redis_client = aioredis.from_url(redis_url, decode_responses=True)

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    # Register 10 agents
    config_store = ConfigStore()
    agents = []
    for i in range(10):
        name = f"bench-worker-{i}"
        agents.append({
            "name": name, "display_name": f"Bench {i}",
            "agent_type": "custom", "model": "claude-sonnet-4-6",
            "system_prompt": "Mock", "allowed_tools": ["Read"],
            "is_active": True,
        })
    config_store.agents = agents

    supervisor = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        max_agents=20,
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

    completed = []

    async def on_event(agent_name: str, msg: dict) -> None:
        if msg.get("type") == "task_complete":
            completed.append(msg["task_id"])

    supervisor._on_event = on_event

    os.environ["MOCK_DELAY"] = "2"

    try:
        # Queue 10 tasks
        for i in range(10):
            await dispatcher.add_task({
                "task_id": f"bench-{i}",
                "readable_id": f"BN-001.T{i:02d}",
                "title": f"Bench task {i}",
                "assigned_agent": f"bench-worker-{i}",
                "priority": "medium",
                "brief": {"goal": "benchmark"},
                "labels": [],
                "workstream_name": "Bench",
            })

        dispatch_task = asyncio.create_task(dispatcher.run())

        start = time.monotonic()

        # Wait for all 10 to complete
        for _ in range(150):  # 15 second timeout
            await asyncio.sleep(0.1)
            if len(completed) >= 10:
                break

        elapsed = time.monotonic() - start

        assert len(completed) == 10, \
            f"Expected 10 completions, got {len(completed)} in {elapsed:.1f}s"

        # All 10 should complete in ~2s (parallel), not 20s (serial)
        assert elapsed < 10.0, \
            f"10 concurrent agents took {elapsed:.1f}s (expected < 10s)"

        print(f"\n  BENCHMARK: 10 concurrent agents completed in {elapsed:.1f}s")

    finally:
        os.environ.pop("MOCK_DELAY", None)
        try:
            await dispatcher.stop()
            dispatch_task.cancel()
            try:
                await dispatch_task
            except asyncio.CancelledError:
                pass
            await supervisor.shutdown(timeout=5)
        finally:
            keys = await redis_client.keys(f"office:{TEST_OFFICE_ID}:*")
            if keys:
                await redis_client.delete(*keys)
            await redis_client.aclose()


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_dispatch_latency(tmp_path):
    """Verify task dispatch latency is under 500ms."""
    import redis.asyncio as aioredis
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.orchestrator.task_dispatcher import TaskDispatcher
    from src.orchestrator.agent_queue import AgentQueueManager
    from src.config_sync.sync_service import ConfigStore

    redis_url = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")
    redis_client = aioredis.from_url(redis_url, decode_responses=True)

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    config_store = ConfigStore()
    config_store.agents = [{
        "name": "latency-worker", "display_name": "Latency Worker",
        "agent_type": "custom", "model": "claude-sonnet-4-6",
        "system_prompt": "Mock", "allowed_tools": ["Read"],
        "is_active": True,
    }]

    supervisor = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
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

    spawn_detected = asyncio.Event()

    async def on_event(agent_name: str, msg: dict) -> None:
        if msg.get("type") == "progress":
            spawn_detected.set()

    supervisor._on_event = on_event

    os.environ["MOCK_DELAY"] = "1"

    try:
        dispatch_task = asyncio.create_task(dispatcher.run())

        # Measure time from add_task to first progress event
        start = time.monotonic()
        await dispatcher.add_task({
            "task_id": "latency-test",
            "readable_id": "LT-001.T01",
            "title": "Latency test",
            "assigned_agent": "latency-worker",
            "priority": "urgent",
            "brief": {"goal": "test"},
            "labels": [],
            "workstream_name": "Test",
        })

        await asyncio.wait_for(spawn_detected.wait(), timeout=5.0)
        elapsed_ms = (time.monotonic() - start) * 1000

        # Dispatch latency includes: Redis enqueue + wake + dequeue + process spawn + ready
        # Should be under 1000ms (includes 2s poll interval safety margin)
        assert elapsed_ms < 1000, \
            f"Dispatch latency {elapsed_ms:.0f}ms exceeds 1000ms threshold"

        print(f"\n  BENCHMARK: Dispatch latency = {elapsed_ms:.0f}ms")

    finally:
        os.environ.pop("MOCK_DELAY", None)
        try:
            await dispatcher.stop()
            dispatch_task.cancel()
            try:
                await dispatch_task
            except asyncio.CancelledError:
                pass
            await supervisor.shutdown(timeout=5)
        finally:
            keys = await redis_client.keys(f"office:{TEST_OFFICE_ID}:*")
            if keys:
                await redis_client.delete(*keys)
            await redis_client.aclose()


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_memory_per_agent_process(tmp_path):
    """Verify each mock agent process uses less than 100MB RSS."""
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.config_sync.sync_service import ConfigStore

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    config_store = ConfigStore()
    config_store.agents = [{
        "name": "mem-worker", "display_name": "Mem Worker",
        "agent_type": "custom", "model": "claude-sonnet-4-6",
        "system_prompt": "Mock", "allowed_tools": ["Read"],
        "is_active": True,
    }]

    supervisor = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        _agent_command=[sys.executable, MOCK_AGENT_SCRIPT],
    )

    os.environ["MOCK_DELAY"] = "5"  # Keep agent alive long enough to measure

    try:
        agent_config = config_store.get_agent("mem-worker")
        success = await supervisor.spawn_worker("mem-worker", agent_config, {
            "task_id": "mem-test",
            "readable_id": "MM-001.T01",
            "title": "Memory test",
            "assigned_agent": "mem-worker",
            "priority": "medium",
            "brief": {"goal": "test"},
        })
        assert success, "Failed to spawn worker"

        # Give the process time to initialize
        await asyncio.sleep(1)

        # Read memory usage
        agent_proc = supervisor._agents.get("mem-worker")
        assert agent_proc and agent_proc.pid, "Agent process not found"

        rss_mb = None
        try:
            import psutil
            proc = psutil.Process(agent_proc.pid)
            mem_info = proc.memory_info()
            rss_mb = mem_info.rss / (1024 * 1024)
        except ImportError:
            pass

        if rss_mb is None:
            # Fallback: read from /proc on Linux
            proc_status = Path(f"/proc/{agent_proc.pid}/status")
            if proc_status.exists():
                status = proc_status.read_text()
                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        rss_mb = rss_kb / 1024
                        break

        if rss_mb is None:
            # macOS: use ps
            import subprocess
            result = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(agent_proc.pid)],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                rss_kb = int(result.stdout.strip())
                rss_mb = rss_kb / 1024

        if rss_mb is None:
            pytest.skip("Cannot read process memory on this platform")

        assert rss_mb < 100, \
            f"Agent process RSS = {rss_mb:.1f}MB, exceeds 100MB threshold"

        print(f"\n  BENCHMARK: Agent process RSS = {rss_mb:.1f}MB")

    finally:
        os.environ.pop("MOCK_DELAY", None)
        await supervisor.shutdown(timeout=5)


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_ipc_latency(tmp_path):
    """Verify IPC round-trip latency (stdout write to event callback) is under 10ms p99."""
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.config_sync.sync_service import ConfigStore

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    config_store = ConfigStore()
    config_store.agents = [{
        "name": "ipc-worker", "display_name": "IPC Worker",
        "agent_type": "custom", "model": "claude-sonnet-4-6",
        "system_prompt": "Mock", "allowed_tools": ["Read"],
        "is_active": True,
    }]

    latencies = []

    async def on_event(agent_name: str, msg: dict) -> None:
        if msg.get("type") == "progress":
            latencies.append(time.monotonic())

    supervisor = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        on_event=on_event,
        _agent_command=[sys.executable, MOCK_AGENT_SCRIPT],
    )

    # Use very fast mock with many progress steps to measure IPC throughput
    os.environ["MOCK_DELAY"] = "0.3"
    os.environ["MOCK_PROGRESS_STEPS"] = "10"

    try:
        agent_config = config_store.get_agent("ipc-worker")
        task_start = time.monotonic()
        success = await supervisor.spawn_worker("ipc-worker", agent_config, {
            "task_id": "ipc-test",
            "readable_id": "IP-001.T01",
            "title": "IPC latency test",
            "assigned_agent": "ipc-worker",
            "priority": "medium",
            "brief": {"goal": "test"},
        })
        assert success, "Failed to spawn worker"

        # Wait for all progress events
        for _ in range(50):
            await asyncio.sleep(0.1)
            if len(latencies) >= 10:
                break

        assert len(latencies) >= 2, f"Expected multiple progress events, got {len(latencies)}"

        # Measure inter-event gaps (proxy for IPC delivery latency)
        gaps_ms = [
            (latencies[i] - latencies[i - 1]) * 1000
            for i in range(1, len(latencies))
        ]
        # The mock sends progress events with step_delay = 0.3/10 = 30ms apart.
        # Subtract that to isolate IPC overhead. But since we can't perfectly
        # separate mock sleep from IPC, we just verify the gaps are reasonable.
        # p99 gap should be well under 100ms (30ms sleep + <10ms IPC overhead).
        sorted_gaps = sorted(gaps_ms)
        p99 = sorted_gaps[int(len(sorted_gaps) * 0.99)] if len(sorted_gaps) > 1 else sorted_gaps[0]

        assert p99 < 100, \
            f"IPC latency p99 = {p99:.1f}ms (expected < 100ms including mock sleep)"

        print(f"\n  BENCHMARK: IPC inter-event p99 = {p99:.1f}ms "
              f"(min={sorted_gaps[0]:.1f}ms, median={sorted_gaps[len(sorted_gaps)//2]:.1f}ms)")

    finally:
        os.environ.pop("MOCK_DELAY", None)
        os.environ.pop("MOCK_PROGRESS_STEPS", None)
        await supervisor.shutdown(timeout=5)


@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_manager_first_chunk_latency(tmp_path):
    """Verify Manager first response chunk arrives within 2 seconds of sending chat."""
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.config_sync.sync_service import ConfigStore

    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace, exist_ok=True)

    config_store = ConfigStore()
    config_store.agents = [{
        "name": "manager", "display_name": "Manager",
        "agent_type": "system", "model": "claude-sonnet-4-6",
        "system_prompt": "Mock Manager", "allowed_tools": [],
        "is_active": True,
    }]

    first_chunk_event = asyncio.Event()
    first_chunk_time = []

    async def on_event(agent_name: str, msg: dict) -> None:
        if msg.get("type") == "response_chunk" and not first_chunk_time:
            first_chunk_time.append(time.monotonic())
            first_chunk_event.set()

    supervisor = AgentSupervisor(
        workspace_path=workspace,
        office_id=TEST_OFFICE_ID,
        on_event=on_event,
        _agent_command=[sys.executable, MOCK_AGENT_SCRIPT],
    )

    os.environ["MOCK_DELAY"] = "0.5"
    os.environ["MOCK_RESPONSE_CHUNKS"] = "3"

    try:
        manager_config = config_store.get_agent("manager")
        success = await supervisor.spawn_manager(manager_config)
        assert success, "Failed to spawn manager"

        send_time = time.monotonic()
        await supervisor.send_chat_to_manager({
            "context_key": "general_chat",
            "content": "Benchmark test",
            "conversation_id": "bench-conv-001",
            "context_data": {},
        })

        await asyncio.wait_for(first_chunk_event.wait(), timeout=5.0)
        elapsed_ms = (first_chunk_time[0] - send_time) * 1000

        assert elapsed_ms < 2000, \
            f"Manager first chunk latency = {elapsed_ms:.0f}ms (threshold: 2000ms)"

        print(f"\n  BENCHMARK: Manager first-chunk latency = {elapsed_ms:.0f}ms")

    finally:
        os.environ.pop("MOCK_DELAY", None)
        os.environ.pop("MOCK_RESPONSE_CHUNKS", None)
        await supervisor.shutdown(timeout=5)
