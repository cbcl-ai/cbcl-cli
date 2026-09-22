"""Task identity and capacity remain isolated through cleanup and restart."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.agent_instance_workspace import prepare_instance_workspace
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.runtime_state import RuntimeState
from src.tool_proxy_identity import ProxySessionRegistry


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = RuntimeState(tmp_path / "private" / "runtime.sqlite", "office")
    supervisor = AgentSupervisor(
        str(workspace), "office", container_name="test-container"
    )
    supervisor.set_runtime_state(state)
    supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 3, "max_workers_per_profile": 2}
    )
    supervisor.set_execution_releaser(AsyncMock())
    supervisor.set_tool_proxy("http://proxy", "", sessions=ProxySessionRegistry())
    for method in (
        "_wait_for_ready",
        "_send_to_agent",
        "_reader_loop",
        "_monitor_exit",
        "_heartbeat_loop",
    ):
        monkeypatch.setattr(supervisor, method, AsyncMock())
    processes = []

    async def spawn(*args, **kwargs):
        process = MagicMock(pid=100 + len(processes), returncode=None)
        process.wait = AsyncMock(return_value=0)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    cleanup = AsyncMock()
    monkeypatch.setattr(
        "src.docker.task_process_cleanup.terminate_worker_execution", cleanup
    )
    profiles = {}
    instances = {}

    async def claim(name, task, attempt_id):
        profile_id = profiles.setdefault(name, str(uuid4()))
        instance_id = instances.setdefault((name, task["task_id"]), str(uuid4()))
        request = {
            "attempt_id": attempt_id,
            "execution_mode": "execute",
            "expected_assigned_agent": name,
        }
        state.begin_worker_claim(name, task["task_id"], request)
        receipt = {
            "attempt_id": attempt_id,
            "agent_name": name,
            "agent_instance_id": instance_id,
            "profile_id": profile_id,
            "execution_cycle": 1,
            "execution_generation": 1,
            "expected_assigned_agent": name,
            "runtime_release_required": True,
            "profile_revision": "frozen-revision",
            "effective_agent_config": {"name": name, "skills": [], "allowed_tools": []},
        }
        state.record_worker_claim(attempt_id, receipt)
        return receipt

    supervisor.set_execution_claimer(claim)
    return supervisor, state, cleanup, processes


def task():
    return {
        "task_id": str(uuid4()),
        "status": "in_progress",
        "workstream_short_code": "WS-001",
    }


async def test_same_profile_siblings_have_distinct_identity_and_exact_stop(runtime):
    supervisor, state, cleanup, processes = runtime
    first, second = task(), task()
    assert await supervisor.spawn_worker("engineer", {}, first)
    assert await supervisor.spawn_worker("engineer", {}, second)
    a = supervisor.get_task_agent("engineer", first["task_id"])
    b = supervisor.get_task_agent("engineer", second["task_id"])
    assert a.agent_instance_id != b.agent_instance_id
    assert a.profile_id == b.profile_id
    assert a.execution_attempt_id != b.execution_attempt_id
    assert len(state.pending_worker_executions()) == 2
    assert supervisor.get_agent_current_task("engineer") is None
    assert not supervisor.profile_can_spawn("engineer")
    identity_a = supervisor._execution_event(a, {})["_caller"]
    assert supervisor.execution_is_current(identity_a, first["task_id"])
    assert not supervisor.execution_is_current(
        {**identity_a, "agent_instance_id": b.agent_instance_id}, first["task_id"]
    )
    assert await supervisor.stop_task("engineer", first["task_id"])
    cleanup.assert_awaited_once()
    assert cleanup.await_args.args[1] != b.execution_marker
    processes[0].terminate.assert_called_once()
    processes[1].terminate.assert_not_called()
    assert supervisor.is_task_busy("engineer", second["task_id"])
    assert supervisor.get_all_statuses()["engineer"]["status"] == "working"
    assert supervisor.get_all_statuses()["engineer"]["running_count"] == 1
    assert not supervisor.execution_is_current(identity_a, first["task_id"])
    assert len(state.pending_worker_executions()) == 1
    await supervisor.stop_task("engineer", second["task_id"])


async def test_duplicate_task_is_not_admitted_while_sibling_capacity_exists(runtime):
    supervisor, _, _, processes = runtime
    work = task()
    assert await supervisor.spawn_worker("engineer", {}, work)
    assert not await supervisor.spawn_worker("engineer", {}, work)
    assert len(processes) == 1
    await supervisor.stop_task("engineer", work["task_id"])


async def test_policy_toggle_preserves_active_sibling_and_pending_cleanup_capacity(
    runtime,
):
    supervisor, state, cleanup, _ = runtime
    first, second, third = task(), task(), task()
    assert await supervisor.spawn_worker("engineer", {}, first)
    assert await supervisor.spawn_worker("engineer", {}, second)
    policy = supervisor.execution_policy
    supervisor.set_execution_policy({**policy, "enabled": False})
    cleanup.side_effect = RuntimeError("cleanup unavailable")
    with pytest.raises(RuntimeError, match="cleanup unavailable"):
        await supervisor.stop_task("engineer", first["task_id"])
    assert supervisor.is_task_busy("engineer", first["task_id"])
    assert supervisor.is_task_busy("engineer", second["task_id"])
    assert not await supervisor.spawn_worker("engineer", {}, third)
    supervisor.set_execution_policy(policy)
    assert not await supervisor.spawn_worker("engineer", {}, third)
    assert len(state.pending_worker_executions()) == 2
    cleanup.side_effect = None
    await supervisor.stop_task("engineer", first["task_id"])
    assert supervisor.is_task_busy("engineer", second["task_id"])
    assert await supervisor.spawn_worker("engineer", {}, third)
    await supervisor.stop_task("engineer", second["task_id"])
    await supervisor.stop_task("engineer", third["task_id"])


async def test_pool_capacity_refusal_releases_claim_without_orphaned_journal(runtime):
    from src.docker.execution_ledger import ExecutionCapacityUnavailable

    supervisor, state, _, processes = runtime
    pool = SimpleNamespace(
        available=AsyncMock(return_value=True),
        task_available=AsyncMock(return_value=True),
        prepare=AsyncMock(side_effect=ExecutionCapacityUnavailable("pool full")),
        stop_attempt=AsyncMock(),
    )
    supervisor.set_execution_containers(pool)
    supervisor._on_event = AsyncMock()
    work = task()
    assert not await supervisor.spawn_worker("engineer", {}, work)
    assert not processes
    supervisor._execution_releaser.assert_awaited_once()
    supervisor._on_event.assert_not_awaited()
    assert not state.pending_worker_executions()

    pool.prepare.side_effect = None
    pool.prepare.return_value = SimpleNamespace(container_id="c" * 64)
    assert await supervisor.spawn_worker("engineer", {}, work)
    assert len(state.pending_worker_executions()) == 1
    await supervisor.stop_task("engineer", work["task_id"])


async def test_snapshot_failure_before_pool_prepare_releases_only_the_backend_claim(
    runtime, monkeypatch
):
    supervisor, state, _, processes = runtime
    pool = SimpleNamespace(
        available=AsyncMock(return_value=True),
        task_available=AsyncMock(return_value=True),
        prepare=AsyncMock(),
        stop_attempt=AsyncMock(side_effect=RuntimeError("No pool attempt exists")),
    )
    supervisor.set_execution_containers(pool)
    supervisor._on_event = AsyncMock()
    monkeypatch.setattr(
        "src.agent_instance_workspace.prepare_instance_workspace",
        MagicMock(side_effect=RuntimeError("Snapshot temporarily unavailable")),
    )
    assert not await supervisor.spawn_worker("engineer", {}, task())
    assert not processes
    pool.prepare.assert_not_awaited()
    pool.stop_attempt.assert_not_awaited()
    supervisor._execution_releaser.assert_awaited_once()
    assert state.pending_worker_executions() == []
    assert state.pending_completions() == []
    assert supervisor.profile_can_spawn("engineer")


async def test_snapshot_cancellation_needs_no_physical_cleanup_before_launch(runtime, monkeypatch):
    import threading

    supervisor, state, cleanup, _ = runtime
    pool = SimpleNamespace(
        available=AsyncMock(return_value=True), task_available=AsyncMock(return_value=True),
        prepare=AsyncMock(), stop_attempt=AsyncMock(),
    )
    supervisor.set_execution_containers(pool)
    cleanup.side_effect = RuntimeError("Docker temporarily unavailable")
    entered = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()

    def snapshot(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(timeout=2)
        return "/workspace/agents/.instances/synthetic"

    monkeypatch.setattr("src.agent_instance_workspace.prepare_instance_workspace", snapshot)
    admission = asyncio.create_task(supervisor.spawn_worker("engineer", {}, task()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        admission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await admission
    finally:
        finish.set()
    cleanup.assert_not_awaited()
    pool.prepare.assert_not_awaited()
    pool.stop_attempt.assert_not_awaited()
    supervisor._execution_releaser.assert_awaited_once()
    assert state.pending_worker_executions() == []


@pytest.mark.parametrize("wait_stage", ["office_budget", "retained_task", "atomic_budget"])
async def test_pool_wait_does_not_consume_watchdog_crash_budget(runtime, wait_stage):
    from src.docker.execution_ledger import ExecutionCapacityUnavailable
    from src.watchdog import TaskWatchdog

    supervisor, state, _, processes = runtime
    pool = SimpleNamespace(
        available=AsyncMock(return_value=wait_stage != "office_budget"),
        task_available=AsyncMock(return_value=wait_stage != "retained_task"),
        prepare=AsyncMock(side_effect=ExecutionCapacityUnavailable("pool full")),
        stop_attempt=AsyncMock(),
    )
    supervisor.set_execution_containers(pool)
    dispatcher = MagicMock(add_task=AsyncMock())
    watchdog = TaskWatchdog(
        ws=AsyncMock(), executor=None, manager=MagicMock(), config_store=MagicMock(),
        task_queue=None, office_id="office", supervisor=supervisor,
        dispatcher=dispatcher, runtime_state=state,
    )
    work = task()
    for _ in range(5):
        assert not await supervisor.spawn_worker("engineer", {}, work)
        watchdog._recently_dispatched.clear()
        await watchdog._handle_in_progress(
            {**work, "id": work["task_id"], "assigned_agent": "engineer"}
        )
        assert watchdog._task_crash_count == {}
    assert not processes and not state.pending_worker_executions()
    assert dispatcher.add_task.await_count == 5
    pool.available.return_value = pool.task_available.return_value = True
    pool.prepare.side_effect = None
    pool.prepare.return_value = SimpleNamespace(container_id="c" * 64)
    assert await supervisor.spawn_worker("engineer", {}, work)
    assert not supervisor.execution_is_deferred(work["task_id"])
    await supervisor.stop_task("engineer", work["task_id"])


async def test_cancelled_admission_releases_claim_and_preserves_no_phantom_run(runtime):
    supervisor, state, _, _ = runtime
    waiting = asyncio.Event()

    async def wait_for_ready(*args):
        waiting.set()
        await asyncio.Event().wait()

    supervisor._wait_for_ready = wait_for_ready
    work = task()
    admission = asyncio.create_task(supervisor.spawn_worker("engineer", {}, work))
    await waiting.wait()
    admission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await admission
    supervisor._execution_releaser.assert_awaited_once()
    assert not state.pending_worker_executions()
    await supervisor.retry_pending_cleanup()
    assert not supervisor.is_task_busy("engineer", work["task_id"])


async def test_cancelled_admission_retains_stop_until_release_recovers(runtime):
    supervisor, state, _, _ = runtime
    waiting = asyncio.Event()

    async def wait_for_ready(*args):
        waiting.set()
        await asyncio.Event().wait()

    supervisor._wait_for_ready = wait_for_ready
    supervisor._execution_releaser.side_effect = RuntimeError("release offline")
    supervisor._on_event = AsyncMock()
    work = task()
    admission = asyncio.create_task(supervisor.spawn_worker("engineer", {}, work))
    await waiting.wait()
    admission.cancel()
    with pytest.raises(RuntimeError, match="release offline"):
        await admission
    pending = state.pending_worker_executions()
    assert len(pending) == 1 and pending[0]["stop_requested"]
    assert supervisor.is_task_busy("engineer", work["task_id"])
    supervisor._execution_releaser.side_effect = None
    await supervisor.retry_pending_cleanup()
    assert not state.pending_worker_executions()
    supervisor._on_event.assert_not_awaited()


async def test_cleanup_failure_and_lost_release_ack_hold_capacity(runtime):
    supervisor, state, cleanup, _ = runtime
    supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 1, "max_workers_per_profile": 1}
    )
    work = task()
    assert await supervisor.spawn_worker("engineer", {}, work)
    agent = supervisor.get_task_agent("engineer", work["task_id"])
    cleanup.side_effect = RuntimeError("cleanup uncertain")
    with pytest.raises(RuntimeError, match="cleanup uncertain"):
        await supervisor.stop_task("engineer", work["task_id"])
    assert not supervisor.profile_can_spawn("other-profile")
    supervisor._execution_releaser.assert_not_awaited()
    cleanup.side_effect = None
    supervisor._execution_releaser.side_effect = RuntimeError(
        "release acknowledgment lost"
    )
    await supervisor.retry_pending_cleanup()
    assert agent.cleanup_pending
    assert not supervisor.profile_can_spawn("other-profile")
    assert state.pending_worker_executions()
    supervisor._execution_releaser.side_effect = None
    await supervisor.retry_pending_cleanup()
    assert not agent.cleanup_pending
    assert supervisor.profile_can_spawn("other-profile")
    assert not state.pending_worker_executions()


async def test_repeated_stop_retries_backend_release_after_marker_is_gone(runtime):
    supervisor, state, cleanup, processes = runtime
    work = task()
    assert await supervisor.spawn_worker("engineer", {}, work)
    agent = supervisor.get_task_agent("engineer", work["task_id"])
    releaser = supervisor._execution_releaser
    releaser.side_effect = RuntimeError("release unavailable")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="release unavailable"):
            await supervisor.stop_task("engineer", work["task_id"])
        assert agent.execution_marker == ""
        assert agent.cleanup_pending
        assert not agent.runtime_released
        assert state.pending_worker_executions()
        processes[0].returncode = 0
    assert releaser.await_count == 2
    cleanup.assert_awaited_once()
    releaser.side_effect = None
    assert await supervisor.stop_task("engineer", work["task_id"])
    assert agent.runtime_released
    assert not state.pending_worker_executions()


async def test_restart_recovers_both_same_profile_attempts_and_exact_outcomes(runtime):
    supervisor, state, cleanup, _ = runtime
    first, second = task(), task()
    await supervisor.spawn_worker("engineer", {}, first)
    await supervisor.spawn_worker("engineer", {}, second)
    markers = {agent.execution_marker for agent in supervisor._agents.values()}
    restarted = AgentSupervisor(
        supervisor._workspace,
        "office",
        container_name="test-container",
        on_event=AsyncMock(),
    )
    restarted.set_runtime_state(RuntimeState(state.database_path, "office"))
    restarted.set_execution_policy(supervisor.execution_policy)
    restarted.set_execution_releaser(AsyncMock())
    assert not restarted.profile_can_spawn("engineer")
    assert restarted.is_task_busy("engineer", first["task_id"])
    await restarted.retry_pending_cleanup()
    assert {call.args[1] for call in cleanup.await_args_list} == markers
    assert restarted._on_event.await_count == 2
    assert restarted._execution_releaser.await_count == 2
    assert not state.pending_worker_executions()
    assert restarted.profile_can_spawn("engineer")


async def test_lost_claim_response_keeps_capacity_until_exact_recovery(runtime):
    supervisor, state, _, processes = runtime
    supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 1, "max_workers_per_profile": 1}
    )
    work = task()
    request = {
        "attempt_id": str(uuid4()),
        "execution_mode": "execute",
        "expected_assigned_agent": "engineer",
    }
    state.begin_worker_claim("engineer", work["task_id"], request)
    assert not supervisor.profile_can_spawn("another-profile")
    assert supervisor.is_task_busy("engineer", work["task_id"])
    supervisor.set_execution_recoverer(
        AsyncMock(
            return_value={
                **request,
                "agent_name": "engineer",
                "agent_instance_id": str(uuid4()),
                "profile_id": str(uuid4()),
                "execution_cycle": 1,
                "execution_generation": 1,
                "runtime_release_required": True,
            }
        )
    )
    await supervisor.retry_pending_cleanup()
    supervisor._execution_releaser.assert_awaited_once()
    assert not processes
    assert not state.pending_worker_executions()


@pytest.mark.parametrize("code", ["execution_capacity_busy", "execution_resource_busy"])
async def test_backend_admission_wait_retries_without_spending_crash_budget(
    runtime,
    monkeypatch,
    code,
):
    from functools import partial
    import httpx
    from src.execution_claim import claim_worker_execution, recover_worker_claim
    from src.watchdog import TaskWatchdog

    supervisor, state, _, processes = runtime
    work = task()
    successful_claim = supervisor._execution_claimer
    client_type = httpx.AsyncClient

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(400, json={"code": code, "detail": "occupied"})
        if "/execution-attempts/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "status": "in_progress",
                "assigned_agent": "engineer",
                "execution_generation": 0,
                "execution_cycle": 1,
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    connection = {
        "platform_url": "http://platform",
        "office_id": "office",
        "security_token": "token",
        "office_tool_secret": "owner",
    }
    supervisor.set_execution_claimer(
        partial(
            claim_worker_execution,
            runtime_state=state,
            **connection,
        )
    )
    supervisor.set_execution_recoverer(partial(recover_worker_claim, **connection))
    dispatcher = MagicMock(add_task=AsyncMock())
    watchdog = TaskWatchdog(
        ws=AsyncMock(),
        executor=None,
        manager=MagicMock(),
        config_store=MagicMock(),
        task_queue=None,
        office_id="office",
        supervisor=supervisor,
        dispatcher=dispatcher,
    )
    for _ in range(5):
        assert not await supervisor.spawn_worker("engineer", {}, work)
        assert state.pending_worker_executions()
        await supervisor.retry_pending_cleanup()
        assert not state.pending_worker_executions()
        watchdog._recently_dispatched.clear()
        await watchdog._handle_in_progress(
            {**work, "id": work["task_id"], "assigned_agent": "engineer"}
        )
        assert watchdog._task_crash_count == {}
    assert not processes
    assert dispatcher.add_task.await_count == 5
    # An unrelated refusal cannot inherit the previous capacity exemption.
    supervisor.set_execution_claimer(
        AsyncMock(side_effect=RuntimeError("invalid claim"))
    )
    assert not await supervisor.spawn_worker("engineer", {}, work)
    assert not supervisor.execution_is_deferred(work["task_id"])
    supervisor.set_execution_claimer(successful_claim)
    assert await supervisor.spawn_worker("engineer", {}, work)
    assert not supervisor.execution_is_deferred(work["task_id"])
    await supervisor.stop_task("engineer", work["task_id"])


def test_instance_snapshot_survives_profile_and_skill_edits(tmp_path):
    workspace, archive = tmp_path / "workspace", tmp_path / "private"
    skill = workspace / ".claude/skills/design"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("original skill")
    work = {
        **task(),
        "agent_instance_id": str(uuid4()),
        "profile_id": str(uuid4()),
        "profile_revision": "v1",
        "agent_execution_policy": {"enabled": True},
    }
    profile = {
        "name": "designer",
        "skills": [{"name": "design"}],
        "claude_md_content": "original playbook",
    }
    cwd = prepare_instance_workspace(str(workspace), archive, profile, work)
    target = workspace / Path(cwd).relative_to("/workspace")
    original = (target / "CLAUDE.md").read_text()
    (skill / "SKILL.md").write_text("new skill")
    (target / ".claude/skills/design/SKILL.md").write_text(
        "worker changed its local copy"
    )
    profile["claude_md_content"] = "new profile text"
    prepare_instance_workspace(str(workspace), archive, profile, work)
    assert (target / "CLAUDE.md").read_text() == original
    assert (target / ".claude/skills/design/SKILL.md").read_text() == "original skill"
    from src.config_sync.claude_md_writer import ClaudeMdWriter
    from src.config_sync.workspace_setup import WorkspaceSetup

    ClaudeMdWriter(str(workspace)).sync_agent_directories([profile])
    WorkspaceSetup(str(workspace)).sync_agent_workspaces([profile])
    assert target.is_dir()


def test_invalid_policy_closes_admission(runtime):
    supervisor, _, _, _ = runtime
    with pytest.raises(ValueError):
        supervisor.set_execution_policy({"enabled": True, "max_workers": 0})
    assert not supervisor.config_ready
    assert not supervisor.profile_can_spawn("engineer")


def test_unchanged_legacy_policy_keeps_existing_default_and_disable_drains_enabled_work(tmp_path):
    supervisor = AgentSupervisor(str(tmp_path), "office")
    assert supervisor.set_execution_policy({"enabled": False}) is True
    holder = AgentProcess("reader", "worker", state=AgentState.WORKING)
    supervisor._agents[holder.runtime_key] = holder
    assert supervisor.set_execution_policy({"enabled": False}) is True
    assert supervisor.config_ready
    assert supervisor.set_execution_policy({"enabled": True}) is True
    assert supervisor.set_execution_policy({"enabled": False}) is False
    assert not supervisor.config_ready
    assert supervisor.execution_policy["enabled"]
    holder.state = AgentState.IDLE
    assert supervisor.set_execution_policy({"enabled": False}) is True
    assert not supervisor.execution_policy["enabled"]


@pytest.mark.parametrize("profile", [
    {}, {"allowed_tools": []}, {"allowed_tools": ["Read"]},
    {"allowed_tools": ["Bash(git *)"]}, {"allowed_tools": ["Write"]},
])
def test_resources_serialize_default_workers_but_allow_declared_independent_work(runtime, profile):
    supervisor, _, _, _ = runtime
    holder = AgentProcess(
        "developer",
        "worker",
        state=AgentState.WORKING,
        execution_resources=["shared-workspace", "test-database"],
    )
    supervisor._agents[holder.runtime_key] = holder
    assert not supervisor.resources_available(
        profile, task()
    )
    assert not supervisor.resources_available(
        {}, {**task(), "execution_resources": ["test-database"]}
    )
    assert supervisor.resources_available(
        profile, {**task(), "execution_resources": []}
    )
    assert supervisor.resources_available(
        {}, {**task(), "execution_resources": ["isolated-checkout-b"]}
    )
    holder.state = AgentState.IDLE
    assert supervisor.resources_available(profile, task())


def test_retained_skill_archive_rejects_external_symlink(tmp_path):
    workspace, archive = tmp_path / "workspace", tmp_path / "private"
    skill = workspace / ".claude/skills/design"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill")
    outside = tmp_path / "outside.txt"
    outside.write_text("not a selected skill")
    (skill / "escape.txt").symlink_to(outside)
    work = {
        **task(),
        "agent_instance_id": str(uuid4()),
        "profile_id": str(uuid4()),
        "profile_revision": "v1",
    }
    with pytest.raises(ValueError, match="external symlink"):
        prepare_instance_workspace(
            str(workspace), archive, {"skills": [{"name": "design"}]}, work
        )
