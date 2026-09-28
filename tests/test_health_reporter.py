"""Tests for the Redis-based Health Reporter (P2-T11)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.health.reporter import (
    HEALTH_KEY_TTL_SECONDS,
    HealthReporter,
)


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = AsyncMock()
    redis.set = AsyncMock()
    return redis


@pytest.fixture
def mock_supervisor():
    """Mock AgentSupervisor."""
    supervisor = MagicMock()
    supervisor.get_all_statuses = MagicMock(return_value={
        "analyst": {
            "status": "idle",
            "pid": None,
            "current_task": None,
            "uptime": 0,
        },
        "python-developer": {
            "status": "working",
            "pid": 12345,
            "current_task": "task-uuid-1",
            "uptime": 300.5,
        },
    })
    return supervisor


@pytest.fixture
def mock_dispatcher():
    """Mock TaskDispatcher."""
    dispatcher = AsyncMock()
    dispatcher.get_queue_size = AsyncMock(return_value=3)
    return dispatcher


@pytest.fixture
def mock_sessions():
    """Mock SessionManager."""
    sessions = MagicMock()
    sessions.manager_sessions = {
        "general_chat": "gc-session-123",
    }
    return sessions


@pytest.fixture
def mock_script_runner():
    """Mock ScriptRunner."""
    runner = AsyncMock()
    runner.get_running_scripts = AsyncMock(return_value=[
        {
            "script_name": "source-linkedin",
            "execution_id": "exec-001",
            "status": "running",
            "progress": {"done": 50, "total": 100},
            "task_id": "task-uuid-2",
        },
    ])
    return runner


@pytest.fixture
def mock_config():
    """Mock ConfigStore."""
    config = MagicMock()
    config.agents = [
        {"name": "analyst", "is_active": True},
        {"name": "python-developer", "is_active": True},
    ]
    return config


@pytest.fixture
def reporter(
    mock_redis, mock_supervisor, mock_dispatcher,
    mock_sessions, mock_script_runner, mock_config,
):
    """Create a HealthReporter with all mocked dependencies (Phase 2 mode)."""
    return HealthReporter(
        redis=mock_redis,
        office_id="test-office",
        supervisor=mock_supervisor,
        dispatcher=mock_dispatcher,
        session_manager=mock_sessions,
        script_runner=mock_script_runner,
        config_store=mock_config,
    )


class TestSendReport:
    """Tests for send_report()."""

    @pytest.mark.asyncio
    async def test_writes_to_redis_key(self, reporter, mock_redis):
        """Health report is written to the correct Redis key.

        ``send_report`` writes two keys (health + presence hash); we
        only assert on the health key here and check for it among the
        call list instead of asserting ``called_once``.
        """
        await reporter.send_report()

        health_calls = [
            c for c in mock_redis.set.call_args_list
            if c.args and c.args[0] == "office:test-office:health"
        ]
        assert len(health_calls) == 1, (
            f"Expected exactly one set() to the health key, "
            f"got {len(health_calls)}"
        )
        assert health_calls[0].kwargs["ex"] == HEALTH_KEY_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_report_is_valid_json(self, reporter, mock_redis):
        """The written value is valid JSON."""
        await reporter.send_report()

        health_call = next(
            c for c in mock_redis.set.call_args_list
            if c.args and c.args[0] == "office:test-office:health"
        )
        written_json = health_call.args[1]
        report = json.loads(written_json)
        assert report["type"] == "health_report"
        assert report["office_id"] == "test-office"

    @pytest.mark.asyncio
    async def test_ttl_is_120_seconds(self, reporter, mock_redis):
        """Redis key TTL is set to 120 seconds."""
        await reporter.send_report()

        call_kwargs = mock_redis.set.call_args[1]
        assert call_kwargs["ex"] == 120


class TestBuildReport:
    """Tests for _build_report() content."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pending_field", ["execution_cleanup_failed", "execution_finalization_pending"])
    async def test_pending_execution_is_not_reported_as_productive_work(
        self, reporter, mock_supervisor, pending_field,
    ):
        state = {
            "status": "working",
            "current_task": "task-awaiting-safe-release",
            pending_field: True,
        }
        mock_supervisor.get_all_statuses.return_value = {"engineer": state}

        report = await reporter._build_report()

        assert report["agent_statuses"]["engineer"] == {
            "status": "error",
            "current_task": "task-awaiting-safe-release",
            pending_field: True,
        }
        assert state["status"] == "working"

        state[pending_field] = False
        state["status"] = "idle"
        state["current_task"] = None
        recovered = await reporter._build_report()
        assert recovered["agent_statuses"]["engineer"] == {
            "status": "idle", "current_task": None,
        }

    @pytest.mark.asyncio
    async def test_normal_cleanup_does_not_flash_an_execution_error(
        self, reporter, mock_supervisor,
    ):
        mock_supervisor.get_all_statuses.return_value = {
            "engineer": {
                "status": "working",
                "current_task": "finishing-task",
                "execution_cleanup_pending": True,
                "execution_cleanup_failed": False,
                "execution_finalization_pending": False,
            },
        }

        report = await reporter._build_report()

        assert report["agent_statuses"]["engineer"] == {
            "status": "working",
            "current_task": "finishing-task",
            "execution_cleanup_pending": True,
        }

    @pytest.mark.asyncio
    async def test_includes_agent_statuses(self, reporter):
        """Report includes agent statuses from supervisor."""
        report = await reporter._build_report()

        assert "analyst" in report["agent_statuses"]
        assert report["agent_statuses"]["analyst"]["status"] == "idle"
        assert "python-developer" in report["agent_statuses"]
        assert report["agent_statuses"]["python-developer"]["status"] == "working"
        assert report["agent_statuses"]["python-developer"]["current_task"] == "task-uuid-1"

    @pytest.mark.asyncio
    async def test_omits_synthetic_consult_task_ids(self, reporter, mock_supervisor):
        """A consult session's synthetic id never rides the wire.

        The consult spawns (``consult_planner`` /
        ``consult_flow_architect`` / ``consult_data_curator`` in
        ``src/handlers.py``) stamp the supervisor's ``current_task``
        with ``planner-<hex12>`` / ``flow-consult-<hex12>`` ids that
        have NO backend ``tasks`` row — a report carrying one invites
        clients into a board deep-link that can only 422. The report
        must drop the id (``current_task: None``) while keeping the
        agent honestly ``working``; a REAL (non-synthetic) id passes
        through untouched.
        """
        mock_supervisor.get_all_statuses.return_value = {
            "planner": {
                "status": "working",
                "pid": 111,
                "current_task": "planner-ab12cd34ef56",
                "uptime": 5.0,
            },
            "flow-architect": {
                "status": "working",
                "pid": 222,
                "current_task": "flow-consult-0123456789ab",
                "uptime": 5.0,
            },
            "builder": {
                "status": "working",
                "pid": 333,
                "current_task": "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9",
                "uptime": 5.0,
            },
        }

        report = await reporter._build_report()

        planner = report["agent_statuses"]["planner"]
        assert planner["current_task"] is None
        assert planner["status"] == "working"
        architect = report["agent_statuses"]["flow-architect"]
        assert architect["current_task"] is None
        assert architect["status"] == "working"
        # The real board task id is untouched.
        builder = report["agent_statuses"]["builder"]
        assert builder["current_task"] == "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"

    @pytest.mark.asyncio
    async def test_includes_queue_size(self, reporter):
        """Report includes queue size from dispatcher."""
        report = await reporter._build_report()
        assert report["queue_size"] == 3

    @pytest.mark.asyncio
    async def test_includes_running_scripts(self, reporter):
        """Report includes running scripts from script runner."""
        report = await reporter._build_report()
        assert len(report["running_scripts"]) == 1
        assert report["running_scripts"][0]["script_name"] == "source-linkedin"

    @pytest.mark.asyncio
    async def test_includes_active_sessions(self, reporter):
        """Report includes Manager sessions."""
        report = await reporter._build_report()
        assert "general_chat" in report["active_sessions"]

    @pytest.mark.asyncio
    async def test_includes_sdk_version(self, reporter):
        """Report includes SDK version."""
        report = await reporter._build_report()
        assert "sdk_version" in report

    @pytest.mark.asyncio
    async def test_includes_container_uptime(self, reporter):
        """Report includes process uptime."""
        report = await reporter._build_report()
        assert "container_uptime" in report
        assert isinstance(report["container_uptime"], float)

    @pytest.mark.asyncio
    async def test_reports_no_api_key_state(self, reporter):
        """Subscription-only: the report carries no API-key flag."""
        report = await reporter._build_report()
        assert "api_key_valid" not in report

    @pytest.mark.asyncio
    async def test_includes_daemon_version(self, reporter):
        """Report carries the cbcl daemon's own installed version
        (importlib metadata of cubicle-communicator), distinct from
        sdk_version (host-side claude-agent-sdk)."""
        from src.health import reporter as reporter_mod

        report = await reporter._build_report()
        assert report["daemon_version"] == reporter_mod._DAEMON_VERSION
        assert isinstance(report["daemon_version"], str)
        assert report["daemon_version"]  # never empty

    @pytest.mark.asyncio
    async def test_includes_capability_flags(self, reporter):
        """The report carries the daemon capability flags the backend
        gates features on: ``flow_studio`` (FS-P2.T10 — flow runs),
        ``instructions_v2`` (instruction-surfaces D6 — sources survey,
        workstream improve, the changes report), and ``memory_v1``
        (office-memory v1 — recall/remember catalogs, fenced memory
        indexes, the learnings import). Dropping one silently disables
        (or hides) its feature family for every office this daemon
        serves."""
        from src.health.reporter import DAEMON_CAPABILITIES

        report = await reporter._build_report()
        assert report["capabilities"] == list(DAEMON_CAPABILITIES)
        assert "flow_studio" in report["capabilities"]
        assert "instructions_v2" in report["capabilities"]
        assert "memory_v1" in report["capabilities"]


def _park_capacity_wait(
    state,
    task_id: str,
    state_value: str = "waiting",
    *,
    waiting_since: float | None = 0.0,
    updated_at: float = 0.0,
) -> None:
    """Write one capacity-wait ledger row the way an accepted handoff does.

    ``waiting_since=None`` stands for a row a pre-U14 daemon wrote."""
    with state._connection() as connection:
        connection.execute(
            "INSERT INTO capacity_waits (office_id,task_id,wait_id,operation_id,cycle,"
            "generation,epoch,phase,agent_name,assigned_agent,attempt_id,"
            "accepted_attempt_id,pending_resume_attempt_id,state,resume_context,"
            "next_check_at,updated_at,waiting_since) VALUES (?,?,?,?,1,1,0,'execute',"
            "'analyst','analyst','attempt','attempt',NULL,?,'{}',0,?,?)",
            (
                state.office_id,
                task_id,
                f"wait-{task_id}",
                f"op-{task_id}",
                state_value,
                updated_at,
                waiting_since,
            ),
        )


class TestCapacityWaitReport:
    """C3a-G3: tasks parked on an accepted capacity wait ride the heartbeat,
    so the backend sweeper does not call a healthy wait a stuck task."""

    @pytest.fixture(autouse=True)
    def _sync_counts(self, mock_supervisor, mock_script_runner):
        # The runtime snapshot reads these synchronously.
        mock_supervisor.active_execution_count = MagicMock(return_value=0)
        mock_script_runner.active_execution_count = MagicMock(return_value=0)

    @pytest.mark.asyncio
    async def test_lists_active_capacity_waits_from_the_ledger(
        self, reporter, tmp_path,
    ):
        from src.runtime_state import RuntimeState

        state = RuntimeState(tmp_path / "runtime.sqlite", "test-office")
        _park_capacity_wait(state, "task-b", "waiting")
        _park_capacity_wait(state, "task-a", "resuming")
        _park_capacity_wait(state, "task-done", "resumed")
        _park_capacity_wait(state, "task-gone", "retired")
        reporter._runtime_state = state

        report = await reporter._build_report()

        assert report["active_capacity_wait_task_ids"] == ["task-a", "task-b"]

    @pytest.mark.asyncio
    async def test_capacity_wait_list_is_bounded(self, reporter, tmp_path):
        from src.health.reporter import CAPACITY_WAIT_REPORT_LIMIT
        from src.runtime_state import RuntimeState

        state = RuntimeState(tmp_path / "runtime.sqlite", "test-office")
        for index in range(CAPACITY_WAIT_REPORT_LIMIT + 20):
            _park_capacity_wait(state, f"task-{index:04d}")
        reporter._runtime_state = state

        report = await reporter._build_report()

        assert len(report["active_capacity_wait_task_ids"]) == CAPACITY_WAIT_REPORT_LIMIT

    @pytest.mark.asyncio
    async def test_empty_without_a_ledger_or_when_the_read_fails(self, reporter):
        report = await reporter._build_report()
        assert report["active_capacity_wait_task_ids"] == []

        broken = MagicMock()
        broken.active_capacity_waits.side_effect = RuntimeError("database is locked")
        broken.maintenance_status.return_value = {"state": "open"}
        broken.quota_status.return_value = {}
        reporter._runtime_state = broken

        report = await reporter._build_report()

        assert report["active_capacity_wait_task_ids"] == []
        assert report["type"] == "health_report"


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat()


class TestWaitEvidenceReport:
    """U14 / C3a-G3: each parked wait reports when it began, so the backend
    can suppress a healthy wait's alarms for a bounded time and alarm on one
    that never ends. Contract: ``[{task_id, waiting_since}]`` with ISO-8601
    UTC times, alongside the id list older backends read."""

    @pytest.fixture(autouse=True)
    def _sync_counts(self, mock_supervisor, mock_script_runner):
        mock_supervisor.active_execution_count = MagicMock(return_value=0)
        mock_script_runner.active_execution_count = MagicMock(return_value=0)

    @pytest.mark.asyncio
    async def test_capacity_waits_carry_when_each_wait_began(
        self, reporter, tmp_path,
    ):
        from src.runtime_state import RuntimeState

        state = RuntimeState(tmp_path / "runtime.sqlite", "test-office")
        _park_capacity_wait(
            state, "task-b", "waiting", waiting_since=1_000.0, updated_at=5_000.0,
        )
        _park_capacity_wait(
            state, "task-a", "resuming", waiting_since=2_000.0, updated_at=6_000.0,
        )
        _park_capacity_wait(state, "task-done", "resumed", waiting_since=3_000.0)
        reporter._runtime_state = state

        report = await reporter._build_report()

        # The start of the wait, not its last resume/claim transition.
        assert report["capacity_waits"] == [
            {"task_id": "task-a", "waiting_since": _iso(2_000.0)},
            {"task_id": "task-b", "waiting_since": _iso(1_000.0)},
        ]
        assert report["active_capacity_wait_task_ids"] == ["task-a", "task-b"]

    @pytest.mark.asyncio
    async def test_a_wait_without_a_start_reports_its_last_transition(
        self, reporter,
    ):
        ledger = MagicMock()
        ledger.active_capacity_waits.return_value = [
            {"task_id": "task-old", "waiting_since": None, "updated_at": 4_000.0},
            {"task_id": "", "waiting_since": 1.0},
            {"task_id": "task-bad", "waiting_since": "yesterday"},
        ]
        ledger.parked_script_handoffs.return_value = []
        ledger.maintenance_status.return_value = {"state": "open"}
        ledger.quota_status.return_value = {}
        reporter._runtime_state = ledger

        report = await reporter._build_report()

        # A made-up start would let a stuck wait hide forever; a row with no
        # usable time is left out rather than guessed.
        assert report["capacity_waits"] == [
            {"task_id": "task-old", "waiting_since": _iso(4_000.0)},
        ]

    @pytest.mark.asyncio
    async def test_capacity_wait_evidence_is_bounded(self, reporter, tmp_path):
        from src.health.reporter import CAPACITY_WAIT_REPORT_LIMIT
        from src.runtime_state import RuntimeState

        state = RuntimeState(tmp_path / "runtime.sqlite", "test-office")
        for index in range(CAPACITY_WAIT_REPORT_LIMIT + 20):
            _park_capacity_wait(state, f"task-{index:04d}")
        reporter._runtime_state = state

        report = await reporter._build_report()

        assert len(report["capacity_waits"]) == CAPACITY_WAIT_REPORT_LIMIT

    @pytest.mark.asyncio
    async def test_script_handoff_waits_list_parked_tasks_of_the_current_cycle(
        self, reporter, tmp_path,
    ):
        from src.runtime_state import RuntimeState

        state = RuntimeState(tmp_path / "runtime.sqlite", "test-office")
        # The script still runs.
        state.observe_cycle("task-running", 1)
        state.note_script("task-running", "exec-1", "running")
        state.park_script_handoff("task-running")
        # The script finished; the phase waits for its (busy) agent.
        state.observe_cycle("task-finished", 1)
        state.note_script("task-finished", "exec-2", "completed")
        state.park_script_handoff("task-finished")
        # Already resumed: no longer waiting.
        state.observe_cycle("task-resumed", 1)
        state.park_script_handoff("task-resumed")
        state.resume_script_handoff("task-resumed")
        # Parked in an earlier execution cycle.
        state.observe_cycle("task-old-cycle", 1)
        state.park_script_handoff("task-old-cycle")
        state.observe_cycle("task-old-cycle", 2)
        with state._connection() as connection:
            for task_id, parked_at in (
                ("task-running", 10_000.0),
                ("task-finished", 20_000.0),
            ):
                connection.execute(
                    "UPDATE task_script_wait_times SET parked_at=? WHERE task_id=?",
                    (parked_at, task_id),
                )
        reporter._runtime_state = state

        report = await reporter._build_report()

        assert report["script_handoff_waits"] == [
            {"task_id": "task-finished", "waiting_since": _iso(20_000.0)},
            {"task_id": "task-running", "waiting_since": _iso(10_000.0)},
        ]

    @pytest.mark.asyncio
    async def test_wait_evidence_is_empty_without_a_ledger_or_when_reads_fail(
        self, reporter,
    ):
        report = await reporter._build_report()
        assert report["capacity_waits"] == []
        assert report["script_handoff_waits"] == []

        broken = MagicMock()
        broken.active_capacity_waits.side_effect = RuntimeError("database is locked")
        broken.parked_script_handoffs.side_effect = RuntimeError("database is locked")
        broken.maintenance_status.return_value = {"state": "open"}
        broken.quota_status.return_value = {}
        reporter._runtime_state = broken

        report = await reporter._build_report()

        assert report["capacity_waits"] == []
        assert report["script_handoff_waits"] == []
        assert report["type"] == "health_report"

    @pytest.mark.asyncio
    async def test_one_failed_read_does_not_hide_the_other_list(self, reporter):
        ledger = MagicMock()
        ledger.active_capacity_waits.side_effect = RuntimeError("database is locked")
        ledger.parked_script_handoffs.return_value = [
            {"task_id": "task-parked", "parked_at": 7_000.0},
        ]
        ledger.maintenance_status.return_value = {"state": "open"}
        ledger.quota_status.return_value = {}
        reporter._runtime_state = ledger

        report = await reporter._build_report()

        assert report["capacity_waits"] == []
        assert report["script_handoff_waits"] == [
            {"task_id": "task-parked", "waiting_since": _iso(7_000.0)},
        ]


class TestFallbackBehavior:
    """Tests for fallback when supervisor is not available."""

    @pytest.mark.asyncio
    async def test_uses_config_agents_when_no_supervisor(
        self, mock_redis, mock_config,
    ):
        """Falls back to config agent list when supervisor is None."""
        reporter = HealthReporter(
            redis=mock_redis,
            office_id="test-office",
            supervisor=None,
            config_store=mock_config,
        )
        report = await reporter._build_report()

        assert "analyst" in report["agent_statuses"]
        assert report["agent_statuses"]["analyst"]["status"] == "idle"

    @pytest.mark.asyncio
    async def test_redis_error_does_not_crash(self, reporter, mock_redis):
        """Redis write failure is logged but does not raise."""
        mock_redis.set = AsyncMock(side_effect=RuntimeError("Redis down"))

        # Should not raise
        await reporter.send_report()


class TestWebSocketFallback:
    """Tests for WS-transport publish behaviour.

    The ``ws_client`` parameter was renamed to ``transport`` when the
    reporter was wired to the new WsTransport abstraction. The transport
    exposes ``publish_event`` (not ``send``), so these tests assert on
    that. Redis failures no longer "fall back" to WS — both paths run
    independently; the WS publish runs on every report as a secondary
    channel.
    """

    @pytest.mark.asyncio
    async def test_transport_publish_when_no_redis(self, mock_config):
        """When redis is None, the transport still publishes the report."""
        transport = AsyncMock()
        reporter = HealthReporter(
            transport=transport,
            office_id="test-office",
            config_store=mock_config,
        )
        await reporter.send_report()

        transport.publish_event.assert_awaited_once()
        sent = transport.publish_event.call_args[0][0]
        assert sent["type"] == "health_report"
        assert sent["office_id"] == "test-office"

    @pytest.mark.asyncio
    async def test_transport_publish_on_redis_error(
        self, mock_redis, mock_config,
    ):
        """Redis write failure is logged but transport still publishes."""
        mock_redis.set = AsyncMock(side_effect=RuntimeError("Redis down"))
        transport = AsyncMock()
        reporter = HealthReporter(
            redis=mock_redis,
            transport=transport,
            office_id="test-office",
            config_store=mock_config,
        )
        await reporter.send_report()

        # Redis attempted but failed — transport still publishes.
        assert mock_redis.set.call_count >= 1
        transport.publish_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_agents_idle_when_no_supervisor(self, mock_config):
        """Without supervisor, all agents report as idle."""
        transport = AsyncMock()

        reporter = HealthReporter(
            transport=transport,
            office_id="test-office",
            config_store=mock_config,
        )
        report = await reporter._build_report()

        # All agents from config should be idle (no supervisor to track state)
        for agent_name, status in report["agent_statuses"].items():
            assert status["status"] == "idle"


class TestLimitsReconcilerTick:
    """The report loop doubles as the periodic recheck for a deferred
    container-limits recreate (``recheck_pending`` piggybacks on the
    health tick)."""

    @pytest.mark.asyncio
    async def test_loop_calls_recheck_pending(self, mock_config):
        reconciler = MagicMock()
        reconciler.recheck_pending = AsyncMock(return_value="in_sync")
        reporter = HealthReporter(
            transport=AsyncMock(),
            office_id="test-office",
            config_store=mock_config,
            interval=0.01,
            limits_reconciler=reconciler,
        )
        reporter.start()
        await asyncio.sleep(0.06)
        reporter.stop()
        assert reconciler.recheck_pending.await_count >= 1

    @pytest.mark.asyncio
    async def test_recheck_error_does_not_kill_loop(self, mock_config):
        reconciler = MagicMock()
        reconciler.recheck_pending = AsyncMock(
            side_effect=RuntimeError("docker down")
        )
        transport = AsyncMock()
        reporter = HealthReporter(
            transport=transport,
            office_id="test-office",
            config_store=mock_config,
            interval=0.01,
            limits_reconciler=reconciler,
        )
        reporter.start()
        await asyncio.sleep(0.08)
        reporter.stop()
        # The recheck raised on every tick, yet reports kept flowing.
        assert reconciler.recheck_pending.await_count >= 2
        assert transport.publish_event.await_count >= 2

    @pytest.mark.asyncio
    async def test_no_reconciler_is_fine(self, mock_config):
        reporter = HealthReporter(
            transport=AsyncMock(),
            office_id="test-office",
            config_store=mock_config,
            interval=0.01,
        )
        reporter.start()
        await asyncio.sleep(0.03)
        reporter.stop()  # no crash — reconciler is optional


class TestLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self, reporter):
        """start() creates a background asyncio task."""
        reporter.start()
        assert reporter._task is not None
        reporter.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, reporter):
        """stop() cancels the background task."""
        reporter.start()
        task = reporter._task
        reporter.stop()
        assert reporter._task is None
        # Wait for cancellation to propagate
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()
