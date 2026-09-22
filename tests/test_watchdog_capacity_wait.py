"""Capacity waits keep queue recovery without spending the crash budget."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.watchdog import TaskWatchdog
from tests.test_watchdog import _make_config, _make_dispatcher, _make_manager, _make_supervisor, _make_ws


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["waiting", "resuming"])
@pytest.mark.parametrize("previous_crashes", [0, 3])
async def test_attested_capacity_wait_requeues_without_failure_or_escalation(state, previous_crashes):
    dispatcher = _make_dispatcher()
    task = {"id": "t1", "status": "in_progress", "assigned_agent": "analyst",
            "execution_cycle": 2, "execution_generation": 5, "active_execution_attempt_id": "attempt"}
    runtime = SimpleNamespace(
        quota_status=lambda: {"state": "running"}, admission_open=lambda: True,
        has_pending_completion=lambda _task: False, script_wait=lambda _task: None,
        capacity_wait_for_task=Mock(return_value={"state": state}), record_failure=Mock(),
    )
    watchdog = TaskWatchdog(ws=_make_ws(), executor=None, manager=_make_manager(),
        config_store=_make_config(), task_queue=None, office_id="off1",
        supervisor=_make_supervisor(), dispatcher=dispatcher, runtime_state=runtime)
    watchdog._task_crash_count["t1"] = previous_crashes
    watchdog._move_task = Mock(side_effect=AssertionError("Capacity wait must not escalate"))
    await watchdog._handle_in_progress(task)
    runtime.capacity_wait_for_task.assert_called_once_with(task)
    dispatcher.add_task.assert_awaited_once_with({**task, "task_id": "t1"})
    runtime.record_failure.assert_not_called()
    assert watchdog._task_crash_count["t1"] == previous_crashes
