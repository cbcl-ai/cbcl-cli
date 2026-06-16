"""Tests for agent_supervisor.py -- Process pool manager."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.agent_supervisor import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    SPAWN_TIMEOUT_SECONDS,
    AgentProcess,
    AgentState,
    AgentSupervisor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def supervisor():
    """Create an AgentSupervisor with no event callback."""
    return AgentSupervisor(
        workspace_path="/tmp/test-workspace",
        office_id="test-office-id",
        backend_url="http://localhost:8000",
        container_name="cbcl-office-test",
        max_agents=5,
    )


@pytest.fixture
def supervisor_with_callback():
    """Create an AgentSupervisor with a mock event callback."""
    callback = AsyncMock()
    sup = AgentSupervisor(
        workspace_path="/tmp/test-workspace",
        office_id="test-office-id",
        backend_url="http://localhost:8000",
        container_name="cbcl-office-test",
        max_agents=5,
        on_event=callback,
    )
    return sup, callback


def make_mock_process(
    pid: int = 100,
    returncode: int | None = None,
) -> MagicMock:
    """Create a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode if returncode is not None else 0)
    return proc


# ---------------------------------------------------------------------------
# AgentState tests
# ---------------------------------------------------------------------------


class TestAgentState:
    """Tests for the AgentState enum."""

    def test_all_states_exist(self) -> None:
        assert len(AgentState) == 5

    def test_state_values(self) -> None:
        assert AgentState.IDLE.value == "idle"
        assert AgentState.SPAWNING.value == "spawning"
        assert AgentState.READY.value == "ready"
        assert AgentState.WORKING.value == "working"
        assert AgentState.CRASHED.value == "crashed"


# ---------------------------------------------------------------------------
# AgentProcess tests
# ---------------------------------------------------------------------------


class TestAgentProcess:
    """Tests for the AgentProcess dataclass."""

    def test_default_state(self) -> None:
        agent = AgentProcess(agent_name="test", role="worker")
        assert agent.state == AgentState.IDLE
        assert agent.process is None
        assert agent.pid is None
        assert agent.current_task_id is None
        assert agent.reader_task is None
        assert agent.monitor_task is None
        assert agent.heartbeat_task is None


# ---------------------------------------------------------------------------
# State query tests
# ---------------------------------------------------------------------------


class TestStateQueries:
    """Tests for state query methods."""

    def test_active_count_empty(self, supervisor: AgentSupervisor) -> None:
        assert supervisor.active_count == 0

    def test_active_count_with_agents(
        self, supervisor: AgentSupervisor
    ) -> None:
        supervisor._agents["a1"] = AgentProcess(
            agent_name="a1", role="worker", state=AgentState.WORKING
        )
        supervisor._agents["a2"] = AgentProcess(
            agent_name="a2", role="worker", state=AgentState.IDLE
        )
        supervisor._agents["a3"] = AgentProcess(
            agent_name="a3", role="worker", state=AgentState.READY
        )
        assert supervisor.active_count == 2  # WORKING + READY

    def test_can_spawn_under_limit(
        self, supervisor: AgentSupervisor
    ) -> None:
        assert supervisor.can_spawn()

    def test_can_spawn_at_limit(
        self, supervisor: AgentSupervisor
    ) -> None:
        for i in range(5):
            supervisor._agents[f"agent-{i}"] = AgentProcess(
                agent_name=f"agent-{i}",
                role="worker",
                state=AgentState.WORKING,
            )
        assert not supervisor.can_spawn()

    def test_get_agent_state_unknown(
        self, supervisor: AgentSupervisor
    ) -> None:
        assert supervisor.get_agent_state("nonexistent") == AgentState.IDLE

    def test_get_agent_state_known(
        self, supervisor: AgentSupervisor
    ) -> None:
        supervisor._agents["test"] = AgentProcess(
            agent_name="test", role="worker", state=AgentState.WORKING
        )
        assert supervisor.get_agent_state("test") == AgentState.WORKING

    def test_is_agent_busy(self, supervisor: AgentSupervisor) -> None:
        supervisor._agents["a"] = AgentProcess(
            agent_name="a", role="worker", state=AgentState.SPAWNING
        )
        supervisor._agents["b"] = AgentProcess(
            agent_name="b", role="worker", state=AgentState.READY
        )
        supervisor._agents["c"] = AgentProcess(
            agent_name="c", role="worker", state=AgentState.WORKING
        )
        supervisor._agents["d"] = AgentProcess(
            agent_name="d", role="worker", state=AgentState.IDLE
        )
        supervisor._agents["e"] = AgentProcess(
            agent_name="e", role="worker", state=AgentState.CRASHED
        )
        assert supervisor.is_agent_busy("a")
        assert supervisor.is_agent_busy("b")
        assert supervisor.is_agent_busy("c")
        assert not supervisor.is_agent_busy("d")
        assert not supervisor.is_agent_busy("e")
        assert not supervisor.is_agent_busy("unknown")

    def test_get_all_statuses(self, supervisor: AgentSupervisor) -> None:
        supervisor._agents["analyst"] = AgentProcess(
            agent_name="analyst",
            role="worker",
            state=AgentState.WORKING,
            pid=123,
            current_task_id="t1",
            started_at=time.monotonic() - 60,
        )
        statuses = supervisor.get_all_statuses()
        assert "analyst" in statuses
        assert statuses["analyst"]["status"] == "working"
        assert statuses["analyst"]["pid"] == 123
        assert statuses["analyst"]["current_task"] == "t1"
        assert statuses["analyst"]["uptime"] >= 59


# ---------------------------------------------------------------------------
# Per-agent lock tests
# ---------------------------------------------------------------------------


class TestAgentLock:
    """Tests for per-agent locking."""

    def test_get_lock_creates_new(
        self, supervisor: AgentSupervisor
    ) -> None:
        lock = supervisor._get_lock("agent-1")
        assert isinstance(lock, asyncio.Lock)

    def test_get_lock_returns_same(
        self, supervisor: AgentSupervisor
    ) -> None:
        lock1 = supervisor._get_lock("agent-1")
        lock2 = supervisor._get_lock("agent-1")
        assert lock1 is lock2

    def test_different_agents_different_locks(
        self, supervisor: AgentSupervisor
    ) -> None:
        lock1 = supervisor._get_lock("agent-1")
        lock2 = supervisor._get_lock("agent-2")
        assert lock1 is not lock2


# ---------------------------------------------------------------------------
# spawn_worker tests
# ---------------------------------------------------------------------------


class TestSpawnWorker:
    """Tests for spawn_worker()."""

    @pytest.mark.asyncio
    async def test_spawn_rejects_busy_agent(
        self, supervisor: AgentSupervisor
    ) -> None:
        supervisor._agents["analyst"] = AgentProcess(
            agent_name="analyst", role="worker", state=AgentState.WORKING
        )
        result = await supervisor.spawn_worker(
            "analyst", {}, {"task_id": "t1"}
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_spawn_rejects_at_limit(
        self, supervisor: AgentSupervisor
    ) -> None:
        for i in range(5):
            supervisor._agents[f"a{i}"] = AgentProcess(
                agent_name=f"a{i}",
                role="worker",
                state=AgentState.WORKING,
            )
        result = await supervisor.spawn_worker(
            "new-agent", {}, {"task_id": "t1"}
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_spawn_crashes_on_exec_failure(
        self, supervisor: AgentSupervisor
    ) -> None:
        """spawn_worker transitions to CRASHED when exec fails."""

        async def fake_exec(*args, **kwargs):
            raise RuntimeError("Not actually spawning")

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_exec
        ):
            result = await supervisor.spawn_worker(
                "analyst", {}, {"task_id": "t1"}
            )

        assert result is False
        agent = supervisor._agents.get("analyst")
        assert agent is not None
        assert agent.state == AgentState.CRASHED

    @pytest.mark.asyncio
    async def test_spawn_worker_success(
        self, supervisor: AgentSupervisor
    ) -> None:
        """Full happy path: spawn, wait ready, assign task, verify state."""
        proc = make_mock_process(pid=200)
        proc.returncode = None

        # Simulate stdout: ready message followed by EOF
        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"type":"ready","pid":200,"agent_name":"analyst"}\n'
        )
        proc.stdout = reader

        async def fake_exec(*args, **kwargs):
            return proc

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_exec
        ):
            result = await supervisor.spawn_worker(
                "analyst",
                {"model": "claude-sonnet-4-6"},
                {"task_id": "t1", "readable_id": "WR-001.T01"},
            )

        assert result is True
        agent = supervisor._agents["analyst"]
        assert agent.state == AgentState.WORKING
        assert agent.current_task_id == "t1"
        assert agent.current_readable_id == "WR-001.T01"
        assert agent.pid == 200

        # Verify assign_task was written to stdin
        proc.stdin.write.assert_called()
        written = proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode().strip())
        assert msg["type"] == "assign_task"
        assert msg["task_id"] == "t1"

        # Verify background tasks are running
        assert agent.reader_task is not None
        assert agent.monitor_task is not None
        assert agent.heartbeat_task is not None

        # Cleanup: cancel background tasks to avoid warnings
        for t in (agent.reader_task, agent.monitor_task, agent.heartbeat_task):
            if t and not t.done():
                t.cancel()


# ---------------------------------------------------------------------------
# spawn_manager tests
# ---------------------------------------------------------------------------


class TestSpawnManager:
    """Tests for spawn_manager()."""

    @pytest.mark.asyncio
    async def test_spawn_returns_true_if_already_running(
        self, supervisor: AgentSupervisor
    ) -> None:
        supervisor._agents["manager"] = AgentProcess(
            agent_name="manager", role="manager", state=AgentState.READY
        )
        result = await supervisor.spawn_manager({})
        assert result is True

    @pytest.mark.asyncio
    async def test_spawn_manager_success(
        self, supervisor: AgentSupervisor
    ) -> None:
        """Full happy path: spawn Manager, wait ready, verify READY state."""
        proc = make_mock_process(pid=300)
        proc.returncode = None

        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"type":"ready","pid":300,"agent_name":"manager"}\n'
        )
        proc.stdout = reader

        async def fake_exec(*args, **kwargs):
            return proc

        with patch(
            "asyncio.create_subprocess_exec", side_effect=fake_exec
        ):
            result = await supervisor.spawn_manager(
                {"model": "claude-opus-4-6"}
            )

        assert result is True
        agent = supervisor._agents["manager"]
        assert agent.state == AgentState.READY
        assert agent.pid == 300
        assert agent.role == "manager"

        # Manager should NOT have a current_task_id
        assert agent.current_task_id is None

        # Background tasks should be running
        assert agent.reader_task is not None
        assert agent.monitor_task is not None
        assert agent.heartbeat_task is not None

        # Cleanup
        for t in (agent.reader_task, agent.monitor_task, agent.heartbeat_task):
            if t and not t.done():
                t.cancel()

    @pytest.mark.asyncio
    async def test_spawn_handles_exec_failure(
        self, supervisor: AgentSupervisor
    ) -> None:
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("No such file"),
        ):
            result = await supervisor.spawn_manager({})
        assert result is False
        agent = supervisor._agents.get("manager")
        assert agent is not None
        assert agent.state == AgentState.CRASHED


# ---------------------------------------------------------------------------
# send_chat_to_manager tests
# ---------------------------------------------------------------------------


class TestSendChatToManager:
    """Tests for send_chat_to_manager()."""

    @pytest.mark.asyncio
    async def test_raises_if_manager_not_running(
        self, supervisor: AgentSupervisor
    ) -> None:
        with pytest.raises(RuntimeError, match="Manager process is not running"):
            await supervisor.send_chat_to_manager({"content": "hello"})

    @pytest.mark.asyncio
    async def test_raises_if_manager_idle(
        self, supervisor: AgentSupervisor
    ) -> None:
        supervisor._agents["manager"] = AgentProcess(
            agent_name="manager", role="manager", state=AgentState.IDLE
        )
        with pytest.raises(RuntimeError):
            await supervisor.send_chat_to_manager({"content": "hello"})

    @pytest.mark.asyncio
    async def test_sends_to_ready_manager(
        self, supervisor: AgentSupervisor
    ) -> None:
        proc = make_mock_process()
        supervisor._agents["manager"] = AgentProcess(
            agent_name="manager",
            role="manager",
            state=AgentState.READY,
            process=proc,
            pid=100,
        )

        await supervisor.send_chat_to_manager({
            "context_key": "general_chat",
            "content": "hello",
            "conversation_id": "c1",
        })

        # Verify stdin.write was called
        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode().strip())
        assert msg["type"] == "chat_message"
        assert msg["content"] == "hello"

        # Manager should transition to WORKING
        assert supervisor._agents["manager"].state == AgentState.WORKING

    @pytest.mark.asyncio
    async def test_sends_to_working_manager(
        self, supervisor: AgentSupervisor
    ) -> None:
        """Manager in WORKING state should accept additional chat messages."""
        proc = make_mock_process()
        supervisor._agents["manager"] = AgentProcess(
            agent_name="manager",
            role="manager",
            state=AgentState.WORKING,
            process=proc,
            pid=100,
        )

        await supervisor.send_chat_to_manager({
            "context_key": "workstream:ws-1",
            "content": "follow up",
            "conversation_id": "c2",
        })

        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode().strip())
        assert msg["type"] == "chat_message"
        assert msg["content"] == "follow up"
        assert supervisor._agents["manager"].state == AgentState.WORKING


# ---------------------------------------------------------------------------
# _reader_loop tests (Amendment C-2)
# ---------------------------------------------------------------------------


class TestReaderLoop:
    """Tests for _reader_loop() -- dedicated reader per process."""

    @pytest.mark.asyncio
    async def test_reads_and_dispatches_messages(
        self,
    ) -> None:
        """Reader loop should read messages and call on_event."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            last_message_at=time.monotonic(),
        )

        # Create a stream with messages
        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"type":"progress","task_id":"t1","event_type":"checkpoint","content":"step 1"}\n'
            b'{"type":"task_complete","task_id":"t1","status":"review","comment":"done","token_cost":0.5,"session_id":"s1"}\n'
        )
        reader.feed_eof()

        await supervisor._reader_loop("test", reader)

        # on_event should have been called for progress and task_complete
        # (ready is handled internally and not forwarded)
        assert callback.call_count == 2
        assert callback.call_args_list[0][0][0] == "test"
        assert callback.call_args_list[0][0][1]["type"] == "progress"
        assert callback.call_args_list[1][0][1]["type"] == "task_complete"

    @pytest.mark.asyncio
    async def test_ready_handled_internally(self) -> None:
        """Ready message should set state to READY, NOT forwarded."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.SPAWNING,
        )

        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"type":"ready","pid":100,"agent_name":"test"}\n'
        )
        reader.feed_eof()

        await supervisor._reader_loop("test", reader)

        # State should be READY
        assert supervisor._agents["test"].state == AgentState.READY
        # on_event should NOT be called for ready
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_malformed_json(self) -> None:
        """Non-JSON lines should be skipped without error."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            last_message_at=time.monotonic(),
        )

        reader = asyncio.StreamReader()
        reader.feed_data(
            b'NOT JSON\n'
            b'{"type":"progress","content":"valid"}\n'
            b'ALSO NOT JSON\n'
        )
        reader.feed_eof()

        await supervisor._reader_loop("test", reader)

        # Only the valid non-PONG message should be forwarded.
        # P6.10 v2: PONGs are now consumed internally by the
        # heartbeat machinery and never reach on_event, so this
        # test uses "progress" instead.
        assert callback.call_count == 1
        assert callback.call_args_list[0][0][1]["type"] == "progress"

    @pytest.mark.asyncio
    async def test_task_complete_transitions_to_idle(self) -> None:
        """task_complete should clear task and transition to IDLE (not READY).

        Workers are not long-lived — each task gets a fresh process.
        IDLE allows the dispatcher to spawn a new process for the next task.
        """
        supervisor = AgentSupervisor(
            workspace_path="/tmp", office_id="test"
        )
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            current_task_id="t1",
            current_readable_id="WR-001.T01",
            last_message_at=time.monotonic(),
        )

        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"type":"task_complete","task_id":"t1","status":"review","comment":"done","token_cost":0.5,"session_id":"s1"}\n'
        )
        reader.feed_eof()

        await supervisor._reader_loop("test", reader)

        agent = supervisor._agents["test"]
        assert agent.state == AgentState.IDLE
        assert agent.current_task_id is None
        assert agent.current_readable_id is None

    @pytest.mark.asyncio
    async def test_response_final_transitions_to_ready(self) -> None:
        """response_final should transition Manager back to READY."""
        supervisor = AgentSupervisor(
            workspace_path="/tmp", office_id="test"
        )
        supervisor._agents["manager"] = AgentProcess(
            agent_name="manager",
            role="manager",
            state=AgentState.WORKING,
            last_message_at=time.monotonic(),
        )

        reader = asyncio.StreamReader()
        reader.feed_data(
            b'{"type":"response_final","conversation_id":"c1","context_key":"gc","token_cost":0.1,"session_id":"s1"}\n'
        )
        reader.feed_eof()

        await supervisor._reader_loop("manager", reader)

        assert supervisor._agents["manager"].state == AgentState.READY

    @pytest.mark.asyncio
    async def test_updates_last_message_at(self) -> None:
        """Every valid message should update last_message_at."""
        supervisor = AgentSupervisor(
            workspace_path="/tmp", office_id="test"
        )
        old_time = time.monotonic() - 100
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            last_message_at=old_time,
        )

        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"pong"}\n')
        reader.feed_eof()

        await supervisor._reader_loop("test", reader)

        assert supervisor._agents["test"].last_message_at > old_time

    @pytest.mark.asyncio
    async def test_pong_bumps_last_pong_at_and_does_not_emit(self) -> None:
        """P6.10 v2: PONG messages bump last_pong_at AND are consumed
        internally — never reach the on_event callback."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp", office_id="test", on_event=callback,
        )
        old_pong = time.monotonic() - 100
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            last_message_at=time.monotonic(),
            last_pong_at=old_pong,
        )

        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"pong"}\n')
        reader.feed_eof()

        await supervisor._reader_loop("test", reader)

        # last_pong_at advanced.
        assert supervisor._agents["test"].last_pong_at > old_pong
        # And the message was NOT forwarded — heartbeat is internal.
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_ready_seeds_initial_last_pong_at(self) -> None:
        """P6.10 v2: 'ready' is treated as the initial PONG so the
        first heartbeat tick has a valid baseline instead of comparing
        against 0."""
        supervisor = AgentSupervisor(
            workspace_path="/tmp", office_id="test"
        )
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.SPAWNING,
            last_message_at=time.monotonic(),
            last_pong_at=0.0,  # explicitly unset
        )

        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"ready"}\n')
        reader.feed_eof()

        await supervisor._reader_loop("test", reader)

        assert supervisor._agents["test"].last_pong_at > 0.0
        assert supervisor._agents["test"].state == AgentState.READY


# ---------------------------------------------------------------------------
# _monitor_exit tests
# ---------------------------------------------------------------------------


class TestMonitorExit:
    """Tests for _monitor_exit() crash detection."""

    @pytest.mark.asyncio
    async def test_clean_exit_transitions_to_idle(
        self, supervisor: AgentSupervisor
    ) -> None:
        proc = make_mock_process(returncode=0)
        proc.wait = AsyncMock(return_value=0)
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc,
            pid=100,
        )

        await supervisor._monitor_exit("test")

        agent = supervisor._agents["test"]
        assert agent.state == AgentState.IDLE
        assert agent.process is None
        assert agent.pid is None

    @pytest.mark.asyncio
    async def test_crash_transitions_to_crashed(self) -> None:
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )

        proc = make_mock_process(returncode=1)
        proc.wait = AsyncMock(return_value=1)
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc,
            pid=100,
            current_task_id="t1",
        )

        await supervisor._monitor_exit("test")

        agent = supervisor._agents["test"]
        assert agent.state == AgentState.CRASHED
        assert agent.exit_code == 1

        # Should notify via callback
        callback.assert_called_once()
        call_args = callback.call_args[0]
        assert call_args[0] == "test"
        assert call_args[1]["type"] == "error"
        assert call_args[1]["fatal"] is True
        assert call_args[1]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_crash_without_task_no_callback(self) -> None:
        """Crash without current_task_id should not call on_event."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )

        proc = make_mock_process(returncode=1)
        proc.wait = AsyncMock(return_value=1)
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.READY,
            process=proc,
            pid=100,
            current_task_id=None,
        )

        await supervisor._monitor_exit("test")

        callback.assert_not_called()


# ---------------------------------------------------------------------------
# _kill_process tests
# ---------------------------------------------------------------------------


class TestKillProcess:
    """Tests for _kill_process()."""

    @pytest.mark.asyncio
    async def test_kill_terminates_then_kills(
        self, supervisor: AgentSupervisor
    ) -> None:
        proc = make_mock_process()
        # Simulate process not exiting after SIGTERM
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            process=proc,
            pid=100,
        )

        # Override wait to succeed on second call (after kill)
        call_count = 0

        async def smart_wait():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.TimeoutError
            return -9

        proc.wait = smart_wait

        await supervisor._kill_process("test")

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_handles_process_lookup_error(
        self, supervisor: AgentSupervisor
    ) -> None:
        proc = make_mock_process()
        proc.terminate = MagicMock(side_effect=ProcessLookupError)
        supervisor._agents["test"] = AgentProcess(
            agent_name="test",
            role="worker",
            process=proc,
            pid=100,
        )

        # Should not raise
        await supervisor._kill_process("test")

    @pytest.mark.asyncio
    async def test_kill_no_process(
        self, supervisor: AgentSupervisor
    ) -> None:
        """Kill with no agent should be a no-op."""
        await supervisor._kill_process("nonexistent")


# ---------------------------------------------------------------------------
# T1.1.8 (G19) — kill / current_task_id race: error events must carry
# the task_id even when _kill_process's reset wins the race
# ---------------------------------------------------------------------------


class TestKillTaskIdRace:
    """The fatal error events synthesized around a kill must carry the
    task_id. handlers.py gates ALL crash-recovery routing on
    ``if task_id:`` — a None task_id silently disables the crashed-
    reviewer urgent re-queue (leaving only the 60s reconciler)."""

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_event_carries_task_id(self) -> None:
        """Killing a WORKING agent via the heartbeat path emits an
        error event that carries the in-flight task_id, snapshotted
        BEFORE _kill_process resets the agent record."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )
        proc = make_mock_process(pid=100)
        proc.returncode = None
        proc.wait = AsyncMock(return_value=-15)
        agent = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc,
            pid=100,
            current_task_id="t-heartbeat",
            # Last PONG far enough in the past to trip the timeout
            # on the first tick.
            last_pong_at=time.monotonic() - HEARTBEAT_TIMEOUT_SECONDS - 10,
        )
        supervisor._agents["test"] = agent

        # Shrink the tick sleep so the test doesn't wait 30s.
        with patch(
            "src.orchestrator.agent_supervisor.HEARTBEAT_INTERVAL_SECONDS",
            0.01,
        ):
            await supervisor._heartbeat_loop("test")

        callback.assert_called_once()
        name, event = callback.call_args[0]
        assert name == "test"
        assert event["type"] == "error"
        assert event["fatal"] is True
        assert event["reason"] == "heartbeat_timeout"
        assert event["task_id"] == "t-heartbeat"
        # The kill DID reset the live pointer — the snapshot is what
        # preserved the id on the event.
        assert agent.current_task_id is None

    @pytest.mark.asyncio
    async def test_monitor_exit_carries_task_id_when_kill_reset_wins(
        self,
    ) -> None:
        """Simulate the racing interleaving: _monitor_exit is parked on
        process.wait() while _kill_process runs to completion FIRST and
        nulls ``current_task_id``. The monitor's fatal error event must
        still carry the task_id via the frozen ``killed_task_id``."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )

        exit_evt = asyncio.Event()
        proc = make_mock_process(pid=100)
        proc.returncode = None
        wait_calls = 0

        async def proc_wait():
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                # The monitor's wait: parked until we release it,
                # AFTER the killer's continuation has fully run.
                await exit_evt.wait()
                return -15
            # The killer's wait: process reaps immediately.
            return -15

        proc.wait = proc_wait

        agent = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc,
            pid=100,
            current_task_id="t-race",
        )
        supervisor._agents["test"] = agent

        monitor = asyncio.create_task(supervisor._monitor_exit("test"))
        await asyncio.sleep(0)  # monitor is now awaiting proc.wait()

        # Killer wins the race: resets the agent record while the
        # monitor's continuation hasn't run yet.
        await supervisor._kill_process("test")
        assert agent.current_task_id is None  # the race condition
        assert agent.killed_task_id == "t-race"  # the frozen snapshot

        exit_evt.set()
        await monitor

        callback.assert_called_once()
        name, event = callback.call_args[0]
        assert name == "test"
        assert event["type"] == "error"
        assert event["fatal"] is True
        assert event["task_id"] == "t-race"

    @pytest.mark.asyncio
    async def test_task_complete_then_kill_does_not_resurrect_task(
        self,
    ) -> None:
        """A task_complete clears ``current_task_id`` BEFORE any kill;
        the kill must NOT freeze a stale id, so a late non-zero exit
        never emits a fatal error event for the finished task."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )
        proc = make_mock_process(pid=100)
        proc.returncode = None
        proc.wait = AsyncMock(return_value=1)
        agent = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc,
            pid=100,
            current_task_id=None,  # task_complete already cleared it
        )
        supervisor._agents["test"] = agent

        await supervisor._kill_process("test")
        assert agent.killed_task_id is None

        # Re-attach a process handle to drive _monitor_exit's
        # crashed-exit branch directly.
        agent.process = proc
        await supervisor._monitor_exit("test")

        # No task in flight → no crash-recovery event at all.
        callback.assert_not_called()


# ---------------------------------------------------------------------------
# Event-hygiene Issue 3 — _monitor_exit must never mutate a REPLACEMENT
# AgentProcess; Issue 4 — kill-path state + double-emission dedupe
# ---------------------------------------------------------------------------


class TestMonitorExitIdentity:
    """``_monitor_exit`` receives the AgentProcess record it was started
    for; if a fresh spawn replaced ``self._agents[name]`` while it was
    parked on ``process.wait()``, it must NOT mutate the replacement's
    state — but must still emit the exit/error event for ITS process
    with ITS task snapshot."""

    @pytest.mark.asyncio
    async def test_replaced_record_state_untouched_event_still_emitted(
        self,
    ) -> None:
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )

        exit_evt = asyncio.Event()
        proc_a = make_mock_process(pid=100)
        proc_a.returncode = None

        async def wait_a():
            await exit_evt.wait()
            return 1

        proc_a.wait = wait_a

        agent_a = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc_a,
            pid=100,
            current_task_id="t-A",
        )
        supervisor._agents["test"] = agent_a

        monitor = asyncio.create_task(
            supervisor._monitor_exit("test", agent_a),
        )
        await asyncio.sleep(0)  # monitor parks on proc_a.wait()

        # A new spawn replaces the registry entry with a fresh record.
        proc_b = make_mock_process(pid=200)
        proc_b.returncode = None
        agent_b = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc_b,
            pid=200,
            current_task_id="t-B",
        )
        supervisor._agents["test"] = agent_b

        # A's process exits with a crash code; A's monitor fires.
        exit_evt.set()
        await monitor

        # B's state is completely untouched.
        assert agent_b.state == AgentState.WORKING
        assert agent_b.process is proc_b
        assert agent_b.pid == 200
        assert agent_b.current_task_id == "t-B"

        # A's fatal error event was still emitted, with A's task.
        callback.assert_called_once()
        name, event = callback.call_args[0]
        assert name == "test"
        assert event["type"] == "error"
        assert event["fatal"] is True
        assert event["task_id"] == "t-A"

    @pytest.mark.asyncio
    async def test_kill_initiated_exit_does_not_flip_to_crashed(
        self,
    ) -> None:
        """Issue 4: after _kill_process reset the agent to IDLE, the
        monitor observing the (killer-initiated) non-zero exit must not
        overwrite the state with CRASHED — but the T1.1.8 error event
        (with the frozen killed_task_id) still goes out for non-
        heartbeat kill paths."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )

        exit_evt = asyncio.Event()
        proc = make_mock_process(pid=100)
        proc.returncode = None
        wait_calls = 0

        async def proc_wait():
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                # The monitor's wait: parked until the kill finished.
                await exit_evt.wait()
                return -15
            return -15  # the killer's wait

        proc.wait = proc_wait

        agent = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc,
            pid=100,
            current_task_id="t-kill",
        )
        supervisor._agents["test"] = agent

        monitor = asyncio.create_task(
            supervisor._monitor_exit("test", agent),
        )
        await asyncio.sleep(0)

        await supervisor._kill_process("test")
        assert agent.state == AgentState.IDLE
        assert agent.kill_initiated is True

        exit_evt.set()
        await monitor

        # State stays as the killer set it — no misleading CRASHED.
        assert agent.state == AgentState.IDLE
        # The crash-recovery event still fired (no prior emission).
        callback.assert_called_once()
        _, event = callback.call_args[0]
        assert event["task_id"] == "t-kill"

    @pytest.mark.asyncio
    async def test_heartbeat_kill_emits_exactly_one_fatal_error(
        self,
    ) -> None:
        """Issue 4 dedupe: a WORKING agent killed by the heartbeat used
        to emit TWO fatal errors (heartbeat + monitor). The monitor now
        skips its emission when the heartbeat already emitted for the
        same process+task."""
        callback = AsyncMock()
        supervisor = AgentSupervisor(
            workspace_path="/tmp",
            office_id="test",
            on_event=callback,
        )

        exit_evt = asyncio.Event()
        proc = make_mock_process(pid=100)
        proc.returncode = None
        wait_calls = 0

        async def proc_wait():
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                await exit_evt.wait()  # the monitor's wait
                return -15
            return -15  # the killer's wait

        proc.wait = proc_wait

        agent = AgentProcess(
            agent_name="test",
            role="worker",
            state=AgentState.WORKING,
            process=proc,
            pid=100,
            current_task_id="t-hb",
            last_pong_at=time.monotonic() - HEARTBEAT_TIMEOUT_SECONDS - 10,
        )
        supervisor._agents["test"] = agent

        monitor = asyncio.create_task(
            supervisor._monitor_exit("test", agent),
        )
        await asyncio.sleep(0)

        with patch(
            "src.orchestrator.agent_supervisor.HEARTBEAT_INTERVAL_SECONDS",
            0.01,
        ):
            await supervisor._heartbeat_loop("test")

        exit_evt.set()
        await monitor

        # Exactly ONE fatal error event for the whole kill sequence —
        # the heartbeat's, carrying the snapshot.
        callback.assert_called_once()
        name, event = callback.call_args[0]
        assert name == "test"
        assert event["reason"] == "heartbeat_timeout"
        assert event["task_id"] == "t-hb"


# ---------------------------------------------------------------------------
# Heartbeat tests (Amendment A4)
# ---------------------------------------------------------------------------


class TestHeartbeat:
    """Tests for heartbeat configuration (Amendment A4)."""

    def test_heartbeat_interval_is_30_seconds(self) -> None:
        assert HEARTBEAT_INTERVAL_SECONDS == 30

    def test_heartbeat_timeout_is_90_seconds(self) -> None:
        assert HEARTBEAT_TIMEOUT_SECONDS == 90

    def test_timeout_is_three_intervals(self) -> None:
        """Timeout should be exactly 3x the interval (3 missed pings)."""
        assert HEARTBEAT_TIMEOUT_SECONDS == 3 * HEARTBEAT_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Shutdown tests
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for shutdown()."""

    @pytest.mark.asyncio
    async def test_shutdown_empty(
        self, supervisor: AgentSupervisor
    ) -> None:
        """Shutdown with no agents should complete immediately."""
        await supervisor.shutdown(timeout=1)
        assert len(supervisor._agents) == 0

    @pytest.mark.asyncio
    async def test_shutdown_sends_to_all_agents(
        self, supervisor: AgentSupervisor
    ) -> None:
        # returncode starts as None (process running), transitions to 0
        # when wait() is called (simulating graceful exit after shutdown msg)
        proc1 = make_mock_process(pid=100)
        proc1.returncode = None

        async def proc1_wait():
            proc1.returncode = 0
            return 0

        proc1.wait = proc1_wait

        proc2 = make_mock_process(pid=101)
        proc2.returncode = None

        async def proc2_wait():
            proc2.returncode = 0
            return 0

        proc2.wait = proc2_wait

        supervisor._agents["agent1"] = AgentProcess(
            agent_name="agent1",
            role="worker",
            state=AgentState.WORKING,
            process=proc1,
            pid=100,
        )
        supervisor._agents["agent2"] = AgentProcess(
            agent_name="agent2",
            role="worker",
            state=AgentState.READY,
            process=proc2,
            pid=101,
        )

        await supervisor.shutdown(timeout=5)

        # All agents should be cleared
        assert len(supervisor._agents) == 0

        # Both processes should have received stdin writes (shutdown msg)
        assert proc1.stdin.write.called
        assert proc2.stdin.write.called


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for module-level constants."""

    def test_spawn_timeout(self) -> None:
        assert SPAWN_TIMEOUT_SECONDS == 30

    def test_shutdown_grace(self) -> None:
        assert SHUTDOWN_GRACE_SECONDS == 30

    def test_default_max_agents(self) -> None:
        # This may be overridden by env var, but default is 20
        from src.orchestrator.agent_supervisor import DEFAULT_MAX_AGENTS

        assert DEFAULT_MAX_AGENTS >= 1


# ---------------------------------------------------------------------------
# P2.5 — IPC hardening tests (stdin lock, drain timeout, _wait_for_ready
# crash-aware path, _on_event timeout)
# ---------------------------------------------------------------------------


class TestStdinWriteLock:
    """P2-B: concurrent ``_send_to_agent`` calls must not interleave bytes."""

    @pytest.mark.asyncio
    async def test_concurrent_sends_keep_ndjson_intact(
        self, supervisor: AgentSupervisor,
    ) -> None:
        """Two concurrent _send_to_agent calls produce two well-formed
        NDJSON lines, never interleaved bytes."""
        # Custom mock: collect every write into a buffer so we can
        # check that each line is a single, complete JSON object.
        proc = MagicMock()
        proc.pid = 100
        proc.returncode = None
        proc.stdin = MagicMock()
        buffer: list[bytes] = []
        proc.stdin.write = lambda b: buffer.append(b)

        async def _slow_drain():
            # Simulate a kernel pipe drain that yields control,
            # forcing the two coroutines to interleave at the
            # asyncio level. Without the write lock, the second
            # coroutine's `write` could land between the first's
            # `write` and `drain`.
            await asyncio.sleep(0.01)

        proc.stdin.drain = _slow_drain

        agent = AgentProcess(
            agent_name="alice", role="worker",
            process=proc, state=AgentState.READY,
        )
        supervisor._agents["alice"] = agent

        # Fire two concurrent sends with distinct payloads.
        await asyncio.gather(
            supervisor._send_to_agent("alice", {"kind": "ping", "n": 1}),
            supervisor._send_to_agent("alice", {"kind": "chat", "n": 2}),
        )

        # Concatenate and split by newline. Each line must parse
        # as a complete JSON object.
        joined = b"".join(buffer).decode()
        lines = [ln for ln in joined.split("\n") if ln]
        assert len(lines) == 2
        for ln in lines:
            obj = json.loads(ln)
            assert "kind" in obj
            assert "n" in obj


class TestDrainTimeout:
    """P2.5-A: a hung stdin reader must not pin the supervisor."""

    @pytest.mark.asyncio
    async def test_send_to_agent_drain_timeout_marks_crashed(
        self, supervisor: AgentSupervisor,
    ) -> None:
        proc = MagicMock()
        proc.pid = 100
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()

        async def _hung_drain():
            await asyncio.sleep(60)  # would-block forever

        proc.stdin.drain = _hung_drain

        agent = AgentProcess(
            agent_name="bob", role="worker",
            process=proc, state=AgentState.READY,
        )
        supervisor._agents["bob"] = agent

        # The send should raise RuntimeError (not hang). We patch the
        # drain timeout to a tiny value via the wait_for in the
        # supervisor; testing the real 5 s would slow the suite.
        with patch(
            "src.orchestrator.agent_supervisor.asyncio.wait_for",
            wraps=asyncio.wait_for,
        ):
            # Override timeout via lambda? Simpler: rely on actual
            # 5 s being acceptable when running locally. Here we
            # short-circuit by replacing drain with one that raises
            # TimeoutError directly.
            pass

        async def _instant_timeout():
            raise asyncio.TimeoutError

        proc.stdin.drain = _instant_timeout
        # The supervisor wraps drain in wait_for; when the inner
        # coroutine raises TimeoutError immediately, wait_for
        # propagates it.
        with pytest.raises(RuntimeError, match="hung reader"):
            await supervisor._send_to_agent("bob", {"k": "v"})
        assert agent.state == AgentState.CRASHED


class TestWaitForReadyCrashAware:
    """P2-C + P2.5-B: _wait_for_ready raises on CRASHED / dead process,
    spawn callers convert to False, _kill_process is reached."""

    @pytest.mark.asyncio
    async def test_wait_for_ready_raises_on_crashed_state(
        self, supervisor: AgentSupervisor,
    ) -> None:
        proc = make_mock_process(returncode=None)
        agent = AgentProcess(
            agent_name="charlie", role="worker",
            process=proc, state=AgentState.SPAWNING,
        )
        supervisor._agents["charlie"] = agent

        # Flip to CRASHED concurrently with the wait.
        async def _crash_after_delay():
            await asyncio.sleep(0.05)
            agent.state = AgentState.CRASHED

        crash_task = asyncio.create_task(_crash_after_delay())
        try:
            with pytest.raises(RuntimeError, match="crashed during spawn"):
                await supervisor._wait_for_ready("charlie")
        finally:
            await crash_task

    @pytest.mark.asyncio
    async def test_wait_for_ready_raises_on_process_exit(
        self, supervisor: AgentSupervisor,
    ) -> None:
        proc = make_mock_process(returncode=1)
        agent = AgentProcess(
            agent_name="dave", role="worker",
            process=proc, state=AgentState.SPAWNING,
        )
        supervisor._agents["dave"] = agent

        with pytest.raises(RuntimeError, match="exited.*before becoming ready"):
            await supervisor._wait_for_ready("dave")


class TestOnEventTimeout:
    """P2-G + P2.5-D: slow callbacks must not pin the reader loop."""

    @pytest.mark.asyncio
    async def test_task_complete_callback_timeout_still_idles_agent(
        self, supervisor_with_callback,
    ) -> None:
        """When the _on_event callback times out, the agent still
        transitions to IDLE so the dispatcher isn't starved."""
        supervisor, callback = supervisor_with_callback

        # Make the callback hang for longer than the 30s timeout.
        # We patch wait_for to use a tiny timeout for test speed.
        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(60)

        callback.side_effect = _hang

        proc = make_mock_process()
        agent = AgentProcess(
            agent_name="eve", role="worker",
            process=proc, state=AgentState.WORKING,
            current_task_id="t-1",
        )
        supervisor._agents["eve"] = agent

        # Mock readline to feed one task_complete then return EOF.
        msg_line = json.dumps({
            "type": "task_complete", "task_id": "t-1", "status": "review",
        }).encode() + b"\n"
        eof = b""
        readlines = [msg_line, eof]

        async def fake_readline():
            return readlines.pop(0) if readlines else eof

        proc.stdout.readline = fake_readline

        # Patch wait_for inside the supervisor module to use 0.1 s
        # so the test doesn't sleep 30s.
        original_wait_for = asyncio.wait_for

        async def _short_wait_for(coro, timeout=None):
            return await original_wait_for(coro, timeout=0.1)

        with patch(
            "src.orchestrator.agent_supervisor.asyncio.wait_for",
            side_effect=_short_wait_for,
        ):
            await supervisor._reader_loop("eve", proc.stdout)

        # Agent must have transitioned to IDLE despite the callback
        # hanging — the dispatcher would otherwise see this slot as
        # blocked.
        assert agent.state == AgentState.IDLE


# ---------------------------------------------------------------------------
# Self-heal: reset agents stuck busy with no live process
# ---------------------------------------------------------------------------


class TestReconcileStuckAgents:
    """Regression: a reviewer/worker whose session was cancelled/shut down
    could be left at WORKING with no live process, so is_agent_busy() blocked
    every dispatch and its queued (review) task never ran."""

    def test_resets_working_agent_with_no_process(
        self, supervisor: AgentSupervisor
    ) -> None:
        supervisor._agents["qa-engineer"] = AgentProcess(
            agent_name="qa-engineer",
            role="worker",
            state=AgentState.WORKING,
            process=None,  # session was cancelled — no live process
            current_task_id="OLD-T07",
        )
        reset = supervisor.reconcile_stuck_agents()
        assert reset == ["qa-engineer"]
        agent = supervisor._agents["qa-engineer"]
        assert agent.state == AgentState.IDLE
        assert agent.current_task_id is None
        assert not supervisor.is_agent_busy("qa-engineer")

    def test_resets_working_agent_with_exited_process(
        self, supervisor: AgentSupervisor
    ) -> None:
        dead = MagicMock()
        dead.returncode = 137  # killed
        supervisor._agents["solution-architect"] = AgentProcess(
            agent_name="solution-architect",
            role="worker",
            state=AgentState.WORKING,
            process=dead,
            current_task_id="OLD",
        )
        assert supervisor.reconcile_stuck_agents() == ["solution-architect"]
        assert supervisor._agents["solution-architect"].state == AgentState.IDLE

    def test_leaves_genuinely_working_agent_alone(
        self, supervisor: AgentSupervisor
    ) -> None:
        alive = MagicMock()
        alive.returncode = None  # still running
        supervisor._agents["backend-engineer"] = AgentProcess(
            agent_name="backend-engineer",
            role="worker",
            state=AgentState.WORKING,
            process=alive,
            current_task_id="T99",
        )
        assert supervisor.reconcile_stuck_agents() == []
        assert supervisor._agents["backend-engineer"].state == AgentState.WORKING

    def test_ignores_idle_agents(self, supervisor: AgentSupervisor) -> None:
        supervisor._agents["x"] = AgentProcess(
            agent_name="x", role="worker", state=AgentState.IDLE
        )
        assert supervisor.reconcile_stuck_agents() == []


class _OversizedThenPongStdout:
    """readline() raises ValueError (oversized line) once, then a pong, EOF.
    Mirrors CPython's StreamReader behavior on an over-limit line (T8.1.2)."""

    def __init__(self):
        self.calls = 0

    async def readline(self):
        self.calls += 1
        if self.calls == 1:
            raise ValueError("Separator is not found, and chunk exceed the limit")
        if self.calls == 2:
            return b'{"type":"pong"}\n'
        return b""  # EOF


@pytest.mark.asyncio
async def test_reader_loop_survives_oversized_line(supervisor):
    """T8.1.2 — an oversized line (ValueError from readline) must be SKIPPED,
    not break the reader loop (which would stop pongs → heartbeat kills a
    healthy agent)."""
    stdout = _OversizedThenPongStdout()
    await supervisor._reader_loop("some-agent", stdout)
    assert stdout.calls == 3
