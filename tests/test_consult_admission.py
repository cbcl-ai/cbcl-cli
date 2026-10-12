"""Consults run outside board admission (stall incident 2026-09-29).

With the office parallel policy enabled, a Planner verify used to be admitted
like a board task: its synthetic task has no execution resources, so it asked
for ``shared-workspace`` and every unscoped board task refused it, silently.
Consults (Planner, Flow Architect, Data Curator) now take no board slot and
hold no execution resource. They keep single-flight per consult agent, the
process ceiling and the quota / maintenance gates.
"""

import asyncio
import logging
from uuid import uuid4

from src.agent_execution_policy import CONSULT_TASK_ID_PREFIXES, is_consult_task_id
from tests.test_dynamic_agent_supervisor import runtime, task  # noqa: F401


def with_backend_receipt_resources(supervisor):
    """The backend resolves a null declaration to ``shared-workspace``."""
    original = supervisor._execution_claimer

    async def claim(name, task_data, attempt_id):
        receipt = await original(name, task_data, attempt_id)
        declared = task_data.get("execution_resources")
        receipt["execution_resources"] = (
            ["shared-workspace"] if declared is None else list(declared)
        )
        return receipt

    supervisor.set_execution_claimer(claim)


def planner_task(mode="verify"):
    return {
        "task_id": f"planner-{uuid4().hex[:12]}",
        "readable_id": "PLAN",
        "title": f"Planning consult ({mode})",
        "status": "planning",
        "priority": "high",
        "brief": {},
        "workstream_context": {},
        "planner_consult": {"mode": mode, "scope_id": str(uuid4())},
    }


def flow_task():
    return {
        "task_id": f"flow-consult-{uuid4().hex[:12]}",
        "status": "consulting",
        "brief": {},
        "flow_consult": {"request_id": "r", "kind": "flow_design"},
    }


def test_consult_prefixes_are_single_sourced():
    assert CONSULT_TASK_ID_PREFIXES == ("planner-", "flow-consult-")
    assert is_consult_task_id("planner-abc")
    assert is_consult_task_id("flow-consult-abc")
    assert not is_consult_task_id(str(uuid4()))
    assert not is_consult_task_id(None)


async def test_planner_verify_spawns_while_null_resource_board_task_runs(runtime):
    supervisor, _, _, _ = runtime
    with_backend_receipt_resources(supervisor)
    assert await supervisor.spawn_worker("builder", {"name": "builder"}, task())
    assert await supervisor.spawn_worker("planner", {"name": "planner"}, planner_task())


async def test_running_consult_does_not_block_null_resource_board_task(runtime):
    supervisor, _, _, _ = runtime
    with_backend_receipt_resources(supervisor)
    assert await supervisor.spawn_worker("planner", {"name": "planner"}, planner_task())
    assert await supervisor.spawn_worker("builder", {"name": "builder"}, task())
    assert supervisor.resources_available({"name": "builder"}, task()) is False


async def test_consult_ignores_full_board_pool_and_keeps_single_flight(runtime):
    supervisor, _, _, _ = runtime
    for name in ("builder", "analyst", "auditor"):
        assert await supervisor.spawn_worker(
            name, {"name": name}, {**task(), "execution_resources": []}
        )
    assert not supervisor.profile_can_spawn("developer")
    assert await supervisor.spawn_worker("planner", {"name": "planner"}, planner_task())
    second = planner_task("specify")
    assert not await supervisor.spawn_worker("planner", {"name": "planner"}, second)
    refusal = supervisor.spawn_refusal(second["task_id"])
    assert refusal.reason == "agent_busy" and refusal.transient
    assert await supervisor.spawn_worker(
        "flow-architect", {"name": "flow-architect"}, flow_task()
    )
    # The consults took no board slot.
    assert not supervisor.profile_can_spawn("developer")
    assert len([a for a in supervisor._agents.values() if supervisor._is_board_worker(a)]) == 3


async def test_quota_pause_still_refuses_consult_with_a_named_reason(runtime):
    supervisor, state, _, _ = runtime
    state.pause_for_quota("usage limit reached", "opus")
    consult = planner_task()
    assert not await supervisor.spawn_worker("planner", {"name": "planner"}, consult)
    refusal = supervisor.spawn_refusal(consult["task_id"])
    assert refusal.reason == "quota_paused"
    assert refusal.transient


async def test_maintenance_pause_refuses_consult_as_maintenance(runtime):
    supervisor, state, _, _ = runtime
    state.set_maintenance(True)
    consult = planner_task()
    assert not await supervisor.spawn_worker("planner", {"name": "planner"}, consult)
    assert supervisor.spawn_refusal(consult["task_id"]).reason == "maintenance"


async def test_policy_disable_drain_does_not_block_consults(runtime):
    supervisor, _, _, _ = runtime
    with_backend_receipt_resources(supervisor)
    assert await supervisor.spawn_worker("builder", {"name": "builder"}, task())
    # Turning parallel execution off waits for the running board task, and
    # board admission stays closed meanwhile.
    applied = supervisor.set_execution_policy(
        {"enabled": False, "max_workers": 3, "max_workers_per_profile": 2},
        ready=True,
    )
    assert applied is False
    assert not supervisor.config_ready
    board = task()
    assert not await supervisor.spawn_worker("analyst", {"name": "analyst"}, board)
    assert supervisor.spawn_refusal(board["task_id"]).reason == "config_not_ready"
    # Planning and verification still start.
    assert await supervisor.spawn_worker("planner", {"name": "planner"}, planner_task())


async def test_resource_refusal_names_the_holder_and_logs_once(runtime, caplog):
    supervisor, _, _, _ = runtime
    with_backend_receipt_resources(supervisor)
    first = {**task(), "readable_id": "ID-001.T302"}
    assert await supervisor.spawn_worker("builder", {"name": "builder"}, first)
    blocked = task()
    caplog.set_level(logging.WARNING)
    assert not await supervisor.spawn_worker("analyst", {"name": "analyst"}, blocked)
    refusal = supervisor.spawn_refusal(blocked["task_id"])
    assert refusal.reason == "resources"
    assert "shared-workspace" in refusal.detail
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "Cannot spawn analyst" in record.getMessage()
    ]
    assert len(warnings) == 1 and "resources" in warnings[0]


async def test_successful_spawn_clears_an_earlier_refusal(runtime):
    supervisor, state, _, _ = runtime
    consult = planner_task()
    state.set_maintenance(True)
    assert not await supervisor.spawn_worker("planner", {"name": "planner"}, consult)
    state.set_maintenance(False)
    assert await supervisor.spawn_worker("planner", {"name": "planner"}, consult)
    assert supervisor.spawn_refusal(consult["task_id"]) is None


async def test_spawn_locks_are_released_with_their_attempt(runtime):
    """Every consult attempt mints a new task id. The per-task spawn lock of
    a finished attempt, admitted or refused, is removed; the map used to
    keep one lock per id for the life of the daemon."""
    supervisor, _, _, _ = runtime
    assert await supervisor.spawn_worker("planner", {"name": "planner"}, planner_task())
    refused = planner_task("specify")
    assert not await supervisor.spawn_worker("planner", {"name": "planner"}, refused)
    assert await supervisor.spawn_worker("builder", {"name": "builder"}, task())
    assert supervisor._task_locks == {}


async def test_the_spawn_lock_still_serializes_one_task_id(runtime):
    supervisor, _, _, _ = runtime
    order: list[str] = []
    first_in, release = asyncio.Event(), asyncio.Event()

    async def first():
        async with supervisor._task_lock("task-1"):
            order.append("first in")
            first_in.set()
            await release.wait()
            order.append("first out")

    async def second():
        await first_in.wait()
        async with supervisor._task_lock("task-1"):
            order.append("second in")

    attempts = [asyncio.create_task(first()), asyncio.create_task(second())]
    await first_in.wait()
    for _ in range(10):
        if supervisor._task_locks["task-1"].users == 2:
            break
        await asyncio.sleep(0)
    assert supervisor._task_locks["task-1"].users == 2
    assert order == ["first in"]

    release.set()
    await asyncio.gather(*attempts)
    assert order == ["first in", "first out", "second in"]
    assert supervisor._task_locks == {}


async def test_a_cancelled_spawn_attempt_leaves_no_lock_behind(runtime):
    supervisor, _, _, _ = runtime
    holding, waiting = asyncio.Event(), asyncio.Event()

    async def holder():
        async with supervisor._task_lock("task-1"):
            holding.set()
            await asyncio.Event().wait()

    async def waiter():
        waiting.set()
        async with supervisor._task_lock("task-1"):
            raise AssertionError("the holder never releases")

    first = asyncio.create_task(holder())
    await holding.wait()
    second = asyncio.create_task(waiter())
    await waiting.wait()
    await asyncio.sleep(0)
    for attempt in (second, first):
        attempt.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    assert supervisor._task_locks == {}


def _ceiling_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "CUBICLE_MAX_AGENTS" in record.getMessage()
    ]


async def test_a_policy_that_lets_the_board_fill_every_process_is_flagged(
    runtime, caplog,
):
    """Consults share the process ceiling with board work and have no slot
    reserved (a known limit). A policy whose board workers alone can use
    every process is called out once, with the remedy."""
    supervisor, _, _, _ = runtime
    supervisor._max_agents = 6
    caplog.set_level(logging.WARNING)

    fills = {"enabled": True, "max_workers": 3, "max_workers_per_profile": 2}
    supervisor.set_execution_policy(fills, ready=True)
    supervisor.set_execution_policy(fills, ready=True)  # every config sync
    (warning,) = _ceiling_warnings(caplog)
    assert "allows 3 board workers" in warning and "at most 6" in warning
    assert "at least 7" in warning and "max_agents" in warning
    assert "or lower max_workers to 2 or less." in warning

    # A policy that leaves room, and the serial policy, are not flagged.
    caplog.clear()
    supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 2, "max_workers_per_profile": 2}, ready=True,
    )
    supervisor.set_execution_policy(
        {"enabled": False, "max_workers": 32, "max_workers_per_profile": 2}, ready=True,
    )
    assert _ceiling_warnings(caplog) == []

    # A ceiling too small for any board worker: only raising it helps, so
    # the warning does not suggest a max_workers no policy can have.
    supervisor._max_agents = 4
    supervisor.set_execution_policy(
        {"enabled": True, "max_workers": 1, "max_workers_per_profile": 1}, ready=True,
    )
    (warning,) = _ceiling_warnings(caplog)
    assert warning.endswith("Set CUBICLE_MAX_AGENTS to at least 5.")


async def test_a_consult_at_the_process_ceiling_waits_with_a_named_reason(runtime):
    """The known limit, made visible: at the ceiling a consult is refused
    with the transient reason ``max_agents``, so it is queued and reported,
    never dropped."""
    supervisor, _, _, _ = runtime
    assert await supervisor.spawn_worker("builder", {"name": "builder"}, task())
    supervisor._max_agents = supervisor.active_count
    consult = planner_task()
    assert not await supervisor.spawn_worker("planner", {"name": "planner"}, consult)
    refusal = supervisor.spawn_refusal(consult["task_id"])
    assert refusal.reason == "max_agents" and refusal.transient
    assert str(supervisor._max_agents) in refusal.detail



async def test_a_refused_consult_is_not_a_supervisor_warning(runtime, caplog):
    """The consult queue retries a waiting consult on every agent exit, each
    time under a new task id, and logs the wait itself: one WARNING when it
    is new or its reason changed. The supervisor's own line for a consult
    is DEBUG, or a consult waiting at the process ceiling would log a
    WARNING on every agent exit. A board refusal stays a WARNING."""
    supervisor, _, _, _ = runtime
    assert await supervisor.spawn_worker("builder", {"name": "builder"}, task())
    supervisor._max_agents = supervisor.active_count
    caplog.set_level(logging.DEBUG, logger="cbcl.supervisor")

    for _ in range(3):
        consult = planner_task()
        assert not await supervisor.spawn_worker(
            "planner", {"name": "planner"}, consult
        )
        assert supervisor.spawn_refusal(consult["task_id"]).reason == "max_agents"
    board = {**task(), "execution_resources": []}
    assert not await supervisor.spawn_worker("analyst", {"name": "analyst"}, board)

    def levels(agent: str) -> list[str]:
        return [
            record.levelname
            for record in caplog.records
            if f"Cannot spawn {agent}" in record.getMessage()
        ]

    assert levels("planner") == ["DEBUG"] * 3
    assert levels("analyst") == ["WARNING"]
