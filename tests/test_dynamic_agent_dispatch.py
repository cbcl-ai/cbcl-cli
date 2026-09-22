"""Offline admission, queue and task-recovery contracts for dynamic Agents."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from src._handlers._tasks import route_task_kill, route_task_updated
from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.task_dispatcher import TaskDispatcher
from src.watchdog import TaskWatchdog


@pytest.fixture
async def queue():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield AgentQueueManager(redis, "office")
    await redis.aclose()


def task(task_id, profile="analyst", **kwargs):
    return {"task_id": task_id, "assigned_agent": profile, "status": "ready", **kwargs}


def runtime(queue, *, profiles=("analyst",), max_workers=4, per_profile=2):
    """A process-free supervisor double enforcing the real capacity contract."""
    supervisor = MagicMock()
    supervisor.execution_policy = {
        "enabled": True,
        "max_workers": max_workers,
        "max_workers_per_profile": per_profile,
    }
    supervisor.admission_lock = asyncio.Lock()
    running = {}
    supervisor.get_task_agent.side_effect = lambda name, task_id: running.get(
        (name, task_id)
    )
    supervisor.is_task_busy.side_effect = (
        lambda name, task_id: (name, task_id) in running
    )
    supervisor.is_agent_busy.side_effect = lambda name: any(
        profile == name for profile, _ in running
    )
    supervisor.profile_can_spawn.side_effect = (
        lambda name: len(running) < max_workers
        and sum(profile == name for profile, _ in running) < per_profile
    )
    supervisor.resources_available.return_value = True
    supervisor.get_all_statuses.return_value = {}
    supervisor.get_instance_statuses.side_effect = lambda: {
        worker.agent_instance_id: {"agent_name": profile, "task_id": task_id}
        for (profile, task_id), worker in running.items()
    }

    async def spawn(profile, config, task_data, **options):
        assert supervisor.admission_lock.locked()
        assert options["admission_locked"] is True
        task_id = task_data["task_id"]
        running[(profile, task_id)] = SimpleNamespace(
            agent_instance_id=f"agent-{task_id}",
            execution_attempt_id=f"attempt-{task_id}",
            execution_generation=1,
            pid=100 + len(running),
        )
        await asyncio.sleep(0)  # allow competing admissions to interleave
        return True

    supervisor.spawn_worker = AsyncMock(side_effect=spawn)
    config = MagicMock()
    config.agents = [{"name": name, "is_active": True} for name in profiles]
    config.get_agent.side_effect = lambda name: {
        "name": name,
        "allowed_tools": ["Read"],
    }
    dispatcher = TaskDispatcher(queue._redis, "office", supervisor, config, queue)
    dispatcher._fetch_task_status = AsyncMock(return_value="ready")
    dispatcher._move_and_assign = AsyncMock(return_value=True)
    dispatcher._is_blocked_triage_in_cooldown = AsyncMock(return_value=False)
    return dispatcher, supervisor, running


async def mark(queue, task_id, attempt_id=None, profile="analyst"):
    await queue.set_active(
        profile,
        task_id,
        task_id,
        "in_progress",
        "execute",
        12,
        agent_instance_id=f"agent-{task_id}",
        attempt_id=attempt_id or f"attempt-{task_id}",
    )


async def test_active_siblings_are_addressable_and_ambiguous_profile_has_no_single_task(
    queue,
):
    await mark(queue, "a")
    await mark(queue, "b")
    assert await queue.get_active("analyst") is None
    assert (await queue.get_active("analyst", "b"))["attempt_id"] == "attempt-b"
    assert await queue.is_busy("analyst")
    assert len(await queue.get_all_active()) == 2
    await queue.clear_active("analyst", "a", expected_attempt_id="attempt-a")
    assert (await queue.get_active("analyst"))["task_id"] == "b"


async def test_late_completion_and_unqualified_clear_cannot_erase_new_attempt(queue):
    await mark(queue, "a", "new-attempt")
    await queue.clear_active("analyst")
    await queue.clear_active("analyst", "a")
    await queue.clear_active("analyst", "a", expected_attempt_id="old-attempt")
    assert (await queue.get_active("analyst", "a"))["attempt_id"] == "new-attempt"
    await queue.clear_active("analyst", "a", expected_attempt_id="new-attempt")
    assert not await queue.is_busy("analyst")


async def test_full_sync_and_reconcile_preserve_all_active_attempts(queue):
    await mark(queue, "a")
    await mark(queue, "b")
    tasks = [task("a"), task("b"), task("c")]
    assert await queue.full_sync(tasks) == {"analyst": 1}
    assert await queue.get_queue_task_ids("analyst") == {"c"}
    assert await queue.reconcile(tasks) == {"added": 0, "removed": 0}
    assert {r["task_id"] for r in await queue.get_active_tasks("analyst")} == {"a", "b"}


async def test_same_profile_admits_two_tasks_without_review_reservation(queue):
    dispatcher, supervisor, running = runtime(queue)
    dispatcher._last_board_snapshot = [task("old", status="review")]
    await queue.add_task("analyst", task("a"))
    await queue.add_task("analyst", task("b"))
    assert await dispatcher.dispatch_all_idle() == 2
    assert set(running) == {("analyst", "a"), ("analyst", "b")}
    assert len(await queue.get_active_tasks("analyst")) == 2
    supervisor.is_agent_busy.assert_not_called()
    dispatcher._strict_block_since["analyst"] = 1.0
    assert dispatcher._detect_strict_deadlock() == []


async def test_office_capacity_checked_before_move_under_concurrent_dispatch(queue):
    dispatcher, _, running = runtime(
        queue, profiles=("analyst", "auditor"), max_workers=1
    )
    for name in ("analyst", "auditor"):
        await queue.add_task(name, task(name, name))
    outcomes = await asyncio.gather(
        dispatcher.dispatch_agent("analyst"), dispatcher.dispatch_agent("auditor")
    )
    assert sorted(outcomes) == [False, True]
    assert len(running) == 1
    assert dispatcher._move_and_assign.await_count == 1
    assert sum((await queue.get_all_queue_sizes()).values()) == 1


async def test_per_profile_capacity_defers_without_moving_ready_task(queue):
    dispatcher, _, _ = runtime(queue, per_profile=1)
    for task_id in ("a", "b"):
        await queue.add_task("analyst", task(task_id))
    assert await dispatcher.dispatch_all_idle() == 1
    assert dispatcher._move_and_assign.await_count == 1
    assert await queue.get_queue_size("analyst") == 1


async def test_round_robin_gives_profiles_slots_before_second_sibling(queue):
    dispatcher, supervisor, _ = runtime(
        queue, profiles=("analyst", "auditor"), max_workers=3
    )
    for profile in ("analyst", "auditor"):
        for suffix in ("a", "b"):
            await queue.add_task(profile, task(f"{profile}-{suffix}", profile))
    assert await dispatcher.dispatch_all_idle() == 3
    assert [call.args[0] for call in supervisor.spawn_worker.await_args_list] == [
        "analyst",
        "auditor",
        "analyst",
    ]
    supervisor.spawn_worker.reset_mock()
    dispatcher.dispatch_agent = AsyncMock(return_value=False)
    await dispatcher.dispatch_all_idle()
    assert [call.args[0] for call in dispatcher.dispatch_agent.await_args_list] == [
        "auditor",
        "analyst",
    ]


async def test_deferred_resource_head_does_not_starve_independent_task(queue):
    dispatcher, supervisor, running = runtime(queue)
    supervisor.resources_available.side_effect = (
        lambda _, data: data["task_id"] != "blocked"
    )
    await queue.add_task("analyst", task("blocked", priority="urgent"))
    await queue.add_task(
        "analyst", task("independent", priority="low", execution_resources=[])
    )
    assert await dispatcher.dispatch_agent("analyst")
    assert set(running) == {("analyst", "independent")}
    assert await queue.get_queue_task_ids("analyst") == {"blocked"}
    dispatcher._move_and_assign.assert_awaited_once()


async def test_bounded_scans_advance_past_many_resource_blocked_tasks(queue):
    dispatcher, supervisor, running = runtime(queue)
    supervisor.resources_available.side_effect = (
        lambda _, data: data["task_id"] == "independent"
    )
    for index in range(65):
        await queue.add_task("analyst", task(f"blocked-{index}", priority="critical"))
    await queue.add_task("analyst", task("independent", priority="low"))

    # A large blocked prefix must not monopolize every bounded periodic scan.
    for _ in range(3):
        before = dispatcher._fetch_task_status.await_count
        await dispatcher.dispatch_agent("analyst")
        assert dispatcher._fetch_task_status.await_count - before <= 32
    assert set(running) == {("analyst", "independent")}
    assert await queue.get_queue_size("analyst") == 65


async def test_exhausted_scan_revisits_tasks_after_resources_are_released(queue):
    dispatcher, supervisor, running = runtime(queue)
    supervisor.resources_available.return_value = False
    for index in range(33):
        await queue.add_task("analyst", task(f"blocked-{index}"))
    assert not await dispatcher.dispatch_agent("analyst")
    assert not await dispatcher.dispatch_agent("analyst")
    supervisor.resources_available.return_value = True
    assert await dispatcher.dispatch_agent("analyst")
    assert len(running) == 1


async def test_host_assignment_and_pickup_use_current_connection_secret(queue, monkeypatch):
    import httpx

    dispatcher, supervisor, _ = runtime(queue)
    dispatcher._security_token = "company-token"
    supervisor._office_tool_secret = "first-owner"
    observed = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer company-token"
        observed.append(request.headers["x-office-secret"])
        return httpx.Response(200, json={"ok": True})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert await dispatcher._assign_only("task", "analyst")
    supervisor._office_tool_secret = "replacement-owner"
    assert await TaskDispatcher._move_and_assign(dispatcher, "task", "analyst", "in_progress")
    assert observed == ["first-owner", "replacement-owner"]


async def test_resource_gate_uses_fresh_detail_not_old_queue_override(queue):
    dispatcher, supervisor, _ = runtime(queue)

    async def fetch(task_id):
        dispatcher._fresh_task_details[task_id] = {
            "status": "ready",
            "execution_resources": None,
        }
        return "ready"

    dispatcher._fetch_task_status = fetch
    supervisor.resources_available.side_effect = (
        lambda _, data: data["execution_resources"] == []
    )
    await queue.add_task("analyst", task("a", execution_resources=[]))
    assert not await dispatcher.dispatch_agent("analyst")
    dispatcher._move_and_assign.assert_not_awaited()
    supervisor.spawn_worker.assert_not_awaited()


async def test_duplicate_active_task_is_skipped_while_sibling_can_start(queue):
    dispatcher, _, running = runtime(queue)
    await queue.add_task("analyst", task("a", priority="urgent"))
    assert await dispatcher.dispatch_agent("analyst")
    await queue.add_task("analyst", task("a", priority="urgent"))
    await queue.add_task("analyst", task("b"))
    assert await dispatcher.dispatch_agent("analyst")
    assert len(running) == 2
    assert dispatcher._move_and_assign.await_count == 2


async def test_completion_clears_only_captured_attempt(queue):
    dispatcher, _, _ = runtime(queue)
    await mark(queue, "a", "new-attempt")
    await mark(queue, "b")
    dispatcher.dispatch_agent = AsyncMock(return_value=False)
    await dispatcher.on_agent_complete("analyst", "a", "old-attempt")
    assert len(await queue.get_active_tasks("analyst")) == 2
    await dispatcher.on_agent_complete("analyst", "a", "new-attempt")
    assert (await queue.get_active("analyst"))["task_id"] == "b"


async def test_reconcile_clears_old_attempt_and_preserves_current_sibling_and_journal(
    queue,
):
    dispatcher, supervisor, running = runtime(queue)
    for task_id in ("old", "live", "journal"):
        await mark(queue, task_id)
    running[("analyst", "live")] = SimpleNamespace(execution_attempt_id="attempt-live")
    supervisor.is_task_busy.side_effect = lambda _, task_id: task_id in {
        "live",
        "journal",
    }
    await dispatcher._clear_stale_active_tasks()
    assert {r["task_id"] for r in await queue.get_active_tasks("analyst")} == {
        "live",
        "journal",
    }


@pytest.mark.parametrize("task_is_busy", [True, False])
@pytest.mark.parametrize("enabled", [True, False])
async def test_watchdog_observes_exact_task_liveness_when_sibling_is_busy(
    task_is_busy, enabled
):
    supervisor = MagicMock()
    supervisor.execution_policy = {"enabled": enabled}
    supervisor.is_agent_busy.return_value = True
    supervisor.is_task_busy.return_value = task_is_busy
    dispatcher = MagicMock(add_task=AsyncMock())
    watchdog = TaskWatchdog(
        ws=MagicMock(),
        executor=None,
        manager=MagicMock(),
        config_store=MagicMock(),
        task_queue=None,
        office_id="office",
        supervisor=supervisor,
        dispatcher=dispatcher,
    )
    await watchdog._handle_in_progress({"id": "a", "assigned_agent": "analyst"})
    assert dispatcher.add_task.await_count == (0 if task_is_busy else 1)
    supervisor.is_task_busy.assert_called_once_with("analyst", "a")
    supervisor.is_agent_busy.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
async def test_task_stop_uses_instance_inventory_when_profile_summary_has_siblings(
    queue,
    enabled,
):
    dispatcher, supervisor, running = runtime(queue)
    for task_id in ("a", "b"):
        await queue.add_task("analyst", task(task_id))
        assert await dispatcher.dispatch_agent("analyst")
    supervisor.get_all_statuses.return_value = {"analyst": {"current_task": None}}
    supervisor.execution_policy["enabled"] = enabled

    async def stop(profile, task_id):
        running.pop((profile, task_id))
        return True

    supervisor.stop_task = AsyncMock(side_effect=stop)
    await route_task_kill(
        {"task_id": "a", "all_agents": True, "stop_request_id": "stop"},
        queue_manager=queue,
        dispatcher=dispatcher,
        supervisor=supervisor,
    )
    supervisor.stop_task.assert_awaited_once_with("analyst", "a")
    assert set(running) == {("analyst", "b")}
    assert (await queue.get_active("analyst"))["task_id"] == "b"
    supervisor.execution_policy["enabled"] = True
    assert (await queue.get_active("analyst", "b"))["attempt_id"] == "attempt-b"


@pytest.mark.parametrize("enabled", [True, False])
async def test_task_updated_deduplicates_exact_active_sibling(queue, enabled):
    dispatcher, supervisor, _ = runtime(queue)
    for task_id in ("a", "b"):
        await queue.add_task("analyst", task(task_id))
        assert await dispatcher.dispatch_agent("analyst")
    dispatcher.dispatch_agent = AsyncMock(return_value=False)
    supervisor.execution_policy["enabled"] = enabled
    await route_task_updated(
        {"task_id": "a", "task_data": task("a", status="in_progress")},
        queue_manager=queue,
        dispatcher=dispatcher,
        supervisor=supervisor,
        router=MagicMock(),
    )
    assert await queue.get_queue_size("analyst") == 0
    dispatcher.dispatch_agent.assert_not_awaited()


@pytest.mark.parametrize("capacity, resources", [(False, True), (True, False)])
@pytest.mark.parametrize("enabled", [True, False])
async def test_waiting_for_capacity_or_resources_does_not_consume_crash_budget(
    capacity, resources, enabled
):
    supervisor = MagicMock()
    supervisor.execution_policy = {"enabled": enabled}
    supervisor.is_task_busy.return_value = False
    supervisor.profile_can_spawn.return_value = capacity
    supervisor.resources_available.return_value = resources
    dispatcher = MagicMock(add_task=AsyncMock())
    watchdog = TaskWatchdog(
        ws=MagicMock(),
        executor=None,
        manager=MagicMock(),
        config_store=MagicMock(),
        task_queue=None,
        office_id="office",
        supervisor=supervisor,
        dispatcher=dispatcher,
    )
    await watchdog._handle_in_progress({"id": "a", "assigned_agent": "analyst"})
    assert watchdog._task_crash_count == {}
    dispatcher.add_task.assert_not_awaited()


async def test_fresh_reassignment_cannot_be_overwritten_by_old_queue_owner(queue):
    dispatcher, supervisor, _ = runtime(queue)

    async def fetch(task_id):
        dispatcher._fresh_task_details[task_id] = {
            "status": "ready",
            "assigned_agent": "auditor",
        }
        return "ready"

    dispatcher._fetch_task_status = fetch
    await queue.add_task("analyst", task("a"))
    assert not await dispatcher.dispatch_agent("analyst")
    dispatcher._move_and_assign.assert_not_awaited()
    supervisor.spawn_worker.assert_not_awaited()
