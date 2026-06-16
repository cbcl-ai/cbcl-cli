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
* fetch fails but the queue entry CARRIES a usable brief → proceeds;
* planner consults (own objective, no backend task row) keep the
  existing tolerance and still run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src._agent_worker_task import _brief_is_usable, run_sdk_session
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

    async def test_fetch_failure_with_carried_brief_proceeds(self):
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
            # The task_ready dispatch shape DOES carry the brief —
            # a transient fetch blip must not abort it.
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

        assert session_id == "sess-1"
        assert total_cost == 0.01

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

    async def test_partial_artifacts_aborts_empty_reviewer_ma_fallback(self):
        """Empty-reviewer fallback: a review-status task with reviewer=""
        is routed to the Manager Assistant (agent_queue: ``reviewer or
        "manager-assistant"``). The abort must fire even though
        ``reviewer != agent_name`` — the gate keys on review-STATUS, not
        reviewer-match. (Assigned_agent is empty here so the MA passes the
        authorization check via ``not current_agent`` and reaches the
        guard.) This case ships a BLIND verdict under a reviewer-match
        gate."""
        worker = _fake_worker()
        worker.agent_name = "manager-assistant"
        stream_spy = MagicMock()

        def _no_stream(*args, **kwargs):  # pragma: no cover — must not run
            stream_spy(*args, **kwargs)
            raise AssertionError("CLI session must not start")

        detail = self._review_detail(partial=True)
        detail["reviewer"] = ""  # no designated reviewer → MA fallback
        detail["assigned_agent"] = ""  # stranded assignee → MA passes auth

        task_data = {
            "task_id": "task-792",
            "readable_id": "WR-001.T10",
            "status": "review",
        }
        sb = __import__(
            "src.docker.session_bridge", fromlist=["stream_cli_session"],
        )
        with patch(
            "httpx.AsyncClient", _detail_httpx_factory(detail),
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
