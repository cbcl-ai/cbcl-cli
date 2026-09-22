"""Provider-neutral operations exercise real local and fake-external wrappers."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.operation_state import OperationConflict, operation_result, operation_spec
from src.orchestrator.agent_supervisor import AgentSupervisor
from src.runtime_state import RuntimeState
from src.scripts.script_execution import on_complete
from src.scripts.script_runner import ScriptRunner
from src.scripts.secrets_store import SecretsStore
from src.scripts.variable_manager import VariableManager


@pytest.fixture
def context(tmp_path, monkeypatch):
    state = RuntimeState(tmp_path / "private/runtime.sqlite", "office")
    state.observe_cycle("task", 1)
    supervisor = AgentSupervisor(str(tmp_path), "office")
    supervisor.set_runtime_state(state)
    supervisor.set_execution_policy({"enabled": True, "max_workers": 4, "max_workers_per_profile": 2})
    runner = ScriptRunner(str(tmp_path), SecretsStore(str(tmp_path)), VariableManager(str(tmp_path)))
    runner.set_runtime_state(state)
    runner.set_resource_supervisor(supervisor)
    runner._operation_host_capacity = None
    runner._assert_task_runnable = AsyncMock(return_value={"execution_resources": []})
    runner._router = SimpleNamespace(publish_event=AsyncMock())
    from src.scripts import script_runner
    monkeypatch.setattr(script_runner, "ensure_deps_installed", AsyncMock())
    caller = {"role": "worker", "task_id": "task", "execution_cycle": 1,
              "task_mode": "execute", "attempt_id": "attempt", "agent_name": "analyst"}
    return SimpleNamespace(state=state, runner=runner, caller=caller, workspace=tmp_path)


def script(ctx, name="local", body="print('done')\n", manifest=""):
    path = ctx.workspace / ".scripts" / name
    path.mkdir(parents=True)
    (path / "script.yaml").write_text("description: test operation\n" + manifest)
    (path / "main.py").write_text(body)
    return path


async def launch(ctx, name="local", key="export", **kwargs):
    return await ctx.runner.execute(name, task_id="task", execution_caller=ctx.caller,
                                    operation={"key": key, "input_fingerprint": "a" * 64}, **kwargs)


async def finish(ctx, execution_id):
    execution = ctx.runner._active[execution_id]
    code = await asyncio.wait_for(execution.process.wait(), 5)
    await on_complete(execution, code, ctx.runner._active, ctx.workspace, None,
                      active_by_task=ctx.runner._active_by_task)
    return ctx.state.list_operations("task")[0]


async def test_local_run_is_wired_deduplicated_and_uses_real_exit(context):
    script(context, body="import os\nfrom pathlib import Path\nPath(os.environ['CUBICLE_OUTPUT_DIR'],'launch-count').write_text('one')\nprint('done')\n")
    first = await launch(context)
    # Output authored by a running script must not be treated as mechanism
    # source. Normal production scripts write to their isolated output path.
    second = await launch(context)
    assert second == first
    result = await finish(context, first)
    assert result["state"] == "succeeded"
    assert result["exit_code"] == 0 and result["cleanup_confirmed"] is True
    assert result["artifact_refs"][0].endswith("/log.txt")
    assert (context.workspace / "outputs/operations" / result["operation_id"] / "launch-count").read_text() == "one"
    events = context.runner._router.publish_event.call_args_list
    assert any(call.args[0].get("event_type") == "execution_progress" for call in events)


async def test_conflicting_intent_and_changed_inputs_do_not_launch(context):
    script(context, body="import time\ntime.sleep(30)\n")
    execution = await launch(context)
    with pytest.raises(OperationConflict, match="resource"):
        await launch(context, key="second")
    with pytest.raises(OperationConflict, match="inputs changed"):
        await context.runner.execute("local", task_id="task", execution_caller=context.caller,
                                     operation={"key": "export", "input_fingerprint": "b" * 64})
    assert len(context.runner._active) == 1
    assert context.state.list_operations("task")[0]["state"] == "running"
    await context.runner.kill(execution)


async def test_independent_review_has_a_distinct_operation(context):
    script(context)
    first = await launch(context)
    await finish(context, first)
    context.caller = {**context.caller, "task_mode": "review", "attempt_id": "review-attempt"}
    second = await launch(context)
    await finish(context, second)
    assert first != second
    assert {row["phase"] for row in context.state.list_operations("task")} == {"execute", "review"}
    assert {row["stage"] for row in context.state.list_operations("task")} == {"execution", "verification"}


@pytest.mark.parametrize("stage", ["preparation", "execution", "verification"])
async def test_declared_timing_stage_preserves_executor_authority(context, stage):
    script(context)
    execution = await context.runner.execute("local", task_id="task", execution_caller=context.caller,
        operation={"key": "work", "input_fingerprint": "a" * 64, "stage": stage})
    result = await finish(context, execution)
    event = context.runner._router.publish_event.call_args_list[-1].args[0]["details"]["execution_event"]
    assert result["phase"] == "execute" and result["stage"] == stage
    assert event["origin_phase"] == "execute" and event["phase"] == stage


async def test_unknown_exit_never_uses_success_log(context):
    script(context, body="print('SUCCESS PASS done')\n")
    execution_id = await launch(context)
    execution = context.runner._active[execution_id]
    await execution.process.wait()
    await on_complete(execution, -1, context.runner._active, context.workspace, None, exit_unknown=True,
                      active_by_task=context.runner._active_by_task)
    result = context.state.list_operations("task")[0]
    assert result["state"] == "unknown" and result["exit_code"] is None
    assert result["cleanup_confirmed"] is True
    assert context.state.script_handoffs("task")[0]["state"] == "failed"


async def test_preparation_failure_has_an_observed_outcome_without_launch(context, monkeypatch):
    from src.scripts import script_runner

    script(context)
    monkeypatch.setattr(script_runner, "ensure_deps_installed", AsyncMock(side_effect=ValueError("invalid local configuration")))
    with pytest.raises(ValueError):
        await launch(context)
    result = context.state.list_operations("task")[0]
    assert result["state"] == "failed" and result["cleanup_confirmed"]
    assert not context.runner._active
    events = context.runner._router.publish_event.call_args_list
    assert events[-1].args[0]["details"]["execution_event"]["state"] == "failed"


async def test_fake_external_reconciliation_reuses_recorded_remote_job(context):
    path = script(context, name="external", manifest=(
        "operation_mode: external\noperation_reconcile_entry_point: reconcile.py\n"
    ), body=(
        "import os,json\nfrom pathlib import Path\n"
        "Path(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':"
        "{'service':'fake-export','run_id':'remote-123'},'state':'running'}))\n"
    ))
    (path / "reconcile.py").write_text(
        "import os,json\nfrom pathlib import Path\n"
        "context=json.loads(Path(os.environ['CUBICLE_OPERATION_CONTEXT']).read_text())\n"
        "assert context['action']=='reconcile'\nassert context['external_ref']['run_id']=='remote-123'\n"
        "Path(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':"
        "context['external_ref'],'state':'succeeded'}))\n"
    )
    first = await launch(context, name="external")
    result = await finish(context, first)
    assert result["state"] == "unknown"  # wrapper exit is not external success
    assert result["external_ref"]["run_id"] == "remote-123"
    # Restart the private ledger and bind a new attempt to the same cycle.
    context.runner.set_runtime_state(RuntimeState(context.state.database_path, "office"))
    context.state = context.runner._runtime_state
    caller = {**context.caller, "attempt_id": "resumed-attempt"}
    resumed = await context.runner.control_operation(result["operation_id"], "reconcile", caller)
    assert resumed["execution_id"] != first
    finished = await finish(context, resumed["execution_id"])
    assert finished["state"] == "succeeded" and finished["external_ref"] == result["external_ref"]


async def test_inspection_cannot_reopen_a_claimed_reconciliation_before_launch(context):
    path = script(context, name="external", manifest="operation_mode: external\noperation_reconcile_entry_point: reconcile.py\n",
                  body="import os,json\nfrom pathlib import Path\nPath(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':{'service':'fake','run_id':'one'},'state':'running'}))\n")
    (path / "reconcile.py").write_text("print('observed')\n")
    result = await finish(context, await launch(context, name="external"))
    claimed, resume = asyncio.Event(), asyncio.Event()
    checks = 0

    async def validate(*_):
        nonlocal checks
        checks += 1
        if checks == 2:
            claimed.set()
            await resume.wait()
        return {"execution_resources": []}

    context.runner._assert_task_runnable = validate
    observer = asyncio.create_task(context.runner.control_operation(result["operation_id"], "reconcile", context.caller))
    try:
        await asyncio.wait_for(claimed.wait(), 2)
        inspected = await context.runner.get_operation(result["operation_id"])
        assert inspected["state"] == "preparing" and not inspected["cleanup_confirmed"]
        with pytest.raises(OperationConflict, match="observer cleanup"):
            context.state.claim_operation_observer(result["operation_id"])
    finally:
        resume.set()
        launched = await observer
        await finish(context, launched["execution_id"])


async def test_retained_cleanup_preserves_live_operation_preparation(context, monkeypatch):
    """The recurring old-lease sweep must not settle a newly admitted operation."""
    from src.scripts import script_runner

    script(context)
    preparing, resume = asyncio.Event(), asyncio.Event()

    async def prepare(*args, **kwargs):
        preparing.set()
        await resume.wait()

    monkeypatch.setattr(script_runner, "ensure_deps_installed", prepare)
    pending = asyncio.create_task(launch(context))
    try:
        await asyncio.wait_for(preparing.wait(), 2)
        before = context.state.list_operations("task")[0]
        assert before["state"] == "preparing"
        assert before["execution_id"]
        assert not context.runner._active
        await context.runner.reconcile_handoffs()
        after = await context.runner.get_operation(before["operation_id"])
        assert after["state"] == "preparing"
        assert after["cleanup_confirmed"] is False
        assert context.state.active_script_resources()
        with pytest.raises(OperationConflict, match="observer cleanup"):
            context.state.claim_operation_observer(before["operation_id"])
    finally:
        resume.set()
        execution_id = await pending
        result = await finish(context, execution_id)
    assert result["state"] == "succeeded"


async def test_terminal_inspection_recovers_interrupted_capacity_release(context, monkeypatch):
    from src.operations.host_capacity import HostCapacity
    from src.scripts import managed_operations

    budget = HostCapacity(context.workspace / "capacity.sqlite", {
        "enabled": True, "budgets": {"host": {"limit": 1, "per_office": 1}}, "default_costs": {"host": 1},
    })
    context.runner._operation_host_capacity = budget
    script(context)
    release = managed_operations.release_capacity
    monkeypatch.setattr(managed_operations, "release_capacity", lambda *_: None)
    result = await finish(context, await launch(context))
    assert budget.status()["counts"]["reserved"] == 1
    monkeypatch.setattr(managed_operations, "release_capacity", release)
    await context.runner.get_operation(result["operation_id"])
    assert budget.status()["counts"].get("reserved", 0) == 0


def test_operation_receipt_rejects_fifo_without_opening_a_blocking_reader(tmp_path, monkeypatch):
    import os
    from src.scripts.managed_operations import read_receipt

    os.mkfifo(tmp_path / "operation-result.json")
    monkeypatch.setattr(Path, "read_text", lambda *_: pytest.fail("Nonregular receipt must never reach a blocking reader"))
    with pytest.raises(ValueError, match="regular file"):
        read_receipt(tmp_path)


async def test_reconcile_capacity_wait_cannot_cancel_the_remote_run(context):
    from src.operations.host_capacity import HostCapacity, HostCapacityUnavailable

    path = script(context, name="external", manifest="operation_mode: external\noperation_reconcile_entry_point: reconcile.py\noperation_cancel_entry_point: cancel.py\n",
                  body="import os,json\nfrom pathlib import Path\nPath(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':{'service':'fake','run_id':'one'},'state':'running'}))\n")
    (path / "reconcile.py").write_text("print('observed')\n")
    (path / "cancel.py").write_text("raise AssertionError('capacity must prevent launch')\n")
    result = await finish(context, await launch(context, name="external"))
    budget = HostCapacity(context.workspace / "capacity.sqlite", {
        "enabled": True, "budgets": {"host": {"limit": 1, "per_office": 1}}, "default_costs": {"host": 1},
    })
    budget.reserve("another-operation", "another-office", [])
    context.runner._operation_host_capacity = budget
    for action in ("reconcile", "cancel"):
        with pytest.raises(HostCapacityUnavailable):
            await context.runner.control_operation(result["operation_id"], action, context.caller)
        retained = context.state.get_operation(result["operation_id"])
        assert retained["state"] == "unknown" and retained["external_ref"] == result["external_ref"]
    assert not context.runner._active


async def test_concurrent_completion_observers_commit_only_once(context):
    script(context)
    execution_id = await launch(context)
    execution = context.runner._active[execution_id]
    code = await execution.process.wait()
    entered, resume = asyncio.Event(), asyncio.Event()
    observe = execution.operation_observer
    calls = 0

    async def paused_observer(state, exit_code):
        nonlocal calls
        calls += 1
        entered.set()
        await resume.wait()
        await observe(state, exit_code)

    execution.operation_observer = paused_observer
    async def complete():
        await on_complete(execution, code, context.runner._active, context.workspace, None,
                          active_by_task=context.runner._active_by_task)
    first = asyncio.create_task(complete())
    await entered.wait()
    second = asyncio.create_task(complete())
    await asyncio.sleep(0)
    resume.set()
    await asyncio.gather(first, second)
    assert calls == 1


async def test_lost_observer_does_not_trust_workspace_success_file(context):
    path = script(context)
    record, _ = context.state.begin_operation(task_id="task", cycle=1, phase="execute", key="lost",
        fingerprint="a" * 64, script_name="local", attempt_id="attempt", mechanism="local", resources=[])
    context.state.update_operation(record["operation_id"], execution_id="lost-execution", state="running")
    execution_dir = path / "executions/lost-execution"
    execution_dir.mkdir(parents=True)
    (execution_dir / "status.json").write_text(json.dumps({"status": "completed", "exit_code": 0}))
    result = await context.runner.get_operation(record["operation_id"])
    assert result["state"] == "unknown" and result["exit_code"] is None


async def test_startup_recovers_only_unlaunched_observer_and_completed_capacity(context):
    from src.operations.host_capacity import HostCapacity
    from src.scripts.operation_control import reconcile_operations

    budget = HostCapacity(context.workspace / "capacity.sqlite", {
        "enabled": True, "budgets": {"host": {"limit": 5, "per_office": 5}}, "default_costs": {"host": 1},
    })
    context.runner._operation_host_capacity = budget
    ids = {}
    for key in ("unlaunched", "completed", "external", "live-preparation"):
        record, _ = context.state.begin_operation(task_id=key, cycle=1, phase="execute", key=key,
            fingerprint="a" * 64, script_name="local", attempt_id="attempt",
            mechanism="external" if key == "external" else "local", resources=[])
        ids[key] = record["operation_id"]
        budget.reserve(record["operation_id"], "office", [])
    context.state.update_operation(ids["completed"], state="succeeded", cleanup_confirmed=True, exit_code=0)
    context.state.update_operation(ids["external"], receipt={"external_ref": {"service": "fake", "run_id": "one"}})
    context.runner._starting_by_task["live-preparation"] = 1
    await reconcile_operations(context.runner)
    assert context.state.get_operation(ids["unlaunched"])["state"] == "failed"
    external = context.state.get_operation(ids["external"])
    assert external["state"] == "unknown" and external["cleanup_confirmed"]
    assert not context.state.get_operation(ids["live-preparation"])["cleanup_confirmed"]
    assert set(budget.list_reserved_operations("office")) == {ids["external"], ids["live-preparation"]}


async def test_cross_phase_control_and_missing_external_cancel_are_refused(context):
    path = script(context, name="external", manifest="operation_mode: external\noperation_reconcile_entry_point: reconcile.py\n",
                  body="import os,json\nfrom pathlib import Path\nPath(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':{'service':'fake','run_id':'one'},'state':'running'}))\n")
    (path / "reconcile.py").write_text("raise RuntimeError('must not start')\n")
    result = await finish(context, await launch(context, name="external"))
    with pytest.raises(OperationConflict, match="current task cycle and phase"):
        await context.runner.control_operation(result["operation_id"], "cancel", {**context.caller, "task_mode": "review"})
    with pytest.raises(OperationConflict, match="does not support cancel"):
        await context.runner.control_operation(result["operation_id"], "cancel", context.caller)
    assert not context.runner._active


@pytest.mark.parametrize("reported", ["succeeded", "failed", "cancelled"])
async def test_external_outcome_without_run_identity_stays_unknown(context, reported):
    script(context, name="external", manifest="operation_mode: external\noperation_reconcile_entry_point: reconcile.py\n",
           body="import os,json\nfrom pathlib import Path\nPath(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'state':" + repr(reported) + "}))\n")
    result = await finish(context, await launch(context, name="external"))
    assert result["state"] == "unknown" and result["external_ref"] is None
    with pytest.raises(OperationConflict, match="identity is unavailable"):
        await context.runner.control_operation(result["operation_id"], "reconcile", context.caller)
    with pytest.raises(OperationConflict, match="resource"):
        await launch(context, name="external", key="unsafe-retry")


async def test_external_cancel_uses_original_identity_and_keeps_capacity_until_confirmed(context):
    from src.operations.host_capacity import HostCapacity

    context.runner._operation_host_capacity = HostCapacity(context.workspace / "capacity.sqlite", {
        "enabled": True, "budgets": {"host": {"limit": 1, "per_office": 1}}, "default_costs": {"host": 1},
    })
    path = script(context, name="external", manifest=(
        "operation_mode: external\noperation_reconcile_entry_point: reconcile.py\noperation_cancel_entry_point: cancel.py\n"
    ), body="import os,json\nfrom pathlib import Path\nPath(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':{'service':'fake','run_id':'one'},'state':'running'}))\n")
    (path / "cancel.py").write_text(
        "import os,json\nfrom pathlib import Path\n"
        "context=json.loads(Path(os.environ['CUBICLE_OPERATION_CONTEXT']).read_text())\n"
        "assert context['action']=='cancel'\nassert context['external_ref']['run_id']=='one'\n"
        "Path(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'state':'cancelled'}))\n"
    )
    result = await finish(context, await launch(context, name="external"))
    assert result["state"] == "unknown"
    assert context.runner._operation_host_capacity.status()["counts"].get("reserved", 0) == 1
    resumed = await context.runner.control_operation(result["operation_id"], "cancel", context.caller)
    result = await finish(context, resumed["execution_id"])
    assert result["state"] == "cancelled" and result["external_ref"]["run_id"] == "one"
    assert context.runner._operation_host_capacity.status()["counts"].get("reserved", 0) == 0


async def test_changed_external_receipt_cannot_finish_another_job(context):
    path = script(context, name="external", manifest="operation_mode: external\noperation_reconcile_entry_point: reconcile.py\n",
                  body="import os,json\nfrom pathlib import Path\nPath(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':{'service':'fake','run_id':'one'},'state':'running'}))\n")
    (path / "reconcile.py").write_text(
        "import os,json\nfrom pathlib import Path\n"
        "Path(os.environ['CUBICLE_OPERATION_RESULT']).write_text(json.dumps({'external_ref':"
        "{'service':'fake','run_id':'different'},'state':'succeeded'}))\n"
    )
    result = await finish(context, await launch(context, name="external"))
    resumed = await context.runner.control_operation(result["operation_id"], "reconcile", context.caller)
    result = await finish(context, resumed["execution_id"])
    assert result["state"] == "unknown" and result["external_ref"]["run_id"] == "one"


@pytest.mark.parametrize("symlink_kind", ["entrypoint", "module_directory"])
async def test_symlinked_authored_source_cannot_escape_fingerprint(context, symlink_kind):
    path = script(context)
    if symlink_kind == "entrypoint":
        (path / "main.py").unlink()
        target = context.workspace / "mutable.py"
        target.write_text("print('done')\n")
        (path / "main.py").symlink_to(target)
    else:
        target = context.workspace / "mutable-lib"
        target.mkdir()
        (path / "lib").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        await launch(context)
    assert not context.runner._active and not context.state.list_operations("task")


async def test_capacity_wait_retries_same_intent_after_release(context):
    from src.operations.host_capacity import HostCapacity, HostCapacityUnavailable

    context.runner._operation_host_capacity = HostCapacity(context.workspace / "capacity.sqlite", {
        "enabled": True, "budgets": {"host": {"limit": 1, "per_office": 1}}, "default_costs": {"host": 1},
    })
    script(context, name="first", body="import time\ntime.sleep(30)\n")
    script(context, name="second")
    first = await launch(context, name="first", key="first")
    with pytest.raises(HostCapacityUnavailable):
        await launch(context, name="second", key="queued")
    queued = context.state.list_operations("task")[0]
    assert queued["state"] == "queued" and queued["cleanup_confirmed"]
    await context.runner.get_operation(queued["operation_id"])
    assert context.runner._operation_host_capacity.status()["counts"].get("waiting", 0) == 1
    assert len(context.runner._active) == 1
    await context.runner.kill(first)
    second = await launch(context, name="second", key="queued")
    result = await finish(context, second)
    assert result["operation_id"] == queued["operation_id"]
    assert result["state"] == "succeeded"


async def test_queued_cancel_cannot_mark_a_concurrent_launch_clean(context):
    record, _ = context.state.begin_operation(task_id="task", cycle=1, phase="execute", key="queued",
        fingerprint="a" * 64, script_name="local", attempt_id="attempt", mechanism="local", resources=[])
    context.state.update_operation(record["operation_id"], state="queued", cleanup_confirmed=True)

    async def another_launch_during_validation(*_):
        context.state.update_operation(record["operation_id"], state="preparing", execution_id="new-execution", cleanup_confirmed=False)
        return {"execution_resources": []}

    context.runner._assert_task_runnable = another_launch_during_validation
    with pytest.raises(OperationConflict, match="Queued operation changed"):
        await context.runner.control_operation(record["operation_id"], "cancel", context.caller)
    current = context.state.get_operation(record["operation_id"])
    assert current["state"] == "preparing" and not current["cleanup_confirmed"]


async def test_uncertain_child_cleanup_retains_operation(context):
    script(context, body="import time\ntime.sleep(30)\n")
    execution_id = await launch(context)
    execution = context.runner._active[execution_id]
    cleanup = execution.resource_lease.confirm_stopped
    execution.resource_lease.confirm_stopped = AsyncMock(side_effect=RuntimeError("unreachable runtime"))
    with pytest.raises(RuntimeError):
        await context.runner.kill(execution_id)
    result = context.state.list_operations("task")[0]
    assert result["cleanup_confirmed"] is False
    with pytest.raises(OperationConflict, match="resource"):
        await launch(context, key="unsafe-retry")
    execution.resource_lease.confirm_stopped = cleanup
    await context.runner.kill(execution_id)
    result = await context.runner.get_operation(result["operation_id"])
    assert result["state"] == "cancelled" and result["cleanup_confirmed"]


async def test_stale_cycle_cannot_control_external_operation(context):
    script(context, body="print('done')\n")
    execution_id = await launch(context)
    await finish(context, execution_id)
    operation = context.state.list_operations("task")[0]
    with pytest.raises(OperationConflict, match="cycle and phase"):
        await context.runner.control_operation(operation["operation_id"], "cancel", {**context.caller, "execution_cycle": 2})


async def test_external_identity_cannot_be_replaced(context):
    script(context)
    operation = await finish(context, await launch(context))
    context.state.update_operation(operation["operation_id"], receipt={"external_ref": {"service": "fake", "run_id": "original"}})
    with pytest.raises(OperationConflict, match="cannot switch"):
        context.state.update_operation(operation["operation_id"], receipt={"external_ref": {"service": "fake", "run_id": "different"}})


async def test_monitor_marks_lost_exit_unknown_even_with_success_log(tmp_path, monkeypatch):
    from src.scripts import script_execution
    from datetime import datetime, timezone

    (tmp_path / "log.txt").write_text("SUCCESS all done")
    execution = SimpleNamespace(process=SimpleNamespace(returncode=None, pid=123),
                                exec_dir=tmp_path, script_name="local", exec_id="one", started_at=datetime.now(timezone.utc))
    sleep_calls = 0

    async def tick(_):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    completed = AsyncMock()
    monkeypatch.setattr(script_execution.asyncio, "sleep", tick)
    monkeypatch.setattr(script_execution, "_resolve_exit_code_via_waitpid", lambda _: -1)
    monkeypatch.setattr(script_execution, "on_complete", completed)
    await script_execution.monitor_all({"one": execution}, str(tmp_path), 10, None)
    assert completed.await_args.kwargs["exit_unknown"] is True


def test_operation_intent_survives_process_restart_and_does_not_store_raw_inputs(tmp_path):
    state = RuntimeState(tmp_path / "runtime.sqlite", "one")
    args = dict(task_id="task", cycle=1, phase="execute", key="report", fingerprint="a" * 64,
                script_name="report", attempt_id="attempt", mechanism="local", resources=["report-output"])
    record, created = state.begin_operation(**args)
    assert created
    again, created = RuntimeState(state.database_path, "one").begin_operation(**args)
    assert again["operation_id"] == record["operation_id"] and not created
    assert RuntimeState(state.database_path, "another-office").get_operation(record["operation_id"]) is None
    assert set(record) >= {"origin_attempt_id", "cycle", "phase", "cleanup_confirmed"}
    assert "variables" not in record


def test_new_timing_columns_upgrade_existing_operation_ledger_without_losing_intent(tmp_path):
    state = RuntimeState(tmp_path / "runtime.sqlite", "one")
    record, _ = state.begin_operation(task_id="task", cycle=1, phase="review", key="report", fingerprint="a" * 64,
                script_name="report", attempt_id="attempt", mechanism="local", resources=[])
    with state._connection() as connection:
        connection.execute("ALTER TABLE managed_operations DROP COLUMN stage")
        connection.execute("ALTER TABLE managed_operations DROP COLUMN observer_fingerprint")
    reopened = RuntimeState(state.database_path, "one").get_operation(record["operation_id"])
    assert reopened["operation_id"] == record["operation_id"]
    assert reopened["stage"] == "verification" and reopened["observer_fingerprint"] is None


@pytest.mark.parametrize("payload", [
    {"key": "report", "input_fingerprint": "not-a-digest"},
    {"key": "report", "input_fingerprint": "a" * 64, "credentials": "secret"},
    {"key": "report", "input_fingerprint": "a" * 64, "stage": "approve"},
    {"key": "report", "input_fingerprint": "a" * 64, "stage": {}},
])
def test_operation_spec_refuses_unbounded_metadata(payload):
    with pytest.raises(ValueError):
        operation_spec(payload)


@pytest.mark.parametrize("payload", [
    {"external_ref": {"service": "fake", "run_id": "https://host/?token=secret"}},
    {"artifact_refs": ["../private/credentials"]},
    {"response_body": "sensitive"},
    {"state": {}},
])
def test_adapter_receipt_accepts_references_only(payload):
    with pytest.raises(ValueError):
        operation_result(payload)


@pytest.mark.parametrize("name", ["CUBICLE_OPERATION_ID", "CUBICLE_OPERATION_CONTEXT", "CUBICLE_OPERATION_RESULT"])
def test_operation_identity_and_receipt_paths_cannot_be_declared_as_variables(name):
    from pydantic import ValidationError
    from src.scripts.manifest import ManifestVariable

    with pytest.raises(ValidationError, match="reserved"):
        ManifestVariable(name=name, type="string")


def test_mechanism_fingerprint_frames_file_names_and_contents(context):
    from src.scripts.managed_operations import _fingerprint

    path = script(context)
    (path / "a").write_text("bc")
    first = _fingerprint(path, {}, {})
    (path / "a").unlink()
    (path / "ab").write_text("c")
    assert _fingerprint(path, {}, {}) != first


def test_mechanism_fingerprint_rejects_special_files(context):
    import os
    from src.scripts.managed_operations import _fingerprint

    path = script(context)
    os.mkfifo(path / "source.py")
    with pytest.raises(ValueError, match="regular"):
        _fingerprint(path, {}, {})


@pytest.mark.parametrize("limit", ["MAX_SOURCE_ENTRIES", "MAX_SOURCE_DEPTH"])
def test_mechanism_fingerprint_bounds_empty_source_metadata(context, monkeypatch, limit):
    from src.scripts import operation_fingerprint

    path = script(context)
    if limit == "MAX_SOURCE_ENTRIES":
        monkeypatch.setattr(operation_fingerprint, limit, 3)
        (path / "empty-one").touch()
        (path / "empty-two").touch()
    else:
        monkeypatch.setattr(operation_fingerprint, limit, 1)
        (path / "one/two").mkdir(parents=True)
    with pytest.raises(ValueError, match="budget"):
        operation_fingerprint.fingerprint(path, {}, {})
