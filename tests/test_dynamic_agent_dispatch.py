"""Offline admission, queue and task-recovery contracts for dynamic Agents."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from src._handlers._tasks import route_task_kill, route_task_updated
from src.orchestrator import task_dispatcher as td_mod
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
        lambda name, **_options: len(running) < max_workers
        and sum(profile == name for profile, _ in running) < per_profile
    )
    supervisor.resources_available.return_value = True
    # The real check is ``not resource_conflicts``. The dispatcher asks for the
    # conflicts only to log a wait, so the double answers them from whatever
    # availability a test configured, naming one placeholder holder.
    supervisor.resource_conflicts.side_effect = lambda profile, data, **options: (
        ([], [])
        if supervisor.resources_available(profile, data, **options)
        else (["shared-workspace"], ["another task"])
    )
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


async def test_a_full_review_slot_does_not_hold_back_an_execution_task(queue):
    """Under resource admission version 2 an agent's review and execution
    sessions count separately: a review it cannot start yet must not hide
    the execution task behind it in the same queue."""
    dispatcher, supervisor, running = runtime(queue)
    supervisor.profile_can_spawn.side_effect = (
        lambda name, mode=None, **_options: mode != "review"
    )
    statuses = {"waiting-review": "review", "next-task": "ready"}
    dispatcher._fetch_task_status = AsyncMock(
        side_effect=lambda task_id: statuses[task_id]
    )
    await queue.add_task("analyst", task("waiting-review", status="review"))
    await queue.add_task("analyst", task("next-task"))
    assert await dispatcher.dispatch_agent("analyst")
    assert set(running) == {("analyst", "next-task")}
    assert await queue.get_queue_task_ids("analyst") == {"waiting-review"}
    modes = [
        call.kwargs.get("mode")
        for call in supervisor.profile_can_spawn.call_args_list
    ]
    assert "review" in modes and "execute" in modes
    # The full class is known locally: the waiting review cost no task read.
    reads = [call.args[0] for call in dispatcher._fetch_task_status.await_args_list]
    assert reads == ["next-task"]


async def test_a_stale_entry_is_read_and_dropped_while_its_class_is_full(queue):
    """A queue entry the board snapshot contradicts may be stale (a lost
    event): it is not held back by its old session class, so the fresh
    read drops it and the reconciler re-adds it with its real status."""
    dispatcher, supervisor, _ = runtime(queue, profiles=("manager-assistant",))
    supervisor.profile_can_spawn.side_effect = (
        lambda name, mode=None, **_options: mode not in ("review", "triage")
    )
    dispatcher._last_board_snapshot = [task("T", "manager-assistant")]
    await queue.add_task(
        "manager-assistant", task("T", "manager-assistant", status="blocked")
    )
    assert not await dispatcher.dispatch_agent("manager-assistant")
    dispatcher._fetch_task_status.assert_awaited_once_with("T")
    assert await queue.get_queue_task_ids("manager-assistant") == set()


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


# ---------------------------------------------------------------------------
# Wait logs: a requeued task says why it did not start.
# ---------------------------------------------------------------------------

PROFILE = "apd-backend-engineer"
SLOT = "profile:apd-backend-engineer:execute"


def pin_clock(monkeypatch, start: float = 1000.0) -> SimpleNamespace:
    """Pin the dispatcher module's monotonic clock; ``.now`` moves it."""
    clock = SimpleNamespace(now=start)
    monkeypatch.setattr(td_mod, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


async def scan(dispatcher, profile: str) -> bool:
    """One dispatch scan of ``profile``'s queue that pops its waiting task.

    A bounded scan remembers the entries it requeued until a later scan
    finds nothing new and resets, so back-to-back scans alternate. This one
    starts reset, as every other tick of the dispatch loop does.
    """
    dispatcher._dispatch_scan_seen.pop(profile, None)
    return await dispatcher.dispatch_agent(profile)


def wait_lines(caplog) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "cbcl.dispatcher"
        and record.getMessage().endswith("; requeued")
    ]


async def test_a_resource_wait_logs_what_is_held_and_by_whom(
    queue, caplog, monkeypatch
):
    """Production ID-001.T473 waited hours behind T469 and then T477, which
    held its agent's execution slot, and the dispatcher requeued it every
    tick without a word. The wait now logs at INFO once per interval, at
    DEBUG in between, and at INFO at once when another task holds the slot.
    """
    clock = pin_clock(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    dispatcher, supervisor, running = runtime(queue, profiles=(PROFILE,))
    holders = ["ID-001.T469"]
    supervisor.resources_available.return_value = False
    supervisor.resource_conflicts.side_effect = lambda profile, data, **options: (
        [SLOT],
        list(holders),
    )
    await queue.add_task(PROFILE, task("T473", PROFILE, readable_id="ID-001.T473"))

    assert not await scan(dispatcher, PROFILE)
    clock.now += 30.0  # the same wait, within the interval
    assert not await scan(dispatcher, PROFILE)
    holders[:] = ["ID-001.T477"]  # the slot passed to another task
    clock.now += 30.0
    assert not await scan(dispatcher, PROFILE)

    def line(holder: str) -> str:
        return f"Task ID-001.T473 waits for {SLOT} held by {holder}; requeued"

    assert wait_lines(caplog) == [
        (logging.INFO, line("ID-001.T469")),
        (logging.DEBUG, line("ID-001.T469")),
        (logging.INFO, line("ID-001.T477")),
    ]
    # Saying so changes nothing: the task stays queued, unmoved, unstarted.
    assert await queue.get_queue_task_ids(PROFILE) == {"T473"}
    dispatcher._move_and_assign.assert_not_awaited()
    assert running == {}


async def test_a_resource_wait_names_at_most_three_holders(queue, caplog):
    """A run-alone task waits for every holder; the line names three and
    counts the rest, and one holder named twice is named once."""
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    dispatcher, supervisor, _ = runtime(queue, profiles=(PROFILE,))
    supervisor.resources_available.return_value = False
    holders = [
        "ID-001.T1",
        "a running script",
        "a running script",
        "ID-001.T2",
        "ID-001.T3",
    ]
    supervisor.resource_conflicts.side_effect = lambda profile, data, **options: (
        ["shared-workspace"],
        holders,
    )
    await queue.add_task(PROFILE, task("T473", PROFILE, readable_id="ID-001.T473"))
    assert not await scan(dispatcher, PROFILE)
    assert wait_lines(caplog) == [
        (
            logging.INFO,
            "Task ID-001.T473 waits for shared-workspace held by ID-001.T1, "
            "a running script, ID-001.T2 and 1 more; requeued",
        )
    ]


async def test_a_capacity_wait_logs_the_full_session_class(
    queue, caplog, monkeypatch
):
    """A full session class keeps the task queued at either check (before
    the backend read, and after it); both say so, under one throttle key."""
    clock = pin_clock(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    dispatcher, supervisor, _ = runtime(queue, profiles=(PROFILE,))
    # The agent could start some session, but no execution session.
    supervisor.profile_can_spawn.side_effect = (
        lambda name, mode=None, **_options: mode != "execute"
    )
    await queue.add_task(PROFILE, task("T473", PROFILE, readable_id="ID-001.T473"))

    # The early check trusts the queue entry and reads nothing.
    assert not await scan(dispatcher, PROFILE)
    dispatcher._fetch_task_status.assert_not_awaited()
    # A snapshot contradicting the entry sends it to the fresh read first;
    # the check after that read finds the same full class.
    dispatcher._last_board_snapshot = [task("T473", PROFILE, status="backlog")]
    clock.now += 30.0
    assert not await scan(dispatcher, PROFILE)
    dispatcher._fetch_task_status.assert_awaited_once_with("T473")
    clock.now += td_mod.STATE_LOG_INTERVAL_SECONDS
    assert not await scan(dispatcher, PROFILE)

    line = (
        "Task ID-001.T473 waits for a free execute session of "
        "'apd-backend-engineer'; requeued"
    )
    assert wait_lines(caplog) == [
        (logging.INFO, line),
        (logging.DEBUG, line),
        (logging.INFO, line),
    ]
    assert await queue.get_queue_task_ids(PROFILE) == {"T473"}
    dispatcher._move_and_assign.assert_not_awaited()
    supervisor.resource_conflicts.assert_not_called()


async def test_the_resource_wait_names_the_task_holding_the_agents_slot(
    queue, caplog, monkeypatch, tmp_path
):
    """With the supervisor's own rules: an agent's undeclared tasks need its
    one execution slot, so the next task waits although the per-Profile
    limit is 2, and the line names the task holding the slot. Once the slot
    is free the task is admitted as before, without another wait line."""
    from src.orchestrator.agent_supervisor import (
        AgentProcess,
        AgentState,
        AgentSupervisor,
    )

    pin_clock(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="cbcl.dispatcher")
    supervisor = AgentSupervisor(str(tmp_path), "office", container_name="a" * 64)
    supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 4, "max_workers_per_profile": 2}
    )
    supervisor.spawn_worker = AsyncMock(return_value=True)

    def hold_slot(readable_id: str) -> None:
        supervisor._agents.clear()
        supervisor._agents[f"attempt-{readable_id}"] = AgentProcess(
            agent_name=PROFILE,
            role="worker",
            state=AgentState.WORKING,
            current_task_id=f"task-{readable_id}",
            execution_task_id=f"task-{readable_id}",
            current_readable_id=readable_id,
            execution_attempt_id=f"attempt-{readable_id}",
            agent_instance_id=f"instance-{readable_id}",
            execution_mode="execute",
            execution_resources=[SLOT],
        )

    config = MagicMock()
    config.get_agent.side_effect = lambda name: {"name": name}
    dispatcher = TaskDispatcher(queue._redis, "office", supervisor, config, queue)
    dispatcher._fetch_task_status = AsyncMock(return_value="ready")
    dispatcher._move_and_assign = AsyncMock(return_value=True)
    # The backend resolves an undeclared task to its agent's execution slot.
    await queue.add_task(
        PROFILE,
        task(
            "T473",
            PROFILE,
            readable_id="ID-001.T473",
            effective_execution_resources=[SLOT],
        ),
    )

    hold_slot("ID-001.T469")
    assert not await scan(dispatcher, PROFILE)
    hold_slot("ID-001.T477")
    assert not await scan(dispatcher, PROFILE)
    waits = "Task ID-001.T473 waits for " + SLOT + " held by {}; requeued"
    assert wait_lines(caplog) == [
        (logging.INFO, waits.format("ID-001.T469")),
        (logging.INFO, waits.format("ID-001.T477")),
    ]
    dispatcher._move_and_assign.assert_not_awaited()

    supervisor._agents.clear()  # T477 ended and its cleanup finished
    assert await scan(dispatcher, PROFILE)
    dispatcher._move_and_assign.assert_awaited_once()
    supervisor.spawn_worker.assert_awaited_once()
    assert len(wait_lines(caplog)) == 2


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


async def test_an_execution_wait_is_no_crash_while_the_review_class_has_room():
    """Version 2 counts review apart from execution: an orphaned In Progress
    task waits for an execution slot even while the agent could start a
    review, so the watchdog must ask about execution, not about either."""
    supervisor = MagicMock()
    supervisor.execution_policy = {"enabled": True}
    supervisor.is_task_busy.return_value = False
    supervisor.profile_can_spawn.side_effect = (
        lambda name, mode=None, **_options: mode != "execute"
    )
    supervisor.resources_available.return_value = True
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


async def test_rest_detail_keeps_the_directory_task_ready_declared(queue):
    """X46: the REST task detail carries the workstream name but never its
    directory. Before the first sync_config the ConfigStore has no row for
    the workstream, so the task_ready declaration is the only source of the
    directory: the dispatched task keeps it (the supervisor pins the
    task-owned output directory from it at spawn) instead of falling back to
    the legacy ``office`` directory of a non-Latin name."""
    from src.orchestrator.worker_prompt import task_output_dir
    from src.orchestrator.workstream_identity import workstream_directory_for_task

    dispatcher, supervisor, _ = runtime(queue)
    dispatcher._config.get_workstream.return_value = None

    async def fetch(task_id):
        dispatcher._fresh_task_details[task_id] = {
            "status": "ready",
            "workstream_name": "Продажі",
            "workstream_short_code": "PR",
            "agent_execution_policy": {"enabled": True},
        }
        return "ready"

    dispatcher._fetch_task_status = fetch
    await queue.add_task(
        "analyst",
        task(
            "a",
            workstream_id="ws-sales",
            workstream_name="Продажі",
            workstream_context={
                "name": "Продажі",
                "short_code": "PR",
                "workspace_dir": "ws-pr",
            },
        ),
    )
    assert await dispatcher.dispatch_agent("analyst")
    spawned = supervisor.spawn_worker.call_args.args[2]
    assert workstream_directory_for_task(spawned) == "ws-pr"
    assert task_output_dir(spawned) == "/workspace/workstreams/ws-pr/tasks/a"


async def test_an_admission_wait_on_pickup_keeps_the_task_queued(queue):
    """At resource admission version 2 the backend refuses a Ready pickup
    it cannot admit yet with the claim's busy codes: a wait, not a failed
    move. The task stays queued and is not read or moved again until the
    retry pause has passed."""
    dispatcher, _, running = runtime(queue)

    async def refused(task_id, agent_name, new_status):
        dispatcher._move_admission_waits[task_id] = (
            "BadRequestException: analyst is busy with PR-001.T01 (execute)"
        )
        return False

    dispatcher._move_and_assign = AsyncMock(side_effect=refused)
    await queue.add_task("analyst", task("waiting"))
    for _ in range(6):
        assert not await dispatcher.dispatch_agent("analyst")
    assert dispatcher._move_and_assign.await_count == 1
    assert dispatcher._fetch_task_status.await_count == 1
    assert running == {}
    assert await queue.get_queue_task_ids("analyst") == {"waiting"}
    assert dispatcher._move_rollback_failures == {}
    assert dispatcher._move_admission_waits == {}
    dispatcher._admission_wait_until["waiting"] = 0.0  # the pause has passed
    for _ in range(2):
        assert not await dispatcher.dispatch_agent("analyst")
    assert dispatcher._move_and_assign.await_count == 2
    assert await queue.get_queue_task_ids("analyst") == {"waiting"}


@pytest.mark.parametrize(
    "body,wait",
    [
        ({"error": "busy", "code": "execution_resource_busy"}, True),
        ({"error": "busy", "code": "execution_capacity_busy"}, True),
        ({"error": "stale", "code": "stale_execution"}, False),
        ({"error": "refused"}, False),
    ],
)
async def test_the_pickup_move_reports_an_admission_wait(monkeypatch, body, wait):
    import httpx

    client = AsyncMock()
    client.__aenter__.return_value = client
    response = MagicMock(status_code=200)
    response.json.return_value = body
    client.post.return_value = response
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    dispatcher = TaskDispatcher(
        redis=MagicMock(),
        office_id="office",
        supervisor=MagicMock(),
        config_store=MagicMock(),
        queue_manager=MagicMock(),
        backend_url="http://unused",
    )
    assert not await dispatcher._move_and_assign("task", "analyst", "in_progress")
    assert ("task" in dispatcher._move_admission_waits) is wait
    if wait:
        assert dispatcher._move_admission_waits["task"] == "busy"
