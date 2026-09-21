"""Reviewer completion requires an explicit, persisted verdict.

Clean but verdictless sessions hold Review with one typed escalation.
Infrastructure failures have a durable, per-cycle unique-attempt budget.
Neither a clean process exit nor retry exhaustion approves or returns work.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers import get_max_rework_cycles


@pytest.fixture(autouse=True)
def isolated_runtime_state(tmp_path, monkeypatch):
    monkeypatch.setattr("src.paths.get_runtime_state_path", lambda: tmp_path / "runtime.sqlite3")
    monkeypatch.setattr("src.runtime_state._generation_controls", {})


# ---------------------------------------------------------------------------
# Harness: build init_office_process_model with everything mocked and
# capture the _on_agent_event callback handed to AgentSupervisor.
# ---------------------------------------------------------------------------


class Harness:
    def __init__(self) -> None:
        self.on_event = None
        self.queue_manager = MagicMock()
        self.queue_manager.add_task = AsyncMock()
        self.queue_manager.clear_active = AsyncMock()
        # LOW-5: the fatal branch reads the active marker before
        # clearing. Default None = "no marker" → clear proceeds.
        self.queue_manager.get_active = AsyncMock(return_value=None)
        self.dispatcher = MagicMock()
        self.dispatcher.on_agent_complete = AsyncMock()
        self.dispatcher.dispatch_agent = AsyncMock()
        self.router = MagicMock()
        self.router.publish_event = AsyncMock()
        self.mgr = MagicMock()
        self.mgr.ingest_planner_result = AsyncMock()
        self.config_store = MagicMock()
        self.config_store.max_rework_cycles = None
        self.config_store.is_agent_dispatchable.return_value = True
        self.supervisor = None  # set by build_harness


async def build_harness(*, office_id="office-1", platform_url="http://test-backend:1", security_token="") -> Harness:
    """Run init_office_process_model with heavy deps patched; return the
    captured ``_on_agent_event`` closure plus the component mocks it uses."""
    from src.handlers import init_office_process_model

    h = Harness()

    office = MagicMock()
    office.id = office_id
    office.workspace_path = "/tmp/test-cb-workspace"
    office.extra_mounts = []

    mock_supervisor = MagicMock()
    # LOW-8: the capped re-queue helper's background dispatch waits for
    # the agent to go idle; a bare MagicMock return is truthy and would
    # spin the bounded wait for its full budget in every test.
    mock_supervisor.is_agent_busy.return_value = False
    h.supervisor = mock_supervisor
    mock_sm = MagicMock()
    mock_sm.init_from_disk = AsyncMock()
    mock_sr = MagicMock()
    mock_sr.cleanup_orphaned_run_files.return_value = 0
    mock_sr.scan_outbox = AsyncMock(return_value=0)
    mock_sr.has_active_scripts.return_value = False
    mock_sr.reconcile_handoffs = AsyncMock()

    config_store_cls = MagicMock(return_value=h.config_store)
    startup_client = AsyncMock()
    startup_client.__aenter__.return_value = startup_client
    startup_client.get.return_value = MagicMock(status_code=503)
    redis_client = AsyncMock()
    pipeline = MagicMock()
    pipeline.__aenter__.return_value = pipeline
    pipeline.execute = AsyncMock()
    redis_client.pipeline = MagicMock(return_value=pipeline)

    patchers = (
        patch("src.fs_handler.FsHandler"),
        patch("httpx.AsyncClient", return_value=startup_client),
        patch("src.recovery.reap_orphan_agent_sessions", new_callable=AsyncMock),
        patch("src.handlers.WorkspaceSetup"),
        patch("src.handlers.ConfigStore", config_store_cls),
        patch("src.handlers.ScriptSyncer"),
        patch("src.handlers.ClaudeMdWriter"),
        patch("src.handlers.SessionManager", return_value=mock_sm),
        patch("src.handlers.VariableManager"),
        patch("src.handlers.SecretsStore"),
        patch("src.handlers.ScriptRunner", return_value=mock_sr),
        patch("src.handlers.AgentQueueManager", return_value=h.queue_manager),
        patch(
            "src.handlers.reconcile_orphaned_script_executions",
            new_callable=AsyncMock, return_value=0,
        ),
        patch("src.handlers._run_history_backfill", new_callable=MagicMock,
              side_effect=lambda *a, **kw: _noop()),
        patch("src.handlers._spawn_background", side_effect=_close_bootstrap_coroutine),
        patch("src.connection.ws_client.PlatformWSClient"),
        patch(
            "src.orchestrator.agent_supervisor.AgentSupervisor",
            return_value=mock_supervisor,
        ),
        patch(
            "src.orchestrator.task_dispatcher.TaskDispatcher",
            return_value=h.dispatcher,
        ),
        patch(
            "src.transport.ws_transport.WsTransport",
            return_value=h.router,
        ),
        patch("src.handlers.ManagerController", return_value=h.mgr),
        patch("src.handlers.HealthReporter"),
        patch("src.watchdog.TaskWatchdog"),
    )
    with ExitStack() as stack:
        for patcher in patchers:
            patched = stack.enter_context(patcher)
            if patcher.attribute == "AgentSupervisor":
                sup_cls = patched
        await init_office_process_model(
            office, platform_url,
            container_name="cbcl-office-test",
            redis_client=redis_client,
            security_token=security_token,
        )
        h.on_event = sup_cls.call_args.kwargs["on_event"]

    return h


async def _noop() -> None:
    return None


def _close_bootstrap_coroutine(coroutine, **_kwargs) -> None:
    """Exclude startup I/O only; event callbacks retain real background tasks."""
    coroutine.close()


def _httpx_mock(task_info: dict, post_result: dict | None = None):
    """Return (client, AsyncClient-classmock) where ``client.get`` returns
    ``task_info`` and ``client.post`` returns ``post_result``."""
    client = MagicMock()
    get_resp = MagicMock(status_code=200)
    get_resp.json.return_value = {"execution_cycle": 1, "execution_generation": 1, "review_retry_epoch": 0, "assigned_agent": "executor", **task_info}
    client.get = AsyncMock(return_value=get_resp)
    post_resp = MagicMock(status_code=200)
    post_resp.json.return_value = post_result if post_result is not None else {"action_request_id": "request-1", "status": "pending", "review_retry_epoch": 0}
    post_resp.text = ""
    client.post = AsyncMock(return_value=post_resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    cls = MagicMock(return_value=cm)
    return client, cls


def _move_calls(client, new_status: str) -> list:
    return [
        c for c in client.post.call_args_list
        if c.kwargs.get("json", {}).get("action") == "move_task"
        and c.kwargs["json"]["params"].get("new_status") == new_status
    ]


REVIEWER_EVENT = {
    "type": "task_complete",
    "task_id": "task-1",
    "status": "review",
    "is_review_completion": True,
    "comment": "Review complete.",
    "_caller": {"attempt_id": str(uuid.uuid4()), "execution_cycle": 1, "execution_generation": 1, "review_retry_epoch": 0},
}


def _new_attempt(event, *, cycle=1):
    return {**event, "_caller": {"attempt_id": str(uuid.uuid4()), "execution_cycle": cycle, "execution_generation": 1, "review_retry_epoch": 0}}


# ---------------------------------------------------------------------------
# (a) cap + pending action request → NOT auto-approved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_skips_approve_when_pending_action_request():
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 2,
    })

    with (
        patch("httpx.AsyncClient", cls),
        patch(
            "src.backend_client.task_has_pending_action_request",
            new_callable=AsyncMock, return_value=True,
        ) as pending,
    ):
        await h.on_event("editor", dict(REVIEWER_EVENT))

    pending.assert_not_awaited()
    # No move at all — neither done nor ready.
    assert _move_calls(client, "done") == []
    assert _move_calls(client, "ready") == []
    # And the review was NOT re-queued (it stays parked on the human).
    h.queue_manager.add_task.assert_not_awaited()


# ---------------------------------------------------------------------------
# (b) heartbeat-killed reviewer below cap → re-queued via the fatal-error
#     path; no review→ready move (pins the EXISTING error-branch behavior)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fatal_error_reviewer_requeues_without_consuming_rework():
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })

    with patch("httpx.AsyncClient", cls):
        await h.on_event("editor", {
            "type": "error",
            "task_id": "task-1",
            "fatal": True,
            "message": "heartbeat timeout — killed",
            "_caller": REVIEWER_EVENT["_caller"],
        })

    # Re-queued to the reviewer, urgently.
    h.queue_manager.add_task.assert_awaited_once()
    agent, payload = h.queue_manager.add_task.call_args[0]
    assert agent == "editor"
    assert payload["status"] == "review"
    assert payload["priority"] == "urgent"
    # NO board move was issued — rework_count untouched.
    assert client.post.call_args_list == []


# ---------------------------------------------------------------------------
# (b') infra-classed reviewer task_complete below cap → re-queued, no
#      review→ready move (the new T1.1.3 guard in the ambiguous branch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infra_classed_completion_requeues_without_rework_move():
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 1,
    })

    event = dict(REVIEWER_EVENT)
    event["details"] = {"error_class": "rate_limited"}

    with patch("httpx.AsyncClient", cls):
        await h.on_event("editor", event)

    h.queue_manager.add_task.assert_awaited_once()
    agent, payload = h.queue_manager.add_task.call_args[0]
    assert agent == "editor"
    assert payload["status"] == "review"
    # No review→ready (would consume a rework cycle) and no done.
    assert _move_calls(client, "ready") == []
    assert _move_calls(client, "done") == []


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_ambiguous_completion_below_cap_holds_without_rework():
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })

    with patch("httpx.AsyncClient", cls):
        await h.on_event("editor", dict(REVIEWER_EVENT))

    moves = _move_calls(client, "ready")
    assert moves == []
    assert _move_calls(client, "done") == []
    assert client.post.call_args.args[0].endswith("/review-hold")
    assert client.post.call_args.kwargs["json"]["reason"] == "missing_verdict"
    h.queue_manager.add_task.assert_not_awaited()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_rework_never_approves_without_verdict():
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 2,
    })

    with (
        patch("httpx.AsyncClient", cls),
        patch(
            "src.backend_client.task_has_pending_action_request",
            new_callable=AsyncMock, return_value=False,
        ),
    ):
        await h.on_event("editor", dict(REVIEWER_EVENT))

    assert _move_calls(client, "done") == []
    activity_events = [
        c.args[0] for c in h.router.publish_event.call_args_list
        if c.args[0].get("type") == "task_activity"
        and c.args[0].get("event_type") == "review_approved"
    ]
    assert activity_events == []
    assert client.post.call_args.args[0].endswith("/review-hold")
    assert client.post.call_args.kwargs["json"]["reason"] == "missing_verdict"


# ---------------------------------------------------------------------------
# MA-review branch — mirrors (a)/(d) above (Phase-1 review issue 1)
# ---------------------------------------------------------------------------


MA_REVIEWER_EVENT = {
    "type": "task_complete",
    "task_id": "task-1",
    "status": "review",
    "is_review_completion": True,
    "comment": "Review complete.",
    "_caller": {"attempt_id": str(uuid.uuid4()), "execution_cycle": 1, "execution_generation": 1, "review_retry_epoch": 0},
}


@pytest.mark.asyncio
async def test_ma_auto_approve_skipped_when_pending_action_request():
    """(a-mirror) MA ambiguous completion + pending AR → NOT auto-approved;
    the task stays in review, parked on the human."""
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "manager-assistant",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })

    with (
        patch("httpx.AsyncClient", cls),
        patch(
            "src.backend_client.task_has_pending_action_request",
            new_callable=AsyncMock, return_value=True,
        ) as pending,
    ):
        await h.on_event("manager-assistant", dict(MA_REVIEWER_EVENT))

    pending.assert_not_awaited()
    assert _move_calls(client, "done") == []
    assert _move_calls(client, "ready") == []
    # Not re-queued either — it stays parked on the human.
    h.queue_manager.add_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_ma_verdictless_completion_holds_without_approval_activity():
    """The Manager Assistant uses the same explicit-verdict contract."""
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "manager-assistant",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })

    with (
        patch("httpx.AsyncClient", cls),
        patch(
            "src.backend_client.task_has_pending_action_request",
            new_callable=AsyncMock, return_value=False,
        ),
    ):
        await h.on_event("manager-assistant", dict(MA_REVIEWER_EVENT))

    assert _move_calls(client, "done") == []
    activity_events = [
        c.args[0] for c in h.router.publish_event.call_args_list
        if c.args[0].get("type") == "task_activity"
        and c.args[0].get("event_type") == "review_approved"
    ]
    assert activity_events == []
    assert client.post.call_args.args[0].endswith("/review-hold")
    assert client.post.call_args.kwargs["json"]["reason"] == "missing_verdict"


@pytest.mark.asyncio
async def test_ma_infra_classed_completion_requeues_instead_of_approving():
    """(b'-mirror) An infra-classed MA review completion (e.g. retry-exhausted
    reviewer session) is re-queued urgently — never auto-approved."""
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "manager-assistant",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })

    event = dict(MA_REVIEWER_EVENT)
    event["details"] = {"error_class": "rate_limited"}

    with patch("httpx.AsyncClient", cls):
        await h.on_event("manager-assistant", event)

    h.queue_manager.add_task.assert_awaited_once()
    agent, payload = h.queue_manager.add_task.call_args[0]
    assert agent == "manager-assistant"
    assert payload["status"] == "review"
    assert payload["priority"] == "urgent"
    assert _move_calls(client, "done") == []


# ---------------------------------------------------------------------------
# Issue 2 — retry-exhausted REVIEWER session lands in the urgent review
# re-queue path (no invalid review→blocked move, no rework consumed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_exhausted_reviewer_requeues_review_end_to_end():
    """A reviewer session whose CLI retries are exhausted emits a
    review-stamped escalation event (worker side), and the orchestrator
    routes it through the reviewer branch's infra-guard: review re-queued
    urgently, NO move_task issued (rework_count untouched, no invalid
    review→blocked move)."""
    from src.agent_worker import AgentErrorEscalation, AgentWorker

    w = AgentWorker(
        role="worker", agent_name="editor",
        workspace_path="/tmp/test-cb-workspace", office_id="office-1",
    )
    sent: list[dict] = []
    w._send = lambda m: sent.append(m)
    w._run_sdk_session = AsyncMock(side_effect=AgentErrorEscalation(
        error_class="rate_limited",
        original_error="429 too many requests",
        escalation_message="Retries exhausted",
    ))

    await w._handle_assign_task({
        "type": "assign_task",
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "review",  # REVIEWER dispatch
        "agent_config": {"name": "editor"},
    })

    completes = [m for m in sent if m["type"] == "task_complete"]
    assert len(completes) == 1
    evt = completes[0]
    # The escalation is stamped as a review completion that stays in review.
    assert evt["status"] == "review"
    assert evt["is_review_completion"] is True
    assert evt["details"]["error_class"] == "rate_limited"

    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 1,
    })
    with patch("httpx.AsyncClient", cls):
        await h.on_event("editor", _new_attempt(evt))

    # Re-queued to the reviewer, urgently.
    h.queue_manager.add_task.assert_awaited_once()
    agent, payload = h.queue_manager.add_task.call_args[0]
    assert agent == "editor"
    assert payload["status"] == "review"
    assert payload["priority"] == "urgent"
    # NO board move at all — no review→blocked, no rework-consuming return.
    moves = [
        c for c in client.post.call_args_list
        if c.kwargs.get("json", {}).get("action") == "move_task"
    ]
    assert moves == []


@pytest.mark.asyncio
async def test_retry_exhausted_executor_still_escalates_to_blocked():
    """Regression guard for the issue-2 fix: an EXECUTOR session (non-review
    dispatch) that exhausts retries keeps the existing blocked escalation."""
    from src.agent_worker import AgentErrorEscalation, AgentWorker

    w = AgentWorker(
        role="worker", agent_name="dev",
        workspace_path="/tmp/test-cb-workspace", office_id="office-1",
    )
    sent: list[dict] = []
    w._send = lambda m: sent.append(m)
    w._run_sdk_session = AsyncMock(side_effect=AgentErrorEscalation(
        error_class="auth_failed",
        original_error="401",
        escalation_message="Credentials rejected",
    ))

    await w._handle_assign_task({
        "type": "assign_task",
        "task_id": "task-2",
        "readable_id": "WR-001.T02",
        "status": "ready",  # EXECUTOR dispatch
        "agent_config": {"name": "dev"},
    })

    completes = [m for m in sent if m["type"] == "task_complete"]
    assert len(completes) == 1
    evt = completes[0]
    assert evt["status"] == "blocked"
    assert evt.get("is_review_completion", False) is False
    assert evt["details"]["error_class"] == "auth_failed"
    assert evt["comment"].startswith("ESCALATED (auth_failed)")


# ---------------------------------------------------------------------------
# HIGH-1 — approve guard fails CLOSED when the pending-AR lookup fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_circuit_breaker_fails_closed_on_lookup_failure():
    """A failed pending-AR lookup (helper returns None) must NOT be read
    as "no pending" — the approve is skipped and the task stays in
    review (a force-done could bury a live escalation)."""
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 2,
    })

    with (
        patch("httpx.AsyncClient", cls),
        patch(
            "src.backend_client.task_has_pending_action_request",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        await h.on_event("editor", dict(REVIEWER_EVENT))

    assert _move_calls(client, "done") == []
    assert _move_calls(client, "ready") == []
    h.queue_manager.add_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_ma_auto_approve_fails_closed_on_lookup_failure():
    """MA mirror of the fail-closed guard."""
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "manager-assistant",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })

    with (
        patch("httpx.AsyncClient", cls),
        patch(
            "src.backend_client.task_has_pending_action_request",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        await h.on_event("manager-assistant", dict(MA_REVIEWER_EVENT))

    assert _move_calls(client, "done") == []
    assert _move_calls(client, "ready") == []
    h.queue_manager.add_task.assert_not_awaited()


class TestPendingActionRequestTriState:
    """The helper itself: True / False / None (lookup failed)."""

    class _Client:
        def __init__(self, status_code=200, body=None, raise_exc=None):
            self._sc = status_code
            self._body = body
            self._raise = raise_exc

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            if self._raise:
                raise self._raise
            resp = MagicMock(status_code=self._sc)
            resp.json.return_value = self._body
            return resp

    @pytest.mark.asyncio
    async def test_transport_error_returns_none(self):
        from src.backend_client import task_has_pending_action_request

        with patch(
            "httpx.AsyncClient",
            return_value=self._Client(raise_exc=OSError("conn refused")),
        ):
            result = await task_has_pending_action_request(
                "http://x", "oid", "tid", None,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        from src.backend_client import task_has_pending_action_request

        with patch(
            "httpx.AsyncClient", return_value=self._Client(status_code=503),
        ):
            result = await task_has_pending_action_request(
                "http://x", "oid", "tid", None,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_pending_and_no_pending_unchanged(self):
        from src.backend_client import task_has_pending_action_request

        with patch(
            "httpx.AsyncClient",
            return_value=self._Client(body={"total": 1}),
        ):
            assert await task_has_pending_action_request(
                "http://x", "oid", "tid", None,
            ) is True
        with patch(
            "httpx.AsyncClient",
            return_value=self._Client(body={"total": 0}),
        ):
            assert await task_has_pending_action_request(
                "http://x", "oid", "tid", None,
            ) is False

    @pytest.mark.asyncio
    async def test_ma_routing_skip_stays_fail_open(self, monkeypatch):
        """The original MA-routing caller keeps fail-open semantics:
        a failed lookup (None) falls through to the cooldown check
        instead of reporting "skip"."""
        import src.backend_client as bc

        async def _none(**kw):
            return None

        async def _no_cooldown(**kw):
            return False

        monkeypatch.setattr(bc, "task_has_pending_triage_decision", _none)
        monkeypatch.setattr(
            bc, "task_blocked_triage_within_cooldown", _no_cooldown,
        )
        assert await bc.task_should_skip_ma_routing(
            "http://x", "oid", "tid", None,
        ) is False


# ---------------------------------------------------------------------------
# HIGH-2 — infra-failure review re-queues are capped per task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infra_requeue_cap_holds_after_two_unique_retries():
    """Two unique infra failures retry; the third creates a durable hold."""
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })
    event = dict(REVIEWER_EVENT)
    event["details"] = {"error_class": "auth_failed"}

    with patch("httpx.AsyncClient", cls):
        for _ in range(2):
            await h.on_event("editor", _new_attempt(event))
        assert h.queue_manager.add_task.await_count == 2
        await h.on_event("editor", _new_attempt(event))
        await h.on_event("editor", _new_attempt(event))

    assert h.queue_manager.add_task.await_count == 2
    escalations = [call for call in client.post.call_args_list if call.args[0].endswith("/review-hold")]
    assert len(escalations) == 1
    # The task was NOT moved anywhere.
    moves = [
        c for c in client.post.call_args_list
        if c.kwargs.get("json", {}).get("action") == "move_task"
    ]
    assert moves == []


@pytest.mark.asyncio
async def test_infra_review_hold_resets_only_after_new_execution_cycle():
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })
    infra = dict(REVIEWER_EVENT)
    infra["details"] = {"error_class": "rate_limited"}

    with patch("httpx.AsyncClient", cls):
        for _ in range(2):
            await h.on_event("editor", _new_attempt(infra))
        assert h.queue_manager.add_task.await_count == 2

        await h.on_event("editor", dict(REVIEWER_EVENT))
        assert _move_calls(client, "ready") == []
        await h.on_event("editor", _new_attempt(infra))
        assert h.queue_manager.add_task.await_count == 2

        client.get.return_value.json.return_value["execution_cycle"] = 2
        await h.on_event("editor", _new_attempt(infra, cycle=2))

    assert h.queue_manager.add_task.await_count == 3


@pytest.mark.asyncio
async def test_crashed_reviewer_requeue_shares_the_cap():
    """The fatal crashed-reviewer re-queue rides the SAME per-task
    counter as the infra-completion sites — a deterministically
    crashing reviewer stops re-spawning after the cap."""
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })
    fatal = {
        "type": "error",
        "task_id": "task-1",
        "fatal": True,
        "message": "heartbeat timeout — killed",
    }

    with patch("httpx.AsyncClient", cls):
        for _ in range(2):
            await h.on_event("editor", _new_attempt(fatal))
        assert h.queue_manager.add_task.await_count == 2
        await h.on_event("editor", _new_attempt(fatal))

    assert h.queue_manager.add_task.await_count == 2


@pytest.mark.asyncio
async def test_capped_crashed_reviewer_does_not_requeue_or_approve(caplog):
    """A held failure must not report a requeue or approved review."""
    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })
    fatal = {
        "type": "error",
        "task_id": "task-1",
        "fatal": True,
        "message": "heartbeat timeout — killed",
    }

    with patch("httpx.AsyncClient", cls):
        with caplog.at_level("INFO", logger="cbcl.handlers"):
            for _ in range(2):
                await h.on_event("editor", _new_attempt(fatal))
            caplog.clear()

            await h.on_event("editor", _new_attempt(fatal))

    assert not any(
        "re-queued to reviewer" in r.message for r in caplog.records
    )
    assert h.queue_manager.add_task.await_count == 2
    assert _move_calls(client, "done") == []


# ---------------------------------------------------------------------------
# MEDIUM-3 — CancelledError in REVIEW mode stays in review (no move)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_reviewer_session_requeues_review_end_to_end():
    """An externally cancelled REVIEWER emits a review-stamped completion;
    the orchestrator's infra-guard re-queues the review — NO move_task(review→blocked),
    no blocked-bounce consumed, no MA triage noise."""
    from src.agent_worker import AgentWorker

    w = AgentWorker(
        role="worker", agent_name="editor",
        workspace_path="/tmp/test-cb-workspace", office_id="office-1",
    )
    sent: list[dict] = []
    w._send = lambda m: sent.append(m)
    w._run_sdk_session = AsyncMock(side_effect=asyncio.CancelledError())

    await w._handle_assign_task({
        "type": "assign_task",
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "review",  # REVIEWER dispatch
        "agent_config": {"name": "editor"},
    })

    completes = [m for m in sent if m["type"] == "task_complete"]
    assert len(completes) == 1
    evt = completes[0]
    assert evt["status"] == "review"
    assert evt["is_review_completion"] is True
    assert evt["details"]["error_class"] == "cancelled"

    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })
    with patch("httpx.AsyncClient", cls):
        await h.on_event("editor", _new_attempt(evt))

    h.queue_manager.add_task.assert_awaited_once()
    agent, payload = h.queue_manager.add_task.call_args[0]
    assert agent == "editor"
    assert payload["status"] == "review"
    assert payload["priority"] == "urgent"
    # NO board move at all — no review→blocked, no rework consumed.
    moves = [
        c for c in client.post.call_args_list
        if c.kwargs.get("json", {}).get("action") == "move_task"
    ]
    assert moves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["explicit_cancel", "external_cancel"])
async def test_cancelled_executor_session_still_goes_to_blocked(source):
    """Regression guard: EXECUTOR-mode cancels keep the existing
    blocked completion (with the planner_consult passthrough)."""
    from src.agent_worker import AgentWorker

    w = AgentWorker(
        role="worker", agent_name="dev",
        workspace_path="/tmp/test-cb-workspace", office_id="office-1",
    )
    sent: list[dict] = []
    w._send = lambda m: sent.append(m)

    async def cancel_session(**kwargs):
        w._cancellation_source = source
        raise asyncio.CancelledError

    w._run_sdk_session = cancel_session

    await w._handle_assign_task({
        "type": "assign_task",
        "task_id": "task-2",
        "readable_id": "WR-001.T02",
        "status": "ready",  # EXECUTOR dispatch
        "agent_config": {"name": "dev"},
    })

    completes = [m for m in sent if m["type"] == "task_complete"]
    assert len(completes) == 1
    evt = completes[0]
    assert evt["status"] == "blocked"
    assert evt.get("is_review_completion", False) is False

    h = await build_harness()
    client, cls = _httpx_mock(
        {"id": "task-2", "status": "in_progress", "assigned_agent": "dev"},
        {"old_status": "in_progress", "new_status": "blocked"},
    )
    with patch("httpx.AsyncClient", cls):
        await h.on_event("dev", _new_attempt(evt))

    assert len(_move_calls(client, "blocked")) == 1
    h.dispatcher.on_agent_complete.assert_awaited_once_with("dev")


# ---------------------------------------------------------------------------
# Pivot-2 P1 — post-terminal cancel completes CLEAN (no error row)
#
# Incident 2026-07-28: the auditor delivered its PASS verdict
# (move_task → done at 13:13:57.584); teardown cancelled the
# still-draining CLI at .974 → a user-visible ERROR activity on a
# successfully completed task. Same shape for the executor whose row
# landed after update_status → review. Contract:
#   * the stream loop flags a NON-error tool_result of the session's
#     terminal board action (update_status → review/blocked, verdict
#     move_task) — an ERRORED result never sets the flag;
#   * a cancel AFTER the flag suppresses the error activity and emits a
#     clean TASK_COMPLETE (marker ``details.post_terminal_cancel``, NO
#     error_class) that is a no-op orchestrator-side;
#   * pre-terminal cancels are byte-identical (the two tests above).
# ---------------------------------------------------------------------------


_STREAM_AGENT_CONFIG = {"_container_name": "cbcl-office-test",
                        "model": "claude-opus-4-7"}


def _stream_worker() -> MagicMock:
    worker = MagicMock()
    worker.backend_url = ""  # skip the get_task_detail fetch
    worker.office_id = "office-1"
    worker.agent_name = "builder"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


def _patch_stream(monkeypatch, seq) -> None:
    from src.docker import session_bridge

    def factory(**kwargs):
        async def agen():
            for m in seq:
                yield m

        return agen()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)


def _terminal_use(name: str = "mcp__cubicle-tools__update_status",
                  block_id: str = "term-1", **input_kw):
    from src.docker.session_bridge import SessionMessage
    return SessionMessage(type="assistant", data={"message": {"content": [
        {"type": "tool_use", "name": name, "id": block_id,
         "input": dict(input_kw)},
    ]}})


def _terminal_result(block_id: str = "term-1", *, is_error: bool = False,
                     content: str = "Session complete."):
    from src.docker.session_bridge import SessionMessage
    return SessionMessage(type="user", data={"message": {"content": [
        {"type": "tool_result", "tool_use_id": block_id,
         "is_error": is_error, "content": content},
    ]}})


def _cli_result(sid: str = "sess-1"):
    from src.docker.session_bridge import SessionMessage
    return SessionMessage(
        type="result", data={"session_id": sid, "cost_usd": 0.01},
    )


def _stream_task_data(status: str = "ready") -> dict:
    return {
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": status,
        "brief": {"goal": "Ship the thing"},
        "agent_config": {},
    }


@pytest.mark.asyncio
async def test_stream_flags_successful_terminal_update_status(monkeypatch):
    """A NON-error tool_result for update_status→review stamps the
    post-terminal flag (tool + status + target)."""
    from src._agent_worker_task import run_sdk_session

    worker = _stream_worker()
    _patch_stream(monkeypatch, [
        _terminal_use(task_id="task-1", new_status="review"),
        _terminal_result(),
        _cli_result(),
    ])
    sid, _cost = await run_sdk_session(
        worker, _STREAM_AGENT_CONFIG, _stream_task_data(),
    )
    assert sid == "sess-1"
    assert worker._terminal_action_completed == {
        "tool": "update_status",
        "new_status": "review",
        "target_task": "task-1",
    }


@pytest.mark.asyncio
async def test_stream_errored_terminal_result_does_not_flag(monkeypatch):
    """(d) A refused/failed terminal call (``is_error`` — the MCP server
    marks refused terminal calls and unlocks for a retry) never sets the
    flag: a cancel after it keeps the load-bearing error path."""
    from src._agent_worker_task import run_sdk_session

    worker = _stream_worker()
    _patch_stream(monkeypatch, [
        _terminal_use(task_id="task-1", new_status="review"),
        _terminal_result(is_error=True, content="Error: brief incomplete"),
        _cli_result(),
    ])
    await run_sdk_session(worker, _STREAM_AGENT_CONFIG, _stream_task_data())
    assert worker._terminal_action_completed is None


@pytest.mark.asyncio
async def test_stream_flags_retried_terminal_after_refusal(monkeypatch):
    """A terminal call refused then RETRIED successfully still flags —
    the pre-scan buffers terminal tool_use blocks even after the output
    lock is set (the retry arrives in a post-lock message)."""
    from src._agent_worker_task import run_sdk_session

    worker = _stream_worker()
    _patch_stream(monkeypatch, [
        _terminal_use(name="mcp__cubicle-tools__move_task", block_id="t1",
                      task_id="WR-001.T01", new_status="done"),
        _terminal_result("t1", is_error=True, content="Error: not allowed"),
        _terminal_use(name="mcp__cubicle-tools__move_task", block_id="t2",
                      task_id="WR-001.T01", new_status="done"),
        _terminal_result("t2"),
        _cli_result(),
    ])
    await run_sdk_session(
        worker, _STREAM_AGENT_CONFIG, _stream_task_data("review"),
    )
    assert worker._terminal_action_completed == {
        "tool": "move_task",
        "new_status": "done",
        "target_task": "WR-001.T01",
    }


@pytest.mark.asyncio
async def test_post_terminal_cancel_executor_clean_completion():
    """(a) Executor cancelled AFTER a successful update_status→review:
    NO error activity, one clean TASK_COMPLETE with the marker; fed
    through the orchestrator it is a same-status no-op move — no
    routing, no MA noise, slot freed."""
    from src.agent_worker import AgentWorker

    w = AgentWorker(
        role="worker", agent_name="builder",
        workspace_path="/tmp/test-cb-workspace", office_id="office-1",
    )
    sent: list[dict] = []
    w._send = lambda m: sent.append(m)

    async def _session(**kwargs):
        w._terminal_action_completed = {
            "tool": "update_status", "new_status": "review",
            "target_task": "task-3",
        }
        raise asyncio.CancelledError()

    w._run_sdk_session = AsyncMock(side_effect=_session)

    await w._handle_assign_task({
        "type": "assign_task",
        "task_id": "task-3",
        "readable_id": "WR-001.T03",
        "status": "ready",  # EXECUTOR dispatch
        "agent_config": {"name": "builder"},
    })

    # The user-visible cancellation error row is SUPPRESSED.
    errors = [m for m in sent if m.get("event_type") == "error"]
    assert errors == []
    completes = [m for m in sent if m["type"] == "task_complete"]
    assert len(completes) == 1
    evt = completes[0]
    assert evt["status"] == "review"  # the terminal action put it there
    assert evt["is_review_completion"] is False
    assert evt["details"]["post_terminal_cancel"] is True
    assert "error_class" not in evt["details"]

    h = await build_harness()
    client, cls = _httpx_mock(
        {"status": "review", "readable_id": "WR-001.T03"},
        post_result={"task_id": "task-3", "old_status": "review",
                     "new_status": "review"},
    )
    with patch("httpx.AsyncClient", cls), \
            patch("src.handlers._spawn_background") as sb:
        await h.on_event("builder", dict(evt))

    h.dispatcher.on_agent_complete.assert_awaited_once_with("builder")
    moves = [
        c for c in client.post.call_args_list
        if c.kwargs.get("json", {}).get("action") == "move_task"
    ]
    assert moves == []
    sb.assert_not_called()  # no reviewer routing spawned
    h.queue_manager.add_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_terminal_cancel_reviewer_clean_completion():
    """(b) Reviewer cancelled AFTER a successful move_task→done (the
    incident shape): NO error activity, clean review-completion; the
    orchestrator's reviewer branch sees the task already done and takes
    "no action needed" — no re-queue, no board move, no rework."""
    from src.agent_worker import AgentWorker

    w = AgentWorker(
        role="worker", agent_name="auditor",
        workspace_path="/tmp/test-cb-workspace", office_id="office-1",
    )
    sent: list[dict] = []
    w._send = lambda m: sent.append(m)

    async def _session(**kwargs):
        w._terminal_action_completed = {
            "tool": "move_task", "new_status": "done",
            "target_task": "WR-001.T01",  # readable-id form matches too
        }
        raise asyncio.CancelledError()

    w._run_sdk_session = AsyncMock(side_effect=_session)

    await w._handle_assign_task({
        "type": "assign_task",
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "review",  # REVIEWER dispatch
        "agent_config": {"name": "auditor"},
    })

    errors = [m for m in sent if m.get("event_type") == "error"]
    assert errors == []
    completes = [m for m in sent if m["type"] == "task_complete"]
    assert len(completes) == 1
    evt = completes[0]
    assert evt["status"] == "review"
    assert evt["is_review_completion"] is True
    assert evt["details"]["post_terminal_cancel"] is True
    assert "error_class" not in evt["details"]

    h = await build_harness()
    client, cls = _httpx_mock({
        "status": "done", "reviewer": "auditor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })
    with patch("httpx.AsyncClient", cls):
        await h.on_event("auditor", dict(evt))

    h.dispatcher.on_agent_complete.assert_awaited_once_with("auditor")
    # "No action needed": no re-queue, no move, no MA dispatch.
    h.queue_manager.add_task.assert_not_awaited()
    assert [
        c for c in client.post.call_args_list
        if c.kwargs.get("json", {}).get("action") == "move_task"
    ] == []
    h.dispatcher.dispatch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_terminal_cancel_other_task_target_keeps_old_path():
    """A terminal action that landed on a DIFFERENT task (e.g. an MA
    board-operator move on a helper task) must NOT suppress the error
    row — the current task's work was genuinely interrupted."""
    from src.agent_worker import AgentWorker

    w = AgentWorker(
        role="worker", agent_name="dev",
        workspace_path="/tmp/test-cb-workspace", office_id="office-1",
    )
    sent: list[dict] = []
    w._send = lambda m: sent.append(m)

    async def _session(**kwargs):
        w._terminal_action_completed = {
            "tool": "move_task", "new_status": "done",
            "target_task": "task-OTHER",
        }
        raise asyncio.CancelledError()

    w._run_sdk_session = AsyncMock(side_effect=_session)

    await w._handle_assign_task({
        "type": "assign_task",
        "task_id": "task-2",
        "readable_id": "WR-001.T02",
        "status": "ready",
        "agent_config": {"name": "dev"},
    })

    errors = [m for m in sent if m.get("event_type") == "error"]
    assert len(errors) == 1  # telemetry row preserved
    completes = [m for m in sent if m["type"] == "task_complete"]
    assert len(completes) == 1
    assert completes[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_post_terminal_cancel_reviewer_same_status_keeps_old_path():
    """Degenerate guard: a reviewer flag whose status is "review" (a
    same-status update_status accepted as an idempotent no-op) keeps the
    error-classed path so the T1.1.3 tree can't consume a rework cycle
    on a cancelled session."""
    from src.agent_worker import AgentWorker

    w = AgentWorker(
        role="worker", agent_name="editor",
        workspace_path="/tmp/test-cb-workspace", office_id="office-1",
    )
    sent: list[dict] = []
    w._send = lambda m: sent.append(m)

    async def _session(**kwargs):
        w._terminal_action_completed = {
            "tool": "update_status", "new_status": "review",
            "target_task": "task-1",
        }
        raise asyncio.CancelledError()

    w._run_sdk_session = AsyncMock(side_effect=_session)

    await w._handle_assign_task({
        "type": "assign_task",
        "task_id": "task-1",
        "readable_id": "WR-001.T01",
        "status": "review",  # REVIEWER dispatch
        "agent_config": {"name": "editor"},
    })

    completes = [m for m in sent if m["type"] == "task_complete"]
    assert len(completes) == 1
    evt = completes[0]
    assert evt["status"] == "review"
    assert evt["is_review_completion"] is True
    assert evt["details"]["error_class"] == "cancelled"


# ---------------------------------------------------------------------------
# LOW-5 — fatal-branch clear_active is scoped to the event's task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fatal_event_for_stale_task_keeps_active_marker():
    """A late fatal event for an OLDER task must not wipe the active
    marker of a newer assignment."""
    h = await build_harness()
    h.queue_manager.get_active = AsyncMock(
        return_value={"task_id": "task-NEW"},
    )
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })

    with patch("httpx.AsyncClient", cls):
        await h.on_event("editor", {
            "type": "error",
            "task_id": "task-OLD",
            "fatal": True,
            "message": "late crash",
        })

    h.queue_manager.clear_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_fatal_event_for_matching_task_still_clears_active():
    h = await build_harness()
    h.queue_manager.get_active = AsyncMock(
        return_value={"task_id": "task-1"},
    )
    client, cls = _httpx_mock({
        "status": "review", "reviewer": "editor",
        "readable_id": "WR-001.T01", "rework_count": 0,
    })

    with patch("httpx.AsyncClient", cls):
        await h.on_event("editor", {
            "type": "error",
            "task_id": "task-1",
            "fatal": True,
            "message": "crash",
        })

    h.queue_manager.clear_active.assert_awaited_once_with("editor")


# ---------------------------------------------------------------------------
# Legacy rework-cap metadata remains readable but never authorizes a verdict.
# ---------------------------------------------------------------------------


class TestSyncedReworkCap:
    def test_synced_value_overrides_env_default(self):
        store = MagicMock()
        store.max_rework_cycles = 5
        assert get_max_rework_cycles(store) == 5

    def test_falls_back_to_env_default_before_first_sync(self):
        from src.handlers import MAX_REWORK_CYCLES

        store = MagicMock()
        store.max_rework_cycles = None
        assert get_max_rework_cycles(store) == MAX_REWORK_CYCLES
        assert get_max_rework_cycles(None) == MAX_REWORK_CYCLES

    def test_malformed_synced_value_falls_back(self):
        from src.handlers import MAX_REWORK_CYCLES

        store = MagicMock()
        store.max_rework_cycles = "not-an-int"
        assert get_max_rework_cycles(store) == MAX_REWORK_CYCLES

    @pytest.mark.asyncio
    async def test_config_store_parses_max_rework_cycles_from_sync(self):
        from src.config_sync.sync_service import ConfigStore

        store = ConfigStore()
        assert store.max_rework_cycles is None
        await store.update_from_sync(
            {"config": {"office_name": "o", "max_rework_cycles": 4}}
        )
        assert store.max_rework_cycles == 4
        # Malformed payload resets to None (env fallback applies).
        await store.update_from_sync(
            {"config": {"office_name": "o", "max_rework_cycles": "junk"}}
        )
        assert store.max_rework_cycles is None

    @pytest.mark.asyncio
    async def test_synced_rework_cap_cannot_authorize_implicit_approval(self):
        """Legacy count metadata cannot turn missing evidence into a verdict."""
        h = await build_harness()
        h.config_store.max_rework_cycles = 1
        client, cls = _httpx_mock({
            "status": "review", "reviewer": "editor",
            "readable_id": "WR-001.T01", "rework_count": 1,
        })

        with (
            patch("httpx.AsyncClient", cls),
            patch(
                "src.backend_client.task_has_pending_action_request",
                new_callable=AsyncMock, return_value=False,
            ),
        ):
            await h.on_event("editor", dict(REVIEWER_EVENT))

        assert _move_calls(client, "done") == []
        assert _move_calls(client, "ready") == []


@pytest.mark.parametrize("details,comment", [
    ({"cancellation_source": "shutdown", "execution_interrupted": True}, "Execution interrupted for communicator restart."),
    ({"cancellation_source": "shutdown", "error_class": "cancelled"}, "Task was cancelled."),
    ({}, "Task was cancelled."),
])
@pytest.mark.parametrize("review", [False, True])
async def test_replayed_shutdown_preserves_board_phase(details, comment, review):
    h = await build_harness()
    await h.on_event("engineer", {
        "type": "task_complete", "task_id": "real-task",
        "status": "review" if review else "blocked",
        "is_review_completion": review, "details": details, "comment": comment,
    })
    h.queue_manager.clear_active.assert_awaited_once_with("engineer", "real-task")
    h.dispatcher.wake.assert_called_once()
    h.dispatcher.on_agent_complete.assert_not_awaited()
    h.router.publish_event.assert_not_awaited()
    h.queue_manager.add_task.assert_not_awaited()


def _progress_text(text: str):
    from src.docker.session_bridge import SessionMessage

    return SessionMessage(type="assistant", data={"message": {"content": [
        {"type": "text", "text": text},
    ]}})


def _checkpoint_contents(worker):
    return [
        call.args[0]["content"] for call in worker._send.call_args_list
        if call.args[0].get("event_type") == "checkpoint"
    ]


async def test_refused_terminal_restores_progress_then_success_locks_again(monkeypatch):
    from src._agent_worker_task import run_sdk_session

    worker = _stream_worker()
    _patch_stream(monkeypatch, [
        _terminal_use(task_id="task-1", new_status="review"),
        _progress_text("Hidden until the pending submission settles."),
        _terminal_result(is_error=True, content="Error: missing required evidence"),
        _progress_text("I am adding the missing evidence."),
        _terminal_use(block_id="term-2", task_id="task-1", new_status="review"),
        _terminal_result("term-2"),
        _progress_text("No more activity after submission."),
        _cli_result(),
    ])
    await run_sdk_session(worker, _STREAM_AGENT_CONFIG, _stream_task_data())
    assert _checkpoint_contents(worker) == ["I am adding the missing evidence."]
    assert worker._terminal_action_completed["new_status"] == "review"


@pytest.mark.parametrize("task_id,status", [
    ("task-1", "blocked"), ("task-1", "in_progress"),
    ("helper-task", "ready"), ("helper-task", "done"), ("", "done"),
])
async def test_nonlocking_or_other_task_moves_preserve_visible_progress(monkeypatch, task_id, status):
    from src._agent_worker_task import run_sdk_session

    worker = _stream_worker()
    _patch_stream(monkeypatch, [
        _terminal_use(name="mcp__cubicle-tools__move_task", task_id=task_id, new_status=status),
        _terminal_result(),
        _progress_text("The remaining action needs this explanation."),
        _cli_result(),
    ])
    await run_sdk_session(worker, _STREAM_AGENT_CONFIG, _stream_task_data("review"))
    assert _checkpoint_contents(worker) == ["The remaining action needs this explanation."]
    if task_id != "task-1":
        assert worker._terminal_action_completed is None


async def test_parallel_refused_terminal_keeps_pending_submission_locked(monkeypatch):
    from src._agent_worker_task import run_sdk_session

    worker = _stream_worker()
    _patch_stream(monkeypatch, [
        _terminal_use(block_id="one", task_id="task-1", new_status="review"),
        _terminal_use(block_id="two", task_id="task-1", new_status="review"),
        _terminal_result("one", is_error=True),
        _progress_text("A terminal operation is still pending."),
        _terminal_result("two", is_error=True),
        _progress_text("Both submissions failed; fixing the cause."),
        _cli_result(),
    ])
    await run_sdk_session(worker, _STREAM_AGENT_CONFIG, _stream_task_data())
    assert _checkpoint_contents(worker) == ["Both submissions failed; fixing the cause."]
    assert worker._terminal_action_completed is None


async def test_human_request_locks_progress_and_records_confirmed_task_yield(monkeypatch):
    from src._agent_worker_task import run_sdk_session

    worker = _stream_worker()
    _patch_stream(monkeypatch, [
        _terminal_use(name="mcp__cubicle-tools__request_user_action", question="Choose a project.", response_mode="text"),
        _terminal_result(),
        _progress_text("Should stop after the human request."),
        _cli_result(),
    ])
    await run_sdk_session(worker, _STREAM_AGENT_CONFIG, _stream_task_data())
    assert _checkpoint_contents(worker) == []
    assert worker._terminal_action_completed == {
        "tool": "request_user_action", "new_status": "blocked", "target_task": "task-1",
    }


async def test_unconfirmed_terminal_on_failed_stream_does_not_hide_retry_progress(monkeypatch):
    from src._agent_worker_task import run_sdk_session
    from src.docker import session_bridge
    from src.docker.session_bridge import SessionMessage

    worker = _stream_worker()
    streams = iter([
        [
            _terminal_use(task_id="task-1", new_status="review"),
            SessionMessage(type="error", data={"error": "error: unknown option '--effort'"}),
        ],
        [_progress_text("Recovered the interrupted submission."), _cli_result()],
    ])

    def factory(**kwargs):
        messages = next(streams)

        async def stream():
            for message in messages:
                yield message

        return stream()

    monkeypatch.setattr(session_bridge, "stream_cli_session", factory)
    await run_sdk_session(worker, {**_STREAM_AGENT_CONFIG, "effort": "xhigh"}, _stream_task_data())
    assert "Recovered the interrupted submission." in _checkpoint_contents(worker)
    assert worker._terminal_action_completed is None
