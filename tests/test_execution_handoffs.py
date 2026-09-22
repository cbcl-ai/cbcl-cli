"""Human and managed-script handoffs must not masquerade as completed work."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src import _setup_cli
from src.execution_completion import completion_disposition
from src.flow_blocks import FlowBlockExecutor
from src.runtime_state import AdmissionPaused, RuntimeState, register_generation_runtime


@pytest.fixture
def runtime(tmp_path):
    return RuntimeState(tmp_path / "runtime.sqlite3", "office")


def test_human_request_takes_precedence_over_executor_review_frame():
    assert completion_disposition(
        {"status": "blocked", "human_action_request_id": "request"},
        {"status": "review"}, active_scripts=False,
    ) == "human_handoff"


def test_active_script_handoff_is_not_review():
    assert completion_disposition(
        {"status": "in_progress"}, {"status": "review"}, active_scripts=True,
    ) == "script_handoff"


def test_fast_script_still_requires_fresh_verification_attempt(runtime):
    runtime.observe_cycle("task", 1)
    runtime.note_script("task", "execution", "completed")
    runtime.note_script_owner("execution", "launch-attempt")
    assert runtime.execution_started_script("task", "launch-attempt")
    assert not runtime.execution_started_script("task", "verification-attempt")
    assert completion_disposition(
        {"status": "in_progress"}, {"status": "review"}, active_scripts=False,
        started_script=runtime.execution_started_script("task", "launch-attempt"),
    ) == "script_handoff"


def test_old_generation_completion_cannot_mutate_successor():
    assert completion_disposition(
        {"status": "in_progress", "execution_cycle": 2, "execution_generation": 4},
        {"status": "review", "_caller": {"execution_cycle": 2, "execution_generation": 3}},
        active_scripts=False,
    ) == "superseded"


def test_attested_review_script_parks_without_missing_verdict_recovery():
    task = {"status": "review", "execution_cycle": 1, "execution_generation": 4}
    event = {"is_review_completion": True,
             "_caller": {"execution_cycle": 1, "execution_generation": 4}}
    assert completion_disposition(task, event, active_scripts=True, started_script=True) == "script_handoff"
    # A different attempt's live script never excuses an empty review verdict.
    assert completion_disposition(task, event, active_scripts=True, started_script=False) == "normal"
    assert completion_disposition(task, {**event, "_caller": {"execution_cycle": 1, "execution_generation": 3}},
                                  active_scripts=True, started_script=True) == "superseded"


def test_real_human_hold_precedes_attested_script_handoff():
    assert completion_disposition(
        {"status": "blocked", "human_action_request_id": "real-request"},
        {"status": "blocked"}, active_scripts=True, started_script=True,
    ) == "human_handoff"


def test_script_handoff_survives_restart_and_resumes_only_after_terminal(runtime):
    runtime.observe_cycle("task", 1)
    runtime.note_script("task", "execution", "running")
    runtime.park_script_handoff("task")
    reopened = RuntimeState(runtime.database_path, "office")
    assert reopened.script_wait("task")["state"] == "waiting"
    reopened.note_script("task", "execution", "completed", cycle=1)
    assert reopened.script_wait("task")["state"] == "resumable"
    reopened.resume_script_handoff("task")
    assert reopened.script_wait("task") is None
    assert reopened.script_handoffs("task")[0]["state"] == "completed"


def test_old_script_completion_does_not_change_new_cycle(runtime):
    runtime.observe_cycle("task", 1)
    runtime.note_script("task", "old", "running")
    runtime.park_script_handoff("task")
    runtime.observe_cycle("task", 2)
    runtime.note_script("task", "old", "completed", cycle=1)
    assert runtime.script_wait("task") is None
    assert runtime.script_handoffs("task") == []


async def test_paused_flow_reports_retryable_without_execution(runtime, tmp_path):
    router = AsyncMock()
    executor = FlowBlockExecutor(router=router, office_id="office", workspace_path=str(tmp_path), runtime_state=runtime)
    executor._execute_ai = AsyncMock()
    runtime.set_maintenance(True)
    await executor.handle_flow_block_execute({"run_id": "run", "block_id": "block", "kind": "ai", "payload": {}})
    await executor.drain()
    executor._execute_ai.assert_not_awaited()
    event = router.publish_event.await_args.args[0]
    assert event["error_code"] == "maintenance_paused"
    assert event["retryable"] is True


async def test_flow_same_activation_resumes_after_pause_and_executes_once(runtime, tmp_path):
    router = AsyncMock()
    executor = FlowBlockExecutor(router=router, office_id="office", workspace_path=str(tmp_path), runtime_state=runtime)
    executor._execute_ai = AsyncMock(return_value={"ok": True, "output": "result"})
    message = {"run_id": "run", "block_id": "block", "activation_id": "activation", "kind": "ai", "payload": {}}
    runtime.set_maintenance(True)
    await executor.handle_flow_block_execute(message)
    await executor.drain()
    assert not executor._results
    runtime.set_maintenance(False)
    await executor.handle_flow_block_execute(message)
    await executor.drain()
    await executor.handle_flow_block_execute(message)
    await executor.drain()
    executor._execute_ai.assert_awaited_once()
    assert router.publish_event.await_args.args[0]["ok"] is True


async def test_paused_generation_never_launches(runtime, monkeypatch):
    register_generation_runtime("paused-generation-container", runtime)
    runtime.set_maintenance(True)
    generation = AsyncMock()
    monkeypatch.setattr(_setup_cli, "_run_claude_cli_admitted", generation)
    with pytest.raises(AdmissionPaused):
        await _setup_cli._run_claude_cli("paused-generation-container", "system", "prompt")
    generation.assert_not_awaited()


async def test_cancelled_generation_caller_retains_admission_until_bounded_work_finishes(runtime, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def generation(**kwargs):
        started.set()
        await release.wait()
        return "done"

    register_generation_runtime("running-generation-container", runtime)
    monkeypatch.setattr(_setup_cli, "_run_claude_cli_admitted", generation)
    caller = asyncio.create_task(_setup_cli._run_claude_cli("running-generation-container", "system", "prompt"))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    runtime.set_maintenance(True)
    runtime.snapshot(0, 0)
    assert runtime.maintenance_status()["state"] == "draining"
    release.set()
    await asyncio.gather(*list(_setup_cli._admitted_generation_tasks))
    runtime.snapshot(0, 0)
    assert runtime.maintenance_status()["state"] == "drained"


async def test_quota_paused_flow_waits_without_invoking_ai_and_can_retry(runtime, tmp_path):
    router = AsyncMock()
    executor = FlowBlockExecutor(router=router, office_id="office", workspace_path=str(tmp_path), runtime_state=runtime)
    executor._execute_ai = AsyncMock(return_value={"ok": True, "output": {"result": "ready"}})
    runtime.pause_for_quota("Claude usage limit reached", "opus")
    message = {"run_id": "run", "block_id": "block", "activation_id": "activation", "kind": "ai", "payload": {}}
    await executor.handle_flow_block_execute(message)
    await executor.drain()
    executor._execute_ai.assert_not_awaited()
    assert router.publish_event.await_args.args[0]["error_code"] == "quota_paused"
    runtime.update_quota(runtime.quota_status()["revision"], {"state": "running"})
    await executor.handle_flow_block_execute(message)
    await executor.drain()
    executor._execute_ai.assert_awaited_once()
    assert router.publish_event.await_args.args[0]["ok"] is True
