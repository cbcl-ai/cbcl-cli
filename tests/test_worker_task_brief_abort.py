"""T3.2.4 (03/#17) — brief-fetch failure aborts the attempt.

Reconcile-added queue entries carry no ``brief``; the worker's fresh
``get_task_detail`` fetch normally repairs that. Before this fix a
fetch FAILURE only logged and proceeded — the worker prompt rendered
an empty contract ("Goal: Not specified") and burned a full Opus
session on un-reviewable output. Pins:

* fetch fails + no usable brief in hand → NO CLI session starts, the
  skip sentinel ``(None, None)`` is returned (orchestrator keeps the
  status; the reconciler re-adds the entry), and a non-fatal error
  activity is emitted;
* fetch fails even with a carried brief → waits for authoritative state;
* planner consults (own objective, no backend task row) keep the
  existing tolerance and still run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src._agent_worker_task import _brief_is_usable, _retry_admission_reason, run_sdk_session
from src.docker.session_bridge import SessionMessage


def _fake_worker() -> MagicMock:
    worker = MagicMock()
    worker.backend_url = "http://backend.test:8000"
    worker.office_id = "office-1"
    worker.agent_name = "analyst"
    worker.workspace_path = "/tmp/cbcl-test-workspace"
    worker._send = MagicMock()
    worker._build_mcp_config = MagicMock(return_value={})
    return worker


def _failing_httpx_factory():
    """An ``httpx.AsyncClient`` whose ``post`` always raises."""
    client = MagicMock()
    client.post = AsyncMock(side_effect=OSError("backend unreachable"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _detail_httpx_factory(detail: dict):
    """An ``httpx.AsyncClient`` whose ``post`` returns HTTP 200 with
    ``detail`` as the JSON body (the get_task_detail success shape)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=detail)
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


class TestBriefIsUsable:

    def test_missing_or_empty(self):
        assert _brief_is_usable(None) is False
        assert _brief_is_usable({}) is False
        assert _brief_is_usable("not a dict") is False
        assert _brief_is_usable({"goal": ""}) is False
        assert _brief_is_usable({"goal": "   "}) is False
        assert _brief_is_usable({"context": "x"}) is False

    def test_goal_present(self):
        assert _brief_is_usable({"goal": "Ship the thing"}) is True


class TestBriefFetchAbort:

    @pytest.mark.parametrize("detail", [
        {"status": "in_progress", "execution_blocked": True},
        {"status": "archived", "execution_blocked": False},
        {"status": "review", "execution_blocked": False},
        {"status": "in_progress", "assigned_agent": "someone-else"},
    ])
    async def test_retry_rechecks_current_admission(self, detail):
        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)):
            assert await _retry_admission_reason(_fake_worker(), "task-id", "in_progress")

    async def test_retry_waits_on_authoritative_lookup_failure(self):
        with patch("httpx.AsyncClient", _failing_httpx_factory()):
            assert await _retry_admission_reason(_fake_worker(), "task-id", "in_progress")

    async def test_retry_allowed_when_authoritative_phase_is_unchanged(self):
        detail = {"status": "in_progress", "execution_blocked": False, "assigned_agent": "analyst"}
        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)):
            assert await _retry_admission_reason(_fake_worker(), "task-id", "in_progress") is None

    async def test_manager_assistant_triage_keeps_original_executor(self):
        worker = _fake_worker()
        worker.agent_name = "manager-assistant"
        detail = {"status": "blocked", "assigned_agent": "original-executor"}
        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)):
            assert await _retry_admission_reason(worker, "task-id", "blocked") is None
        assert detail["assigned_agent"] == "original-executor"

    async def test_fetch_failure_without_brief_aborts_no_cli_spawn(self):
        worker = _fake_worker()
        stream_spy = MagicMock()

        def _no_stream(*args, **kwargs):  # pragma: no cover — must not run
            stream_spy(*args, **kwargs)
            raise AssertionError("CLI session must not start")

        task_data = {
            "task_id": "task-123",
            "readable_id": "WR-001.T05",
            "status": "ready",
            # No brief — the reconcile-added entry shape.
        }
        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        with patch("httpx.AsyncClient", _failing_httpx_factory()), \
                patch.object(sb, "stream_cli_session", _no_stream):
            session_id, total_cost = await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        # Skip sentinel: orchestrator keeps the task's status and the
        # reconciler re-adds the queue entry — re-queue, not drop.
        assert (session_id, total_cost) == (None, None)
        stream_spy.assert_not_called()
        # Non-fatal error activity emitted.
        error_frames = [
            call.args[0] for call in worker._send.call_args_list
            if call.args[0].get("event_type") == "error"
        ]
        assert len(error_frames) == 1
        assert "brief" in error_frames[0]["content"].lower()
        assert error_frames[0]["details"]["error_class"] == (
            "brief_fetch_failed"
        )
        assert error_frames[0]["details"]["retryable"] is True

    async def test_fetch_failure_with_carried_brief_waits_for_authoritative_state(self):
        worker = _fake_worker()

        async def _ok_stream(*args, **kwargs):
            yield SessionMessage(
                type="result",
                data={"session_id": "sess-1", "cost_usd": 0.01},
            )

        task_data = {
            "task_id": "task-456",
            "readable_id": "WR-001.T06",
            "status": "ready",
            "brief": {"goal": "Do the contracted thing"},
        }
        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        with patch("httpx.AsyncClient", _failing_httpx_factory()), \
                patch.object(sb, "stream_cli_session", _ok_stream):
            session_id, total_cost = await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        assert session_id is None
        assert total_cost is None
        assert task_data["_execution_deferred_reason"]

    @pytest.mark.parametrize("status", ["ready", "in_progress", "review", "blocked"])
    async def test_pending_stop_never_starts_board_session(self, status):
        worker = _fake_worker()
        stream = MagicMock(side_effect=AssertionError("CLI must not start"))
        task_data = {"task_id": "task-held", "status": status,
                     "brief": {"goal": "Existing complete brief"}}
        detail = {"status": status, "assigned_agent": "analyst",
                  "execution_blocked": True,
                  "execution_blocked_reason": "A cancelled worker has not stopped."}
        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
            "src.docker.session_bridge.stream_cli_session", stream
        ):
            result = await run_sdk_session(worker, agent_config={}, task_data=task_data)
        assert result == (None, None)
        stream.assert_not_called()
        assert task_data["_execution_deferred_reason"] == detail["execution_blocked_reason"]

    async def test_planner_consult_exempt_from_abort(self):
        worker = _fake_worker()
        worker.agent_name = "planner"

        async def _ok_stream(*args, **kwargs):
            yield SessionMessage(
                type="result",
                data={"session_id": "sess-planner", "cost_usd": 0.02},
            )

        task_data = {
            "task_id": "planner-abc123",
            "readable_id": "PLAN",
            "status": "planning",
            "planner_consult": {
                "mode": "roadmap",
                "objective": "Plan the workstream",
                "workstream_id": "ws-1",
                "scope_id": "",
            },
        }
        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        # httpx is never used (the fetch is skipped for planner mode);
        # patch it to raise anyway as belt-and-braces.
        with patch("httpx.AsyncClient", _failing_httpx_factory()), \
                patch.object(sb, "stream_cli_session", _ok_stream):
            session_id, total_cost = await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        assert session_id == "sess-planner"
        assert total_cost == 0.02
        # No abort error frame.
        error_frames = [
            call.args[0] for call in worker._send.call_args_list
            if call.args[0].get("event_type") == "error"
        ]
        assert error_frames == []


class TestArtifactsPartialReviewAbort:
    """ADD-D1 residue — a PARTIAL artifact fetch must not produce a BLIND
    review. The backend flags ``artifacts_partial=True`` when it could not
    assemble the deliverable list; the reviewer dispatch then aborts +
    re-queues. The abort is review-only and keys on the FLAG, never on an
    empty list (a legitimately artifact-less review must proceed)."""

    def _review_detail(self, *, partial: bool, status: str = "review") -> dict:
        return {
            "status": status,
            "reviewer": "auditor",
            "assigned_agent": "analyst",
            "rework_count": 0,
            "title": "Reviewable task",
            "brief": {"goal": "Verify the deliverable"},
            "recent_activities": [],
            "artifacts": [],
            "artifacts_partial": partial,
        }

    async def test_partial_artifacts_aborts_review_no_cli_spawn(self):
        worker = _fake_worker()
        worker.agent_name = "auditor"  # the designated reviewer
        stream_spy = MagicMock()

        def _no_stream(*args, **kwargs):  # pragma: no cover — must not run
            stream_spy(*args, **kwargs)
            raise AssertionError("CLI session must not start")

        task_data = {
            "task_id": "task-789",
            "readable_id": "WR-001.T07",
            "status": "review",
        }
        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        with patch(
            "httpx.AsyncClient",
            _detail_httpx_factory(self._review_detail(partial=True)),
        ), patch.object(sb, "stream_cli_session", _no_stream):
            session_id, total_cost = await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        assert (session_id, total_cost) == (None, None)
        stream_spy.assert_not_called()
        error_frames = [
            call.args[0] for call in worker._send.call_args_list
            if call.args[0].get("event_type") == "error"
        ]
        assert len(error_frames) == 1
        assert error_frames[0]["details"]["error_class"] == (
            "artifacts_fetch_partial"
        )
        assert error_frames[0]["details"]["retryable"] is True

    async def test_review_without_partial_proceeds(self):
        worker = _fake_worker()
        worker.agent_name = "auditor"

        async def _ok_stream(*args, **kwargs):
            yield SessionMessage(
                type="result",
                data={"session_id": "sess-rev", "cost_usd": 0.0},
            )

        task_data = {
            "task_id": "task-791",
            "readable_id": "WR-001.T09",
            "status": "review",
        }
        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        with patch(
            "httpx.AsyncClient",
            _detail_httpx_factory(self._review_detail(partial=False)),
        ), patch.object(sb, "stream_cli_session", _ok_stream):
            session_id, total_cost = await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        assert session_id == "sess-rev"
        error_frames = [
            call.args[0] for call in worker._send.call_args_list
            if call.args[0].get("event_type") == "error"
        ]
        assert error_frames == []

    async def test_missing_reviewer_defers_until_claim_reconciliation(self):
        """Missing ownership cannot grant an MA session review permission.

        Backend execution claims reconcile a missing reviewer before launch;
        if that ownership vanishes afterward, dispatch must obtain a new claim.
        """
        worker = _fake_worker()
        worker.agent_name = "manager-assistant"
        stream = MagicMock(side_effect=AssertionError("CLI must not start"))
        detail = self._review_detail(partial=True)
        detail["reviewer"] = ""
        detail["assigned_agent"] = ""
        task_data = {"task_id": "task-792", "status": "review"}
        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
            "src.docker.session_bridge.stream_cli_session", stream,
        ):
            assert await run_sdk_session(worker, {}, task_data) == (None, None)
        stream.assert_not_called()
        assert "ownership changed" in task_data["_execution_deferred_reason"]

    async def test_partial_flag_on_non_review_executor_proceeds(self):
        """Same partial flag, but the task is in_progress and we're the
        executor — the abort is review-only, so execution proceeds."""
        worker = _fake_worker()
        worker.agent_name = "analyst"

        async def _ok_stream(*args, **kwargs):
            yield SessionMessage(
                type="result",
                data={"session_id": "sess-exec", "cost_usd": 0.0},
            )

        task_data = {
            "task_id": "task-790",
            "readable_id": "WR-001.T08",
            "status": "in_progress",
        }
        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        with patch(
            "httpx.AsyncClient",
            _detail_httpx_factory(
                self._review_detail(partial=True, status="in_progress")
            ),
        ), patch.object(sb, "stream_cli_session", _ok_stream):
            session_id, total_cost = await run_sdk_session(
                worker, agent_config={"model": "claude-opus-4-7"},
                task_data=task_data,
            )

        assert session_id == "sess-exec"
        error_frames = [
            call.args[0] for call in worker._send.call_args_list
            if call.args[0].get("event_type") == "error"
        ]
        assert error_frames == []


class TestPhaseSpecificAdmission:
    @pytest.mark.parametrize("dispatched_status,current_status,agent,assignee,reviewer", [
        ("ready", "review", "analyst", "analyst", "auditor"),
        ("review", "in_progress", "auditor", "analyst", "auditor"),
        ("review", "blocked", "manager-assistant", "analyst", "manager-assistant"),
        ("blocked", "ready", "manager-assistant", "manager-assistant", "auditor"),
        ("ready", "ready", "analyst", "analyst", "auditor"),
        ("in_progress", "in_progress", "analyst", "engineer", "analyst"),
        ("review", "review", "analyst", "analyst", "auditor"),
        ("blocked", "blocked", "analyst", "analyst", "auditor"),
        ("review", "review", "analyst", "analyst", "analyst"),
        ("in_progress", "in_progress", "analyst", "", "analyst"),
        ("review", "review", "manager-assistant", "analyst", ""),
        ("in_progress", "done", "analyst", "analyst", "auditor"),
        ("review", "archived", "auditor", "analyst", "auditor"),
    ])
    async def test_start_and_retry_refuse_wrong_phase_or_owner(
        self, dispatched_status, current_status, agent, assignee, reviewer,
    ):
        worker = _fake_worker()
        worker.agent_name = agent
        detail = {"status": current_status, "assigned_agent": assignee,
                  "reviewer": reviewer, "brief": {"goal": "Ship the thing"}}
        task = {"task_id": "task-1", "status": dispatched_status}
        stream = MagicMock(side_effect=AssertionError("CLI must not start"))
        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
            "src.docker.session_bridge.stream_cli_session", stream,
        ):
            assert await run_sdk_session(worker, {}, task) == (None, None)
            assert await _retry_admission_reason(worker, "task-1", dispatched_status)
        assert task["status"] == dispatched_status
        assert task["_execution_deferred_reason"]
        stream.assert_not_called()

    @pytest.mark.parametrize("changed", [
        {"execution_cycle": 4}, {"execution_generation": 9},
        {"review_retry_epoch": 2}, {"assigned_agent": "another-executor"},
        {"human_action_request_id": "human-request"},
    ])
    async def test_same_phase_review_refuses_superseded_claim_or_human_wait(self, changed):
        worker = _fake_worker()
        worker.agent_name = "auditor"
        task = {"task_id": "task-1", "status": "review", "execution_cycle": 3,
                "execution_generation": 8, "review_retry_epoch": 1,
                "execution_assignee": "analyst"}
        detail = {**task, "assigned_agent": "analyst", "reviewer": "auditor", **changed}
        stream = MagicMock(side_effect=AssertionError("CLI must not start"))
        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
            "src.docker.session_bridge.stream_cli_session", stream,
        ):
            assert await run_sdk_session(worker, {}, task) == (None, None)
            assert await _retry_admission_reason(worker, "task-1", "review", task)
        stream.assert_not_called()

    async def test_fresh_assignment_policy_and_explicit_nulls_replace_queued_fields(self):
        worker = _fake_worker()
        task = {"task_id": "task-1", "status": "ready", "task_class": "assignment",
                "effort_hint": "ultracode", "scope_id": "old-scope",
                "description": "Old instruction", "reviewer": "old-reviewer"}
        detail = {"status": "in_progress", "assigned_agent": "analyst",
                  "reviewer": "auditor", "brief": {"goal": "Answer the question"},
                  "description": "Current instruction", "task_class": "ask",
                  "effort_hint": None, "scope_id": None}
        stream_kwargs = []

        async def stream(**kwargs):
            stream_kwargs.append(kwargs)
            yield SessionMessage(type="result", data={"session_id": "session-1", "cost_usd": 0.01})

        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
            "src.docker.session_bridge.stream_cli_session", stream,
        ):
            assert await run_sdk_session(
                worker, {"model": "claude-opus-4-7", "effort": "xhigh"}, task,
            ) == ("session-1", 0.01)
        assert task["status"] == "in_progress"
        assert task["description"] == "Current instruction"
        assert task["scope_id"] is None
        assert task["effort_hint"] is None
        assert worker._build_mcp_config.call_args.kwargs["task_class"] == "ask"
        assert "Workflow" in stream_kwargs[0]["disallowed_tools"]

    async def test_review_refresh_preserves_original_executor_identity(self):
        worker = _fake_worker()
        worker.agent_name = "auditor"
        task = {"task_id": "task-1", "status": "review"}
        detail = {"status": "review", "assigned_agent": "analyst", "reviewer": "auditor",
                  "brief": {"goal": "Check deliverable"}}

        async def stream(**kwargs):
            yield SessionMessage(type="result", data={"session_id": "review-1", "cost_usd": 0.01})

        with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
            "src.docker.session_bridge.stream_cli_session", stream,
        ):
            assert await run_sdk_session(worker, {"model": "claude-opus-4-7"}, task) == ("review-1", 0.01)
        assert task["assigned_agent"] == "analyst"
        assert worker._build_mcp_config.call_args.kwargs["task_mode"] == "review"


@pytest.mark.parametrize("brief", [None, {}, "malformed", {"goal": ""}, {"goal": []}, {"goal": {"text": "missing string"}}])
async def test_successful_detail_lookup_with_unusable_brief_never_uses_stale_contract(brief):
    worker = _fake_worker()
    task = {"task_id": "task-1", "status": "in_progress", "brief": {"goal": "Stale contract"}}
    detail = {"status": "in_progress", "assigned_agent": "analyst", "brief": brief}
    stream = MagicMock(side_effect=AssertionError("CLI must not start"))
    with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
        "src.docker.session_bridge.stream_cli_session", stream,
    ):
        assert await run_sdk_session(worker, {}, task) == (None, None)
    assert "brief" in task["_execution_deferred_reason"]
    stream.assert_not_called()


@pytest.mark.parametrize("status,assignee,reviewer", [
    ("in_progress", "manager-assistant", "auditor"),
    ("review", "analyst", "manager-assistant"),
    ("blocked", "analyst", "auditor"),
])
async def test_manager_assistant_has_only_the_claimed_phase_with_real_executor_preserved(status, assignee, reviewer):
    worker = _fake_worker()
    worker.agent_name = "manager-assistant"
    task = {"task_id": "task-1", "status": status}
    detail = {"status": status, "assigned_agent": assignee, "reviewer": reviewer,
              "brief": {"goal": "Do the authorized work"}}

    async def stream(**kwargs):
        yield SessionMessage(type="result", data={"session_id": "session-1", "cost_usd": 0.01})

    with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
        "src.docker.session_bridge.stream_cli_session", stream,
    ):
        assert await run_sdk_session(worker, {"model": "claude-opus-4-7"}, task) == ("session-1", 0.01)
        assert await _retry_admission_reason(worker, "task-1", status) is None
    assert task["assigned_agent"] == assignee
    expected_mode = {"in_progress": "execute", "review": "review", "blocked": "triage"}[status]
    assert worker._build_mcp_config.call_args.kwargs["task_mode"] == expected_mode


async def test_phase_drift_at_initial_fetch_reports_skip_without_executor_completion():
    from src._agent_worker_task import handle_assign_task

    worker = _fake_worker()
    task = {"task_id": "task-1", "status": "in_progress", "agent_config": {}}
    detail = {"status": "review", "assigned_agent": "analyst", "reviewer": "auditor"}

    async def run(**kwargs):
        return await run_sdk_session(worker, **kwargs)

    worker._run_sdk_session = run
    stream = MagicMock(side_effect=AssertionError("CLI must not start"))
    worker._sidechain_failures = 0
    worker._pending_spawns = 0
    with patch("httpx.AsyncClient", _detail_httpx_factory(detail)), patch(
        "src.docker.session_bridge.stream_cli_session", stream,
    ):
        await handle_assign_task(worker, task)
    completions = [call.args[0] for call in worker._send.call_args_list if call.args[0].get("type") == "task_complete"]
    assert len(completions) == 1
    assert completions[0]["review_skipped"] is True
    assert completions[0]["is_review_completion"] is True
    assert completions[0]["status"] == "in_progress"
    assert "phase changed" in completions[0]["execution_deferred"]
    stream.assert_not_called()


@pytest.mark.parametrize("status,agent,reviewer", [
    ("in_progress", "analyst", "auditor"),
    ("review", "auditor", "auditor"),
    ("blocked", "manager-assistant", "auditor"),
])
async def test_http_success_with_malformed_json_never_runs_from_queued_state(status, agent, reviewer):
    worker = _fake_worker()
    worker.agent_name = agent
    task = {"task_id": "task-1", "status": status, "assigned_agent": "analyst",
            "reviewer": reviewer, "brief": {"goal": "A valid but stale queued contract"}}
    factory = _detail_httpx_factory({})
    client = factory.return_value.__aenter__.return_value
    client.post.return_value.json.side_effect = ValueError("Truncated JSON body")
    stream = MagicMock(side_effect=AssertionError("CLI must not start"))
    with patch("httpx.AsyncClient", factory), patch(
        "src.docker.session_bridge.stream_cli_session", stream,
    ):
        assert await run_sdk_session(worker, {}, task) == (None, None)
        assert await _retry_admission_reason(worker, "task-1", status)
    stream.assert_not_called()
    assert "Cannot confirm" in task["_execution_deferred_reason"]
    assert task["status"] == status
