"""Script resource ownership, including parent exit and uncertain cleanup."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.runtime_state import RuntimeState
from src.script_resource_state import ScriptResourceConflict
from src.scripts.script_execution import on_complete
from src.scripts.script_runner import ScriptRunner
from src.scripts.secrets_store import SecretsStore
from src.scripts.variable_manager import VariableManager


@pytest.fixture
async def context(tmp_path, monkeypatch):
    state = RuntimeState(tmp_path / "private" / "runtime.sqlite", "office")
    supervisor = AgentSupervisor(str(tmp_path), "office", container_name="a" * 64)
    supervisor.set_runtime_state(state)
    supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 4, "max_workers_per_profile": 2}
    )
    runner = ScriptRunner(
        str(tmp_path),
        SecretsStore(str(tmp_path)),
        VariableManager(str(tmp_path)),
        container_name="a" * 64,
    )
    runner.set_runtime_state(state)
    runner.set_resource_supervisor(supervisor)
    supervisor.set_script_resource_provider(runner.unleased_resources)
    runner._assert_task_runnable = AsyncMock(return_value=None)
    from src.docker import task_process_cleanup

    cleanup = AsyncMock()
    monkeypatch.setattr(task_process_cleanup, "terminate_worker_execution", cleanup)
    monkeypatch.setattr(
        task_process_cleanup, "_confirmed_container_stopped", lambda _: False
    )
    from src.scripts import script_runner

    monkeypatch.setattr(script_runner, "chown_to_agent", lambda _: None)
    monkeypatch.setattr(script_runner, "ensure_deps_installed", AsyncMock())
    commands = []

    async def spawn(*argv, **kwargs):
        commands.append((argv, kwargs))
        env = kwargs["env"]
        path = (
            tmp_path
            / ".scripts"
            / env["CUBICLE_SCRIPT_NAME"]
            / "executions"
            / env["CUBICLE_EXECUTION_ID"]
        )
        (path / "in_container.pid").write_text("123")
        process = MagicMock(returncode=None, pid=123)

        async def wait():
            process.returncode = 0
            return 0

        process.wait = AsyncMock(side_effect=wait)
        return process

    monkeypatch.setattr(
        script_runner.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn)
    )
    for name in ("one", "two"):
        path = tmp_path / ".scripts" / name
        path.mkdir(parents=True)
        (path / "script.yaml").write_text("description: test script\n")
        (path / "main.py").write_text("print('ok')\n")
    yield SimpleNamespace(
        state=state,
        supervisor=supervisor,
        runner=runner,
        cleanup=cleanup,
        commands=commands,
        workspace=tmp_path,
    )
    for execution in runner._active.values():
        execution.log_handle.close()


def add_worker(ctx, attempt="parent", resources=None):
    worker = AgentProcess(
        agent_name="analyst",
        role="worker",
        state=AgentState.WORKING,
        current_task_id="task",
        execution_task_id="task",
        execution_attempt_id=attempt,
        execution_resources=["shared-workspace"] if resources is None else resources,
    )
    ctx.supervisor._agents[attempt] = worker
    return worker


def add_current_parent(ctx):
    worker = AgentProcess(
        agent_name="analyst", role="worker", state=AgentState.WORKING,
        current_task_id="task", execution_task_id="task", execution_attempt_id="parent",
        agent_instance_id="instance", profile_id="profile", execution_generation=1,
        execution_cycle=1, execution_mode="execute", execution_assignee="analyst",
        execution_resources=["shared-workspace"], process=MagicMock(returncode=None),
    )
    ctx.supervisor._agents[worker.runtime_key] = worker
    caller = ctx.supervisor._execution_event(worker, {})["_caller"]
    return worker, caller


async def test_manual_script_refuses_worker_resource_before_any_preparation(context):
    add_worker(context)
    with pytest.raises(ScriptResourceConflict, match="shared resource"):
        await context.runner.execute("one")
    assert context.commands == []
    assert context.state.active_script_resources() == []


@pytest.mark.parametrize("restart", [False, True])
async def test_disabled_policy_retains_script_gate_until_prior_lease_is_released(context, restart):
    context.state.begin_script_resource_lease(
        lease_id="prior", script_name="two", task_id="", parent_attempt_id="",
        resources=["shared-workspace"], execution_id="prior-execution",
        marker="c" * 64, container_id="a" * 64,
    )
    supervisor = context.supervisor
    if restart:
        supervisor = AgentSupervisor(str(context.workspace), "office", container_name="a" * 64)
        supervisor.set_runtime_state(RuntimeState(context.state.database_path, "office"))
        supervisor.set_script_resource_provider(context.runner.unleased_resources)
        context.runner.set_resource_supervisor(supervisor)
    assert supervisor.set_execution_policy({"enabled": False}) is False
    assert supervisor.execution_policy["enabled"] is True
    assert supervisor.config_ready is False
    with pytest.raises(ScriptResourceConflict, match="configuration is not fully applied"):
        await context.runner.execute("one")
    assert context.commands == []
    assert len(context.state.active_script_resources()) == 1
    context.state.set_script_resource_state("prior", "released")
    assert supervisor.set_execution_policy({"enabled": False}) is True
    assert supervisor.execution_policy["enabled"] is False
    assert supervisor.config_ready is True
    assert context.state.active_script_resources() == []


async def test_late_parent_can_finish_child_script_and_disabled_policy_applies_automatically(context, monkeypatch):
    from tests.test_config_materialization_recovery import register

    parent, caller = add_current_parent(context)
    handler, supervisor, applied, _ = register(
        monkeypatch, context.workspace, MagicMock(), supervisor=context.supervisor
    )
    await handler({"config": {"agent_execution_policy": {"enabled": False}}})
    assert not supervisor.config_ready
    execution_id = await context.runner.execute("one", task_id="task", execution_caller=caller)
    assert len(context.commands) == 1
    assert context.runner._assert_task_runnable.await_count == 2
    assert all(call.args == ("task", caller) for call in context.runner._assert_task_runnable.await_args_list)
    with pytest.raises(ScriptResourceConflict, match="shared resource"):
        await context.runner.execute("two", task_id="task", execution_caller=caller)
    parent.process.returncode = 0
    parent.state = AgentState.IDLE
    parent.current_task_id = None
    await asyncio.sleep(.01)
    assert not supervisor.config_ready  # child ownership outlives its parent
    execution = context.runner._active[execution_id]
    execution.process.returncode = 0
    await on_complete(execution, 0, context.runner._active, context.workspace, None,
                      active_by_task=context.runner._active_by_task)
    await asyncio.wait_for(applied.wait(), timeout=1)
    assert supervisor.config_ready and not supervisor.execution_policy["enabled"]
    assert context.state.active_script_resources() == []
    await supervisor._config_reconciler.close()


@pytest.mark.parametrize("invalid", ["manual", "manager", "stale", "wrong_phase", "stopped", "orphan", "new_worker", "generic_config_failure", "malformed_policy", "paused_config", "retry_closed"])
async def test_policy_drain_continuation_requires_exact_live_parent(context, invalid):
    parent, caller = add_current_parent(context)
    assert context.supervisor.set_execution_policy({"enabled": False}) is False
    task_id = "task"
    if invalid == "manual":
        caller, task_id = None, None
    elif invalid == "manager":
        caller = {**caller, "role": "manager"}
    elif invalid == "stale":
        caller = {**caller, "execution_generation": 0}
    elif invalid == "wrong_phase":
        caller = {**caller, "task_mode": "review"}
    elif invalid == "stopped":
        parent.stop_requested = True
    elif invalid == "orphan":
        parent.process = None
    elif invalid == "new_worker":
        caller = {**caller, "agent_instance_id": "different-instance"}
    elif invalid == "malformed_policy":
        with pytest.raises(ValueError):
            context.supervisor.set_execution_policy({"enabled": "invalid"})
    elif invalid == "paused_config":
        context.supervisor.pause_configuration()
    elif invalid == "retry_closed":
        from src.config_sync.retry import ConfigSyncRetry

        retry = ConfigSyncRetry(AsyncMock(), context.supervisor.pause_configuration, MagicMock())
        await retry.close()
    else:
        context.supervisor.set_execution_policy({"enabled": True}, ready=False)
    with pytest.raises(ScriptResourceConflict, match="configuration is not fully applied"):
        await context.runner.execute("one", task_id=task_id, execution_caller=caller)
    assert context.commands == []
    assert context.state.active_script_resources() == []


@pytest.mark.parametrize("enabled", [False, True])
async def test_cancelled_initial_preflight_retains_no_phantom_resource(
    context, enabled
):
    context.supervisor.set_execution_policy({"enabled": enabled})
    entered = asyncio.Event()

    async def preflight(*_):
        entered.set()
        await asyncio.Event().wait()

    context.runner._assert_task_runnable.side_effect = preflight
    pending = asyncio.create_task(context.runner.execute("one", task_id="task"))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not context.runner.has_active_scripts("task")
    assert context.runner.unleased_resources() == []
    assert context.state.active_script_resources() == []
    assert context.commands == []
    assert context.state.maintenance_status()["pending_admissions"] == 0


@pytest.mark.parametrize("initial_enabled", [False, True])
async def test_policy_toggle_during_script_preflight_preserves_launch_ownership(
    context,
    initial_enabled,
):
    context.supervisor.set_execution_policy({"enabled": initial_enabled})
    entered = asyncio.Event()
    resume = asyncio.Event()
    calls = 0

    async def preflight(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await resume.wait()

    context.runner._assert_task_runnable.side_effect = preflight
    pending = asyncio.create_task(context.runner.execute("one", task_id="task"))
    await entered.wait()
    context.supervisor.set_execution_policy({"enabled": not initial_enabled})
    resume.set()
    execution_id = await pending
    context.supervisor.set_execution_policy({"enabled": True})
    # An enabled launch retains a lease through disablement; a legacy launch
    # that encounters enablement before preparation adopts a lease atomically.
    assert context.runner._active[execution_id].resource_lease is not None
    assert not context.supervisor.resources_available(
        {}, {"execution_resources": ["shared-workspace"]}
    )


async def test_parent_may_launch_own_script_but_lease_outlives_worker(context):
    add_worker(context)
    execution_id = await context.runner.execute(
        "one",
        task_id="task",
        execution_caller={"attempt_id": "parent", "role": "worker"},
    )
    lease = context.state.active_script_resources()[0]
    assert lease["parent_attempt_id"] == "parent"
    assert lease["execution_id"] == execution_id
    assert (
        context.commands[0][1]["env"]["CUBICLE_WORKER_EXECUTION_ID"] == lease["marker"]
    )
    context.supervisor._agents.clear()
    assert not context.supervisor.resources_available(
        {}, {"execution_resources": ["shared-workspace"]}
    )
    with pytest.raises(ScriptResourceConflict):
        await context.runner.execute("two")
    execution = context.runner._active[execution_id]
    execution.process.returncode = 0
    await on_complete(
        execution,
        0,
        context.runner._active,
        context.workspace,
        None,
        active_by_task=context.runner._active_by_task,
    )
    context.cleanup.assert_awaited_once_with("a" * 64, lease["marker"])
    assert context.state.active_script_resources() == []
    assert context.supervisor.resources_available(
        {}, {"execution_resources": ["shared-workspace"]}
    )


async def test_sibling_script_cannot_borrow_parent_exemption(context):
    add_worker(context)
    caller = {"attempt_id": "parent", "role": "worker"}
    await context.runner.execute("one", task_id="task", execution_caller=caller)
    with pytest.raises(ScriptResourceConflict):
        await context.runner.execute("two", task_id="task", execution_caller=caller)
    assert len(context.commands) == 1


async def test_cleanup_failure_keeps_resource_and_execution_until_retry(context):
    execution_id = await context.runner.execute("one")
    execution = context.runner._active[execution_id]
    execution.process.returncode = 0
    context.cleanup.side_effect = RuntimeError("Docker unavailable")
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        await on_complete(execution, 0, context.runner._active, context.workspace, None)
    assert execution_id in context.runner._active
    assert context.state.active_script_resources()[0]["state"] == "uncertain"
    assert not context.supervisor.resources_available(
        {}, {"execution_resources": ["shared-workspace"]}
    )
    context.cleanup.side_effect = None
    await on_complete(execution, 0, context.runner._active, context.workspace, None)
    assert context.state.active_script_resources() == []


async def test_missing_launch_ack_does_not_treat_marker_absence_as_cleanup(context):
    execution_id = await context.runner.execute("one")
    execution = context.runner._active[execution_id]
    execution.process.returncode = 0
    (execution.exec_dir / "in_container.pid").unlink()
    with pytest.raises(RuntimeError, match="acknowledgement"):
        await on_complete(execution, 0, context.runner._active, context.workspace, None)
    assert context.state.active_script_resources()[0]["state"] == "uncertain"


async def test_restart_reconciles_exact_marker_before_releasing_durable_lease(context):
    execution_id = await context.runner.execute("one", task_id="task")
    before = context.state.active_script_resources()[0]
    execution = context.runner._active.pop(execution_id)
    execution.log_handle.close()
    context.runner._active_by_task.clear()
    context.runner.set_runtime_state(
        RuntimeState(context.state.database_path, "office")
    )
    await context.runner.reconcile_handoffs()
    context.cleanup.assert_awaited_once_with("a" * 64, before["marker"])
    assert context.runner._runtime_state.active_script_resources() == []
    assert context.runner._runtime_state.script_handoffs("task")[0]["state"] == "failed"


async def test_script_monitor_retries_retained_cleanup_without_another_restart(
    context, monkeypatch
):
    from src.scripts import script_execution

    context.state.observe_cycle("task", 1)
    execution_id = await context.runner.execute("one", task_id="task")
    execution = context.runner._active.pop(execution_id)
    execution.log_handle.close()
    context.runner._active_by_task.clear()
    # The old execution must never acquire the successor's handoff cycle.
    context.state.observe_cycle("task", 2)
    context.cleanup.side_effect = [RuntimeError("Docker temporarily unavailable"), None]
    reconciled = [asyncio.Event(), asyncio.Event()]
    original_reconcile = context.runner.reconcile_handoffs
    sweeps = 0

    async def reconcile():
        nonlocal sweeps
        await original_reconcile()
        reconciled[sweeps].set()
        sweeps += 1

    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            await asyncio.wait_for(reconciled[ticks - 2].wait(), timeout=1)
        if ticks == 3:
            raise asyncio.CancelledError

    context.runner.reconcile_handoffs = reconcile
    monkeypatch.setattr(
        script_execution.asyncio, "sleep", tick,
    )

    await context.runner.monitor_all()

    assert context.cleanup.await_count == 2
    assert context.state.active_script_resources() == []
    assert context.state.unresolved_scripts() == []
    assert context.state.script_handoffs("task") == []
    assert not context.runner.has_active_scripts("task")
    assert json.loads((execution.exec_dir / "status.json").read_text())["status"] == "failed"
    assert context.supervisor.resources_available({}, {"execution_resources": None})


async def test_slow_recovery_does_not_block_live_monitor_and_is_cancelled_on_shutdown(
    context, monkeypatch
):
    from src.scripts import script_execution

    execution_id = await context.runner.execute("one", task_id="task")
    context.runner._active[execution_id].process.returncode = 0
    recovery_started, recovery_cancelled, completed = (asyncio.Event() for _ in range(3))

    async def slow_recovery():
        recovery_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            recovery_cancelled.set()

    async def complete(execution, *args, **kwargs):
        await asyncio.wait_for(recovery_started.wait(), timeout=1)
        completed.set()

    ticks = 0

    async def tick(_):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            assert completed.is_set()
            raise asyncio.CancelledError

    context.runner.reconcile_handoffs = slow_recovery
    monkeypatch.setattr(script_execution, "on_complete", complete)
    monkeypatch.setattr(script_execution.asyncio, "sleep", tick)
    await asyncio.wait_for(context.runner.monitor_all(), timeout=2)
    assert completed.is_set() and recovery_cancelled.is_set()


@pytest.mark.parametrize("raw", ['{"status":', '[]', 'null', '{"status":[]}', '\\udcff'])
async def test_recovery_preserves_corrupt_status_without_stranding_stopped_lease(context, raw):
    execution_id = await context.runner.execute("one", task_id="task")
    execution = context.runner._active.pop(execution_id)
    execution.log_handle.close()
    context.runner._active_by_task.clear()
    encoded = raw.encode("utf-8", errors="surrogateescape")
    (execution.exec_dir / "status.json").write_bytes(encoded)
    context.runner._router = AsyncMock()
    await context.runner.reconcile_handoffs()
    assert context.state.active_script_resources() == []
    assert not context.runner.has_active_scripts("task")
    assert json.loads((execution.exec_dir / "status.json").read_text())["status"] == "failed"
    preserved = list(execution.exec_dir.glob("status.corrupt-*.json"))
    assert len(preserved) == 1 and preserved[0].read_bytes() == encoded
    assert context.runner._router.publish_event.await_args.args[0]["status"] == "failed"


async def test_recovery_does_not_reap_a_live_script_preparation(context, monkeypatch):
    from src.scripts import script_runner

    preparing = asyncio.Event()
    continue_preparation = asyncio.Event()

    async def prepare(*args, **kwargs):
        preparing.set()
        await continue_preparation.wait()

    monkeypatch.setattr(script_runner, "ensure_deps_installed", prepare)
    launch = asyncio.create_task(context.runner.execute("one", task_id="task"))
    try:
        await asyncio.wait_for(preparing.wait(), timeout=1)
        await context.runner.reconcile_handoffs()
        context.cleanup.assert_not_awaited()
        assert len(context.state.active_script_resources()) == 1
        assert not context.supervisor.resources_available({}, {"execution_resources": None})
    finally:
        continue_preparation.set()
        await launch
    await context.runner.reconcile_handoffs()
    context.cleanup.assert_not_awaited()
    assert context.runner._starting_resource_leases == set()
    assert context.runner._uncertain_tasks == set()


async def test_failed_dynamic_launch_uses_its_durable_lease_without_leaking_admission(context):
    context.runner._execute_v2 = AsyncMock(side_effect=RuntimeError("Launch failed"))
    context.cleanup.side_effect = RuntimeError("Docker unavailable")
    with pytest.raises(RuntimeError, match="Launch failed"):
        await context.runner.execute("one", task_id="task")
    assert len(context.state.active_script_resources()) == 1
    assert context.runner.has_active_scripts("task")
    assert context.state.maintenance_status()["pending_admissions"] == 0
    context.cleanup.side_effect = None
    await context.runner.reconcile_handoffs()
    assert context.state.active_script_resources() == []
    assert not context.runner.has_active_scripts("task")


async def test_periodic_recovery_does_not_turn_live_legacy_script_into_uncertainty(context):
    assert context.supervisor.set_execution_policy({"enabled": False})
    execution_id = await context.runner.execute("one", task_id="task")
    execution = context.runner._active[execution_id]
    await context.runner.reconcile_handoffs()
    assert context.runner._uncertain_tasks == set()
    execution.process.returncode = 0
    await on_complete(
        execution, 0, context.runner._active, context.workspace, None,
        active_by_task=context.runner._active_by_task,
    )
    assert not context.runner.has_active_scripts("task")


async def test_recovered_status_clears_only_the_settled_task_hold(context):
    settled_id = "exec-2026-09-22T00-00-00-abcdef"
    pending_id = "exec-2026-09-22T00-00-01-abcdef"
    for task_id, execution_id, status in (
        ("settled", settled_id, "failed"), ("pending", pending_id, "running")
    ):
        context.state.note_script(task_id, execution_id, "running")
        context.runner._uncertain_tasks.add(task_id)
        path = context.workspace / ".scripts" / "one" / "executions" / execution_id
        path.mkdir(parents=True)
        (path / "status.json").write_text(json.dumps({"task_id": task_id, "status": status}))
    await context.runner.reconcile_handoffs()
    assert context.runner._uncertain_tasks == {"pending"}
    assert not context.runner.has_active_scripts("settled")
    assert context.runner.has_active_scripts("pending")


async def test_concurrent_manual_scripts_reserve_atomically_before_spawn(context):
    result = await asyncio.gather(
        context.runner.execute("one"),
        context.runner.execute("two"),
        return_exceptions=True,
    )
    assert sum(isinstance(value, ScriptResourceConflict) for value in result) == 1
    assert len(context.commands) == 1
    assert len(context.state.active_script_resources()) == 1


async def test_explicit_independent_tasks_can_launch_parallel_scripts(context):
    context.runner._assert_task_runnable.return_value = {"execution_resources": []}
    results = await asyncio.gather(
        context.runner.execute("one", task_id="a"),
        context.runner.execute("two", task_id="b"),
    )
    assert len(set(results)) == 2
    assert len(context.state.active_script_resources()) == 2
    assert context.supervisor.resources_available(
        {}, {"execution_resources": ["shared-workspace"]}
    )


async def test_task_resource_change_during_preparation_refuses_launch(context):
    context.runner._assert_task_runnable.side_effect = [
        {"execution_resources": []},
        {"execution_resources": ["repo:x"]},
    ]
    with pytest.raises(RuntimeError, match="declarations changed"):
        await context.runner.execute("one", task_id="task")
    assert context.commands == []
    assert context.state.active_script_resources() == []


async def test_failed_manifest_releases_known_unstarted_resource(context):
    (context.workspace / ".scripts" / "one" / "script.yaml").unlink()
    with pytest.raises(Exception):
        await context.runner.execute("one")
    assert context.commands == []
    assert context.state.active_script_resources() == []


async def test_policy_adoption_observes_legacy_running_and_starting_scripts(context):
    context.runner._active["legacy"] = SimpleNamespace(
        resource_lease=None, script_name="one"
    )
    assert not context.supervisor.resources_available(
        {}, {"execution_resources": ["shared-workspace"]}
    )
    context.runner._active.clear()
    context.runner._legacy_starting = 1
    assert not context.supervisor.resources_available(
        {}, {"execution_resources": ["shared-workspace"]}
    )
    context.runner._legacy_starting = 0
    assert context.supervisor.resources_available(
        {}, {"execution_resources": ["shared-workspace"]}
    )


def test_private_leases_survive_restart_and_are_office_scoped(tmp_path):
    state = RuntimeState(tmp_path / "state.sqlite", "office")
    state.begin_script_resource_lease(
        lease_id="lease",
        script_name="one",
        task_id="",
        parent_attempt_id="",
        resources=["repo:a"],
        execution_id="execution",
        marker="a" * 64,
        container_id="b" * 64,
    )
    resumed = RuntimeState(state.database_path, "office")
    assert resumed.active_script_resources()[0]["resources"] == ["repo:a"]
    assert (
        RuntimeState(state.database_path, "other-office").active_script_resources()
        == []
    )
    with pytest.raises(ScriptResourceConflict):
        resumed.begin_script_resource_lease(
            lease_id="second",
            script_name="two",
            task_id="",
            parent_attempt_id="",
            resources=["repo:a"],
            execution_id="second",
            marker="c" * 64,
            container_id="b" * 64,
        )
    resumed.set_script_resource_state("lease", "released")
    assert resumed.active_script_resources() == []


async def test_dependencies_and_script_share_one_durable_cleanup_marker(
    context, monkeypatch
):
    from src.scripts import script_runner
    from src.scripts.deps_installer import DepsCleanupUnconfirmed

    (context.workspace / ".scripts" / "one" / "requirements.txt").write_text(
        "synthetic-no-provider\n"
    )
    deps = AsyncMock(side_effect=DepsCleanupUnconfirmed("daemon unreachable"))
    monkeypatch.setattr(script_runner, "ensure_deps_installed", deps)
    with pytest.raises(DepsCleanupUnconfirmed):
        await context.runner.execute("one", task_id="task")
    lease = context.state.active_script_resources()[0]
    assert deps.await_args.kwargs["execution_marker"] == lease["marker"]
    assert lease["preparation_started"] == 1
    assert lease["state"] == "uncertain"
    assert context.commands == []
    assert context.runner.has_active_scripts("task")


async def test_completion_receipt_failure_does_not_release_resource(
    context, monkeypatch
):
    execution_id = await context.runner.execute("one", task_id="task")
    execution = context.runner._active[execution_id]
    execution.process.returncode = 0
    note_script = context.state.note_script
    monkeypatch.setattr(
        context.state,
        "note_script",
        MagicMock(side_effect=OSError("private disk unavailable")),
    )
    with pytest.raises(OSError):
        await on_complete(execution, 0, context.runner._active, context.workspace, None)
    assert context.state.active_script_resources()[0]["state"] == "stopped"
    assert execution_id in context.runner._active
    monkeypatch.setattr(context.state, "note_script", note_script)
    await on_complete(execution, 0, context.runner._active, context.workspace, None)
    assert context.state.active_script_resources() == []
    context.cleanup.assert_awaited_once()


async def test_cancelled_launch_waits_for_client_before_exact_cleanup(
    context, monkeypatch
):
    from src.scripts import script_runner

    entered = asyncio.Event()
    release = asyncio.Event()
    spawn = script_runner.asyncio.create_subprocess_exec.side_effect

    async def delayed_spawn(*argv, **kwargs):
        entered.set()
        await release.wait()
        return await spawn(*argv, **kwargs)

    monkeypatch.setattr(
        script_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=delayed_spawn),
    )
    launching = asyncio.create_task(context.runner.execute("one", task_id="task"))
    await entered.wait()
    launching.cancel()
    await asyncio.sleep(0)
    assert context.state.active_script_resources()
    context.cleanup.assert_not_awaited()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await launching
    context.cleanup.assert_awaited_once()
    assert context.state.active_script_resources() == []
    assert not context.runner.has_active_scripts("task")


async def test_retry_stop_cleans_untracked_durable_script(context):
    execution_id = await context.runner.execute("one", task_id="task")
    execution = context.runner._active.pop(execution_id)
    execution.log_handle.close()
    context.runner._active_by_task.clear()
    context.cleanup.side_effect = RuntimeError("daemon unreachable")
    with pytest.raises(RuntimeError):
        await context.runner.kill(execution_id)
    assert context.state.active_script_resources()
    context.cleanup.side_effect = None
    assert await context.runner.kill(execution_id)
    assert context.state.active_script_resources() == []


@pytest.mark.parametrize("failure", [False, True])
async def test_dynamic_startup_legacy_cleanup_never_marks_unknown_as_failed(
    tmp_path, monkeypatch, failure
):
    from src.scripts import script_execution, script_resources

    execution_id = "exec-2026-09-21T12-00-00-abcdef"
    path = tmp_path / ".scripts" / "one" / "executions" / execution_id
    path.mkdir(parents=True)
    (path / "status.json").write_text(json.dumps({"status": "running"}))
    (path / "in_container.pid").write_text("123")
    cleanup = AsyncMock(side_effect=RuntimeError("cannot verify") if failure else None)
    monkeypatch.setattr(script_resources, "terminate_legacy_script_execution", cleanup)
    legacy_pid_kill = AsyncMock()
    monkeypatch.setattr(script_execution, "_docker_exec_kill", legacy_pid_kill)
    if failure:
        with pytest.raises(RuntimeError, match="cannot verify"):
            await script_execution.reconcile_orphaned_executions(
                str(tmp_path), "a" * 64, require_confirmed=True
            )
        assert json.loads((path / "status.json").read_text())["status"] == "running"
    else:
        assert (
            await script_execution.reconcile_orphaned_executions(
                str(tmp_path), "a" * 64, require_confirmed=True
            )
            == 1
        )
    cleanup.assert_awaited_once_with("a" * 64, execution_id)
    legacy_pid_kill.assert_not_awaited()


async def test_startup_leased_script_is_left_for_private_marker_recovery(
    tmp_path, monkeypatch
):
    from src.scripts import script_execution, script_resources

    execution_id = "exec-2026-09-21T12-00-00-abcdef"
    path = tmp_path / ".scripts" / "one" / "executions" / execution_id
    path.mkdir(parents=True)
    (path / "status.json").write_text(json.dumps({"status": "running"}))
    cleanup = AsyncMock()
    monkeypatch.setattr(script_resources, "terminate_legacy_script_execution", cleanup)
    assert (
        await script_execution.reconcile_orphaned_executions(
            str(tmp_path),
            "a" * 64,
            require_confirmed=True,
            leased_execution_ids={execution_id},
        )
        == 0
    )
    cleanup.assert_not_awaited()
    assert json.loads((path / "status.json").read_text())["status"] == "running"


async def test_legacy_cleanup_helper_uses_validated_exec_marker_and_pidfds(monkeypatch):
    from src.scripts import script_resources

    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"", b""))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(script_resources.asyncio, "create_subprocess_exec", spawn)
    execution_id = "exec-2026-09-21T12-00-00-abcdef"
    await script_resources.terminate_legacy_script_execution("a" * 64, execution_id)
    assert spawn.call_args.args[-1] == execution_id
    program = process.communicate.await_args.args[0]
    assert b"CUBICLE_EXECUTION_ID=" in program
    assert b"pidfd_send_signal" in program
    assert b"CUBICLE_WORKER_EXECUTION_ID=" not in program
    with pytest.raises(RuntimeError, match="immutable container"):
        await script_resources.terminate_legacy_script_execution(
            "mutable-name", execution_id
        )
    with pytest.raises(RuntimeError):
        await script_resources.terminate_legacy_script_execution("a" * 64, "../sibling")
    assert spawn.await_count == 1


async def test_untracked_lease_does_not_report_disk_terminal_status_as_confirmed(
    context,
):
    execution_id = await context.runner.execute("one")
    execution = context.runner._active.pop(execution_id)
    execution.log_handle.close()
    (execution.exec_dir / "status.json").write_text(json.dumps({"status": "completed"}))
    status = await context.runner.get_status(execution_id)
    assert status["status"] == "unknown"
    assert status["resource_cleanup_pending"] is True


@pytest.mark.parametrize("sibling", [None, "same-task", "other-task"])
async def test_legacy_recovery_commits_failed_status_and_clears_only_its_hold(
    context, monkeypatch, sibling
):
    from src.scripts import script_resources

    execution_id = "exec-2026-09-21T12-00-00-abcdef"
    path = context.workspace / ".scripts" / "one" / "executions" / execution_id
    path.mkdir(parents=True)
    (path / "status.json").write_text(
        json.dumps({"status": "running", "task_id": "task"})
    )
    (path / "in_container.pid").write_text("123")
    context.state.note_script("task", execution_id, "running", cycle=0)
    context.runner._uncertain_scripts.add(execution_id)
    context.runner._uncertain_tasks.add("task")
    if sibling:
        sibling_id = "exec-2026-09-21T12-00-01-123456"
        sibling_task = "task" if sibling == "same-task" else "unrelated"
        sibling_path = (
            context.workspace / ".scripts" / "two" / "executions" / sibling_id
        )
        sibling_path.mkdir(parents=True)
        (sibling_path / "status.json").write_text(
            json.dumps({"status": "running", "task_id": sibling_task})
        )
        context.state.note_script(sibling_task, sibling_id, "running", cycle=0)
        context.runner._uncertain_scripts.add(sibling_id)
        context.runner._uncertain_tasks.add(sibling_task)

    async def cleanup_exact(_, identifier):
        if identifier != execution_id:
            raise RuntimeError("sibling remains uncertain")

    cleanup = AsyncMock(side_effect=cleanup_exact)
    monkeypatch.setattr(script_resources, "terminate_legacy_script_execution", cleanup)
    await context.runner.reconcile_handoffs()
    assert json.loads((path / "status.json").read_text())["status"] == "failed"
    assert execution_id not in context.runner._uncertain_scripts
    assert (
        next(
            receipt
            for receipt in context.state.script_handoffs("task")
            if receipt["execution_id"] == execution_id
        )["state"]
        == "failed"
    )
    if sibling == "same-task":
        assert context.runner._uncertain_tasks == {"task"}
    elif sibling == "other-task":
        assert context.runner._uncertain_tasks == {"unrelated"}
    else:
        assert context.runner._uncertain_tasks == set()
        assert context.runner.unleased_resources() == []
    # Another pass must not resurrect the cleared task from stale disk data.
    await context.runner.reconcile_handoffs()
    assert ("task" in context.runner._uncertain_tasks) is (sibling == "same-task")


async def test_legacy_cleanup_receipt_failure_retains_its_uncertainty(
    context, monkeypatch
):
    from src.scripts import script_resources

    execution_id = "exec-2026-09-21T12-00-00-abcdef"
    path = context.workspace / ".scripts" / "one" / "executions" / execution_id
    path.mkdir(parents=True)
    (path / "status.json").write_text(
        json.dumps({"status": "running", "task_id": "task"})
    )
    (path / "in_container.pid").write_text("123")
    context.state.note_script("task", execution_id, "running")
    context.runner._uncertain_scripts.add(execution_id)
    context.runner._uncertain_tasks.add("task")
    monkeypatch.setattr(
        script_resources, "terminate_legacy_script_execution", AsyncMock()
    )
    monkeypatch.setattr(
        context.state,
        "note_script",
        MagicMock(side_effect=OSError("private state unavailable")),
    )
    with pytest.raises(OSError):
        await context.runner._reconcile_legacy_script(execution_id)
    assert execution_id in context.runner._uncertain_scripts
    assert context.runner._uncertain_tasks == {"task"}


async def test_script_admission_rechecks_config_readiness_under_shared_lock(context):
    await context.supervisor.admission_lock.acquire()
    admission = asyncio.create_task(context.runner.execute("one"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    context.supervisor.set_execution_policy(
        context.supervisor.execution_policy, ready=False
    )
    context.supervisor.admission_lock.release()
    with pytest.raises(
        ScriptResourceConflict, match="configuration is not fully applied"
    ):
        await admission
    assert context.commands == []
    assert context.state.active_script_resources() == []
