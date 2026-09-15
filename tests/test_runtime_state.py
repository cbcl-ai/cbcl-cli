"""Persistent admission pauses and recovery budgets remain conservative."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.health.reporter import HealthReporter
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.orchestrator.task_dispatcher import TaskDispatcher
from src.runtime_state import AdmissionPaused, RuntimeState
from src.scripts.script_runner import ScriptRunner
from src.watchdog import TaskWatchdog


@pytest.fixture
def runtime(tmp_path):
    return RuntimeState(tmp_path / "runtime" / "control.sqlite3", "office")


def test_pause_survives_reopening_and_is_office_scoped(runtime):
    runtime.set_maintenance(True)
    reopened = RuntimeState(runtime.database_path, "office")
    other = RuntimeState(runtime.database_path, "other")
    assert not reopened.admission_open()
    assert other.admission_open()
    with pytest.raises(AdmissionPaused):
        reopened.reserve("worker")


def test_global_pause_cannot_be_overridden_by_office_resume(runtime):
    RuntimeState(runtime.database_path, "*").set_maintenance(True)
    runtime.set_maintenance(False)
    assert not runtime.admission_open()


def test_drain_requires_fresh_ack_and_no_inflight_admission(runtime):
    runtime.snapshot(0, 0)
    reservation = runtime.reserve("worker")
    runtime.set_maintenance(True)
    assert runtime.maintenance_status()["state"] == "unknown"
    runtime.snapshot(0, 0)
    assert runtime.maintenance_status()["state"] == "draining"
    assert runtime.owns_reservation(reservation)
    runtime.release(reservation)
    assert runtime.maintenance_status()["state"] == "unknown"
    runtime.snapshot(0, 0)
    assert runtime.maintenance_status()["state"] == "drained"


def test_active_work_or_uncertain_telemetry_never_reports_drained(runtime):
    runtime.set_maintenance(True)
    runtime.snapshot(1, 0)
    assert runtime.maintenance_status()["state"] == "draining"
    runtime.snapshot(0, 1)
    assert runtime.maintenance_status()["state"] == "draining"
    assert runtime.maintenance_status(max_age=-1)["state"] == "unknown"


def test_previous_daemon_reservation_is_explicit_reconciliation_not_forever_draining(runtime):
    runtime.reserve("generation", "task")
    runtime.set_maintenance(True)
    restarted = RuntimeState(runtime.database_path, "office")
    restarted.snapshot(0, 0)
    status = restarted.maintenance_status()
    assert status["state"] == "reconciliation_required"
    assert status["retained_admissions"][0]["task_id"] == "task"
    assert status["retained_admissions"][0]["kind"] == "generation"
    assert status["pending_admissions"] == 1
    assert status["reconciliation_message"]


def test_nested_admitted_work_can_finish_after_pause(runtime):
    with runtime.admission("flow"):
        runtime.set_maintenance(True)
        with runtime.admission("generation"):
            assert runtime.maintenance_status()["pending_admissions"] == 2
    assert runtime.maintenance_status()["pending_admissions"] == 0
    with pytest.raises(AdmissionPaused):
        with runtime.admission("generation"):
            pytest.fail("Unadmitted execution ran during maintenance")


def test_failure_dedup_and_budget_survive_restart(runtime):
    runtime.observe_cycle("task", 2)
    assert runtime.record_failure("task", "attempt", 2) == 1
    reopened = RuntimeState(runtime.database_path, "office")
    assert reopened.record_failure("task", "attempt", 2) == 1
    assert reopened.record_failure("task", "second", 2) == 2
    assert RuntimeState(runtime.database_path, "other").failure_count("task") == 0


def test_only_newer_authoritative_cycle_resets_budget(runtime):
    runtime.observe_cycle("task", 2)
    runtime.record_failure("task", "attempt", 2)
    for stale_cycle in (None, 0, 1, 2, "3", True):
        runtime.observe_cycle("task", stale_cycle)
        assert runtime.failure_count("task") == 1
    runtime.observe_cycle("task", 3)
    assert runtime.failure_count("task") == 0
    assert runtime.record_failure("task", "late-old-attempt", 2) == 0
    assert runtime.record_failure("task", "new-attempt", 3) == 1


def test_persisted_cap_applies_before_watchdog_first_board_poll(runtime):
    runtime.observe_cycle("task", 1)
    for attempt in range(3):
        runtime.record_failure("task", str(attempt), 1)
    watchdog = TaskWatchdog(None, None, MagicMock(), MagicMock(), None, "office", runtime_state=runtime)
    assert watchdog.respawn_capped("task")


def test_unpersisted_failure_caps_admission_until_retried(runtime, monkeypatch):
    watchdog = TaskWatchdog(None, None, MagicMock(), MagicMock(), None, "office", runtime_state=runtime)
    monkeypatch.setattr(runtime, "record_failure", MagicMock(side_effect=OSError("unavailable")))
    watchdog.record_process_failure("task", "attempt", 0)
    assert watchdog.respawn_capped("task")
    assert watchdog._durability_pending == {("task", "attempt"): 0}


async def test_paused_supervisor_does_not_spawn_or_stop_existing_worker(runtime, monkeypatch):
    supervisor = AgentSupervisor(".", "office")
    supervisor.set_runtime_state(runtime)
    spawn = AsyncMock()
    monkeypatch.setattr(supervisor, "_spawn_worker", spawn)
    runtime.set_maintenance(True)
    assert not await supervisor.spawn_worker("engineer", {}, {"task_id": "task"})
    spawn.assert_not_awaited()


async def test_previously_reserved_dispatch_can_finish_admission_after_pause(runtime, monkeypatch):
    supervisor = AgentSupervisor(".", "office")
    supervisor.set_runtime_state(runtime)
    spawn = AsyncMock(return_value=True)
    monkeypatch.setattr(supervisor, "_spawn_worker", spawn)
    reservation = runtime.reserve("worker", "task")
    runtime.set_maintenance(True)
    assert await supervisor.spawn_worker("engineer", {}, {"task_id": "task"}, admission_token=reservation)
    assert runtime.owns_reservation(reservation)
    runtime.release(reservation)


async def test_paused_dispatcher_does_not_pop_or_mutate_board(runtime, monkeypatch):
    dispatcher = TaskDispatcher(MagicMock(), "office", MagicMock(), MagicMock(), MagicMock())
    dispatcher.set_runtime_state(runtime)
    dispatch = AsyncMock()
    monkeypatch.setattr(dispatcher, "_dispatch_agent", dispatch)
    runtime.set_maintenance(True)
    assert not await dispatcher.dispatch_agent("engineer")
    dispatch.assert_not_awaited()


async def test_paused_script_runner_never_starts_script(runtime, tmp_path, monkeypatch):
    runner = ScriptRunner(str(tmp_path), MagicMock(), MagicMock())
    runner.set_runtime_state(runtime)
    launch = AsyncMock()
    monkeypatch.setattr(runner, "_execute_v2", launch)
    runtime.set_maintenance(True)
    with pytest.raises(AdmissionPaused):
        await runner.execute("safe-script", task_id="task")
    launch.assert_not_awaited()
    assert runner.active_execution_count() == 0


async def test_health_ack_ignores_idle_manager_but_counts_pending_cleanup(runtime):
    supervisor = AgentSupervisor(".", "office")
    supervisor._agents["manager"] = AgentProcess("manager", "manager", state=AgentState.READY)
    reporter = HealthReporter(supervisor=supervisor, runtime_state=runtime)
    runtime.set_maintenance(True)
    assert (await reporter._build_report())["maintenance"]["state"] == "drained"
    supervisor._agents["engineer"] = AgentProcess("engineer", "worker", cleanup_pending=True)
    assert (await reporter._build_report())["maintenance"]["state"] == "draining"
