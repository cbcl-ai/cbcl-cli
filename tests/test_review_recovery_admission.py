"""Review backlog recovery must preserve executor reservations and real holds."""

from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio

from src.backend_client import task_has_pending_review_decision
from src.config_sync.sync_service import ConfigStore
from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.agent_supervisor import AgentProcess, AgentState, AgentSupervisor
from src.orchestrator.task_dispatcher import _EXECUTION_BLOCKED, TaskDispatcher

TASK = {
    "id": "task",
    "office_id": "office",
    "workstream_id": "stream",
    "status": "review",
    "assigned_agent": "engineer",
    "reviewer": "editor",
    "execution_cycle": 1,
    "execution_generation": 2,
    "review_retry_epoch": 0,
}


def request(**overrides):
    return {
        "id": "request",
        "office_id": "office",
        "source_task_id": "task",
        "workstream_id": "stream",
        "status": "pending",
        "requesting_agent": "system-sweeper",
        "request_type": "escalate_blocker",
        "requires_user": False,
        "payload": {"sweeper_signals": {"stuck_review": {}}},
        **overrides,
    }


def typed_hold(**identity):
    return request(
        request_type="review_hold",
        requires_user=True,
        payload={
            **{
                key: TASK[key]
                for key in (
                    "reviewer",
                    "execution_cycle",
                    "execution_generation",
                    "review_retry_epoch",
                )
            },
            **identity,
        },
    )


def legacy_hold(**identity):
    payload = typed_hold(**identity)["payload"]
    return request(
        requesting_agent="editor",
        requires_user=True,
        payload={
            "review_recovery": {
                "state": "operator_reconciliation_required",
                "evidence": "communicator_legacy_hold",
                "task_id": "task",
                **payload,
            }
        },
    )


def mock_api(monkeypatch, pages, task=TASK):
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path.endswith("/tasks/task"):
            return httpx.Response(200, json=task)
        page = pages[int(req.url.params.get("offset", 0)) // 100]
        if isinstance(page, int):
            return httpx.Response(page)
        if isinstance(page, Exception):
            raise page
        return httpx.Response(200, json=page)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(handler), **kw),
    )
    return calls


async def lookup():
    return await task_has_pending_review_decision(
        "http://backend", "office", "task", "token", task=TASK
    )


@pytest.mark.parametrize(
    "row,blocked",
    [
        (request(), False),
        (request(payload={"sweeper_signals": {"workstream_stall": {}}}), False),
        (request(request_type="informational", requires_user=True, payload={}), False),
        (request(request_type="board_overview", requires_user=True, payload={}), False),
        (request(requires_user=True), True),
        (request(payload={"rework_cap": True}), True),
        (request(request_type="request_user_action", payload={}), True),
        (request(request_type="request_clarification", payload={}), True),
        (
            request(
                payload={"sweeper_signals": {"stuck_review": {}, "auth_failure": {}}}
            ),
            True,
        ),
        (request(requesting_agent="editor"), True),
        (typed_hold(), True),
        (legacy_hold(), True),
        (typed_hold(execution_cycle=0), False),
        (typed_hold(execution_generation=1), False),
        (typed_hold(review_retry_epoch=1), False),
        (typed_hold(reviewer="old-reviewer"), False),
        (legacy_hold(execution_generation=1), False),
        (typed_hold(execution_generation=None), True),
        (typed_hold(execution_cycle=False), True),
        (
            request(
                payload={
                    "review_recovery": {"state": "operator_reconciliation_required"}
                }
            ),
            True,
        ),
    ],
)
async def test_review_decisions_distinguish_diagnostics_and_current_holds(
    monkeypatch, row, blocked
):
    mock_api(monkeypatch, [{"items": [row], "total": 1}])
    assert await lookup() is blocked


async def test_review_lookup_looks_past_diagnostics_in_one_snapshot(monkeypatch):
    calls = mock_api(
        monkeypatch,
        [
            {
                "items": [request(id=str(i)) for i in range(100)]
                + [request(requires_user=True)],
                "total": 101,
            }
        ],
    )
    assert await lookup() is True
    assert len(calls) == 1
    assert calls[0].url.params["limit"] == "500"
    assert "offset" not in calls[0].url.params
    assert calls[0].headers["Authorization"] == "Bearer token"


@pytest.mark.parametrize(
    "page",
    [
        500,
        {"items": None, "total": 1},
        {"items": [], "total": 1},
        {"items": [request(office_id="other")], "total": 1},
        {"items": [request(source_task_id="other")], "total": 1},
        {"items": [request(status="approved")], "total": 1},
        httpx.ConnectError("unavailable"),
    ],
)
async def test_failed_or_incomplete_review_lookup_is_unknown(monkeypatch, page):
    mock_api(monkeypatch, [page])
    assert await lookup() is None


async def test_bounded_review_scan_does_not_report_no_hold_beyond_cap(monkeypatch):
    calls = mock_api(
        monkeypatch, [{"items": [request() for _ in range(500)], "total": 501}]
    )
    assert await lookup() is None
    assert len(calls) == 1


@pytest_asyncio.fixture
async def dispatcher():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    config = ConfigStore()
    config.agents = [
        {"name": name, "is_active": True}
        for name in ("engineer", "editor", "manager-assistant", "auditor")
    ]
    supervisor = AgentSupervisor(".", "office")
    supervisor.spawn_worker = AsyncMock(return_value=True)
    supervisor.retry_pending_cleanup = AsyncMock()
    queues = AgentQueueManager(redis, "office")
    result = TaskDispatcher(
        redis, "office", supervisor, config, queues, backend_url="http://backend"
    )
    result._move_and_assign = AsyncMock(return_value=True)
    result._refresh_agent_configs = AsyncMock(return_value=True)
    try:
        yield result
    finally:
        await redis.aclose()


@pytest.mark.parametrize(
    "row,expected",
    [
        (request(), "review"),
        (typed_hold(), _EXECUTION_BLOCKED),
        (legacy_hold(), _EXECUTION_BLOCKED),
        (typed_hold(review_retry_epoch=5), "review"),
        (request(requires_user=True), _EXECUTION_BLOCKED),
    ],
)
async def test_fresh_detail_applies_review_gate(dispatcher, monkeypatch, row, expected):
    mock_api(monkeypatch, [{"items": [row], "total": 1}])
    assert await dispatcher._fetch_task_status("task") == expected


async def test_lookup_failure_defers_review_without_spawning_or_starving_queue(
    dispatcher, monkeypatch
):
    mock_api(monkeypatch, [500])
    await dispatcher.add_task(TASK)
    assert not await dispatcher.dispatch_agent("editor")
    dispatcher._supervisor.spawn_worker.assert_not_awaited()
    assert await dispatcher._qm.get_queue_size("editor") == 0
    dispatcher._fetch_board_tasks = AsyncMock(return_value=[TASK])
    await dispatcher._reconcile_once()
    assert await dispatcher._qm.get_queue_task_ids("editor") == {"task"}


@pytest.mark.parametrize("pending", ["working", "cleanup", "completion", "failure"])
async def test_review_preserves_unfinished_process_lifecycle_reservations(
    dispatcher, monkeypatch, pending
):
    process = AgentProcess(
        "engineer", "worker", state=AgentState.IDLE, current_task_id="task"
    )
    if pending == "working":
        process.state = AgentState.WORKING
    elif pending == "cleanup":
        process.cleanup_pending = True
    elif pending == "completion":
        process.pending_completion = {"task_id": "task", "status": "review"}
    else:
        process.pending_failure = {"task_id": "task", "error": "pending"}
    dispatcher._supervisor._agents["engineer"] = process
    dispatcher._last_board_snapshot = [dict(TASK)]
    await dispatcher.add_task(
        {"id": "next", "assigned_agent": "engineer", "status": "ready"}
    )
    assert not await dispatcher.dispatch_agent("engineer")
    dispatcher._supervisor.spawn_worker.assert_not_awaited()
    dispatcher._move_and_assign.assert_not_awaited()
    assert await dispatcher._qm.get_queue_task_ids("engineer") == {"next"}


@pytest.mark.parametrize("method", ["full_sync", "reconcile"])
@pytest.mark.parametrize(
    "reviewer,active,executor,expected",
    [
        ("editor", False, "engineer", "manager-assistant"),
        ("missing", False, "engineer", "manager-assistant"),
        (None, False, "engineer", "manager-assistant"),
        ("editor", False, "manager-assistant", "auditor"),
        ("editor", True, "engineer", "editor"),
    ],
)
async def test_review_projection_repairs_only_unavailable_reviewer(
    dispatcher, method, reviewer, active, executor, expected
):
    dispatcher._config.agents[1]["is_active"] = active
    task = {**TASK, "reviewer": reviewer, "assigned_agent": executor}
    original = dict(task)
    if reviewer:
        await dispatcher._qm.add_task(reviewer, task)
    await getattr(dispatcher._qm, method)(
        [task], reviewer_is_dispatchable=dispatcher._config.is_agent_dispatchable
    )
    queued = await dispatcher._qm.pop_next(expected)
    assert queued is not None and queued["assigned_agent"] == executor
    assert queued["reviewer"] == reviewer  # Persist only in backend's fenced claim.
    assert task == original
    if reviewer and reviewer != expected:
        assert await dispatcher._qm.get_queue_size(reviewer) == 0


async def test_periodic_reconcile_wires_reviewer_eligibility(dispatcher):
    dispatcher._config.agents[1]["is_active"] = False
    dispatcher._fetch_board_tasks = AsyncMock(return_value=[TASK])
    await dispatcher._reconcile_once()
    assert await dispatcher._qm.get_queue_task_ids("manager-assistant") == {"task"}
    assert not await dispatcher._qm.get_queue_task_ids("editor")


async def test_startup_sync_wires_reviewer_eligibility(dispatcher):
    dispatcher._config.agents[1]["is_active"] = False
    dispatcher._fetch_board_tasks = AsyncMock(return_value=[TASK])

    async def finish_startup():
        assert await dispatcher._qm.get_queue_task_ids("manager-assistant") == {"task"}
        assert not await dispatcher._qm.get_queue_task_ids("editor")
        dispatcher._running = False
        return 0

    dispatcher.dispatch_all_idle = finish_startup
    await dispatcher.run()


async def test_diagnostic_only_review_dispatches_after_transient_failure(
    dispatcher, monkeypatch
):
    pages = [500]
    mock_api(monkeypatch, pages)
    await dispatcher.add_task(TASK)
    assert not await dispatcher.dispatch_agent("editor")
    dispatcher._fetch_board_tasks = AsyncMock(return_value=[TASK])
    await dispatcher._reconcile_once()
    pages[0] = {"items": [request()], "total": 1}
    assert await dispatcher.dispatch_agent("editor")
    dispatcher._supervisor.spawn_worker.assert_awaited_once()
    dispatcher._move_and_assign.assert_not_awaited()


async def test_incomplete_diagnostic_snapshot_never_follows_a_shifted_offset(
    monkeypatch,
):
    calls = mock_api(
        monkeypatch,
        [
            {"items": [request(id=str(i)) for i in range(100)], "total": 101},
            # If ten diagnostics resolve, a real decision shifts before offset100.
            # Never consult that page and conclude that no user decision exists.
            {"items": [], "total": 91},
        ],
    )
    assert await lookup() is None
    assert len(calls) == 1


async def test_full_snapshot_is_unknown_even_if_count_matches(monkeypatch):
    mock_api(monkeypatch, [{"items": [request() for _ in range(500)], "total": 500}])
    assert await lookup() is None


@pytest.mark.parametrize("entrypoint", ["add", "reconcile"])
async def test_stale_partial_roster_is_refreshed_before_reviewer_fallback(
    dispatcher, entrypoint
):
    dispatcher._config.agents[1]["is_active"] = False

    async def refresh():
        dispatcher._config.agents[1]["is_active"] = True
        return True

    dispatcher._refresh_agent_configs = AsyncMock(side_effect=refresh)
    if entrypoint == "add":
        await dispatcher.add_task(TASK)
    else:
        dispatcher._fetch_board_tasks = AsyncMock(return_value=[TASK])
        await dispatcher._reconcile_once()
    assert await dispatcher._qm.get_queue_task_ids("editor") == {"task"}
    assert not await dispatcher._qm.get_queue_task_ids("manager-assistant")
    dispatcher._refresh_agent_configs.assert_awaited_once()


async def test_unconfirmed_roster_does_not_guess_fallback_reviewer(dispatcher):
    dispatcher._config.agents[1]["is_active"] = False
    dispatcher._refresh_agent_configs = AsyncMock(return_value=False)
    dispatcher._fetch_board_tasks = AsyncMock(return_value=[TASK])
    await dispatcher._reconcile_once()
    assert await dispatcher._qm.get_queue_task_ids("editor") == {"task"}
    assert not await dispatcher._qm.get_queue_task_ids("manager-assistant")
    assert not await dispatcher.dispatch_all_idle()
    dispatcher._supervisor.spawn_worker.assert_not_awaited()


async def test_active_busy_reviewer_is_neither_refreshed_nor_reassigned(dispatcher):
    dispatcher._refresh_agent_configs = AsyncMock()
    dispatcher._supervisor._agents["editor"] = AgentProcess(
        "editor",
        "worker",
        state=AgentState.WORKING,
        current_task_id="other",
    )
    await dispatcher.add_task(TASK)
    assert not await dispatcher.dispatch_agent("editor")
    assert await dispatcher._qm.get_queue_task_ids("editor") == {"task"}
    dispatcher._refresh_agent_configs.assert_not_awaited()
    dispatcher._supervisor.spawn_worker.assert_not_awaited()


async def test_refused_review_claim_retries_only_after_reconciliation(dispatcher):
    dispatcher._fetch_task_status = AsyncMock(return_value="review")
    dispatcher._supervisor.spawn_worker.return_value = False
    await dispatcher.add_task(TASK)
    assert not await dispatcher.dispatch_agent("editor")
    assert not await dispatcher.dispatch_agent("editor")
    dispatcher._supervisor.spawn_worker.assert_awaited_once()
    assert not await dispatcher._qm.get_queue_task_ids("editor")
    dispatcher._fetch_board_tasks = AsyncMock(return_value=[TASK])
    await dispatcher._reconcile_once()
    assert await dispatcher._qm.get_queue_task_ids("editor") == {"task"}


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        [],
        [{"name": "editor", "is_active": "false"}],
        [{"name": "editor"}, {"name": "editor"}],
    ],
)
async def test_invalid_roster_does_not_replace_prior_config(
    dispatcher, monkeypatch, body
):
    previous = list(dispatcher._config.agents)
    client = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
            **kw,
        ),
    )
    assert not await TaskDispatcher._refresh_agent_configs(dispatcher)
    assert dispatcher._config.agents == previous


async def test_empty_roster_read_preserves_config_and_named_review_queue(
    dispatcher, monkeypatch
):
    dispatcher._config.agents[1]["is_active"] = False
    previous = list(dispatcher._config.agents)
    dispatcher._refresh_agent_configs = TaskDispatcher._refresh_agent_configs.__get__(
        dispatcher
    )
    client = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])),
            **kw,
        ),
    )
    await dispatcher.add_task(TASK)
    assert dispatcher._config.agents == previous
    assert await dispatcher._qm.get_queue_task_ids("editor") == {"task"}
    assert not await dispatcher._qm.get_queue_task_ids("manager-assistant")


async def test_returned_claim_owner_mismatch_does_not_repeat_before_fresh_roster(
    dispatcher, monkeypatch
):
    import json
    from functools import partial

    from src.execution_claim import claim_worker_execution

    claimed = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=TASK)
        body = json.loads(req.content)
        claimed.append(body)
        return httpx.Response(
            200,
            json={
                "attempt_id": body["attempt_id"],
                "execution_cycle": TASK["execution_cycle"],
                "execution_generation": TASK["execution_generation"] + 1,
                "review_retry_epoch": TASK["review_retry_epoch"],
                "agent_name": "editor",
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(handler), **kw),
    )
    launch = AsyncMock()
    monkeypatch.setattr(
        "src.orchestrator.agent_supervisor.asyncio.create_subprocess_exec", launch
    )
    supervisor = dispatcher._supervisor
    supervisor.spawn_worker = AgentSupervisor.spawn_worker.__get__(supervisor)
    supervisor.set_execution_claimer(
        partial(
            claim_worker_execution,
            platform_url="http://backend",
            office_id="office",
            security_token="token",
        )
    )
    dispatcher._config.agents[1]["is_active"] = False
    dispatcher._fetch_task_status = AsyncMock(return_value="review")
    await dispatcher._qm.add_task("manager-assistant", {**TASK, "task_id": TASK["id"]})
    assert not await dispatcher.dispatch_agent("manager-assistant")
    assert not await dispatcher.dispatch_agent("manager-assistant")
    assert len(claimed) == 1
    launch.assert_not_awaited()

    async def refresh():
        dispatcher._config.agents[1]["is_active"] = True
        return True

    dispatcher._refresh_agent_configs = AsyncMock(side_effect=refresh)
    dispatcher._fetch_board_tasks = AsyncMock(return_value=[TASK])
    await dispatcher._reconcile_once()
    assert await dispatcher._qm.get_queue_task_ids("editor") == {"task"}
    assert not await dispatcher._qm.get_queue_task_ids("manager-assistant")
