"""Tests for T1.1.6 — Planner ingest + task_complete routing must not run
inside the supervisor's 30s-bounded ``_on_event`` callback.

``agent_supervisor`` bounds every callback with ``asyncio.wait_for`` (30s
for task_complete). ``ingest_planner_result`` is a FULL Manager turn, so
awaiting it inline got the Planner done-poke cancelled whenever the Manager
was busy. The fix spawns the slow legs via ``_spawn_background``:

* planner task_complete  → ingest in background; callback returns fast;
* planner error          → failure poke in background; cleanup stays inline;
* executor task_complete → the move stays inline, the routing leg (task
  fetch + queue add + dispatch) runs in background.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_review_circuit_breaker import (
    Harness,
    _httpx_mock,
    build_harness,
)


async def _drain_background() -> None:
    """Let spawned background tasks run to completion."""
    from src.handlers import _BACKGROUND_TASKS

    for _ in range(20):
        pending = [t for t in _BACKGROUND_TASKS if not t.done()]
        if not pending:
            break
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Planner done-poke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_complete_returns_fast_while_manager_busy():
    """The callback returns well under the 30s supervisor bound even while
    a (mocked) Manager turn is still chewing; the ingest completes
    afterwards and the poke is delivered."""
    h = await build_harness()

    release = asyncio.Event()
    ingested = asyncio.Event()

    async def _slow_ingest(payload):
        await release.wait()  # simulate a busy Manager turn
        ingested.set()

    h.mgr.ingest_planner_result = AsyncMock(side_effect=_slow_ingest)

    event = {
        "type": "task_complete",
        "task_id": "planner-abc123",
        "status": "planning",
        "is_review_completion": True,
        "planner_consult": {"mode": "roadmap"},
    }

    # Must return promptly — NOT wait for the Manager turn.
    await asyncio.wait_for(h.on_event("planner", event), timeout=1.0)

    assert not ingested.is_set()
    # The planner was published idle immediately (truthful — its session
    # is over even though the Manager is still ingesting).
    idle_events = [
        c.args[0] for c in h.router.publish_event.call_args_list
        if c.args[0].get("type") == "agent_status_changed"
        and c.args[0].get("agent_name") == "planner"
    ]
    assert idle_events and idle_events[0]["status"] == "idle"

    # Release the Manager; the background ingest completes (poke delivered).
    release.set()
    await asyncio.wait_for(ingested.wait(), timeout=1.0)
    h.mgr.ingest_planner_result.assert_awaited_once()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert payload["planner_consult"] == {"mode": "roadmap"}


@pytest.mark.asyncio
async def test_planner_error_poke_spawned_in_background():
    """The planner-error failure poke is also spawned; cleanup (clear
    active + idle publication) happens inline before the ingest finishes."""
    h = await build_harness()

    release = asyncio.Event()

    async def _slow_ingest(payload):
        await release.wait()

    h.mgr.ingest_planner_result = AsyncMock(side_effect=_slow_ingest)

    event = {
        "type": "error",
        "task_id": "planner-abc123",
        "fatal": True,
        "message": "CLI exploded",
        "planner_consult": {"mode": "verify"},
    }

    await asyncio.wait_for(h.on_event("planner", event), timeout=1.0)

    # Inline cleanup happened even though the ingest is still blocked.
    h.queue_manager.clear_active.assert_awaited_once_with("planner")
    idle_events = [
        c.args[0] for c in h.router.publish_event.call_args_list
        if c.args[0].get("type") == "agent_status_changed"
    ]
    assert idle_events and idle_events[0]["status"] == "idle"

    release.set()
    await _drain_background()
    h.mgr.ingest_planner_result.assert_awaited_once()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert payload["planner_error"] == "CLI exploded"


@pytest.mark.asyncio
async def test_heartbeat_killed_planner_routes_to_failure_poke():
    """MEDIUM-4: a supervisor-SYNTHESIZED fatal event (heartbeat kill /
    process exit) carries no planner_consult marker — the fatal branch
    must still detect the Planner (agent name / synthetic task id) and
    deliver the failure poke instead of trying board recovery on a
    non-existent task (404 fetch noise + the Manager waiting on
    "engaged" forever)."""
    h = await build_harness()

    client = MagicMock()
    client.get = AsyncMock()
    client.post = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    event = {
        "type": "error",
        "agent_name": "planner",
        "fatal": True,
        "reason": "heartbeat_timeout",
        "task_id": "planner-abc123",
        "elapsed_seconds": 120.0,
    }
    with patch("httpx.AsyncClient", MagicMock(return_value=cm)):
        await asyncio.wait_for(h.on_event("planner", event), timeout=1.0)
        await _drain_background()

    # Failure poke delivered (spawned-not-inline, T1.1.6 shape).
    h.mgr.ingest_planner_result.assert_awaited_once()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "killed" in payload["planner_error"]
    assert "heartbeat_timeout" in payload["planner_error"]
    # Inline cleanup ran; NO board-recovery fetch on the synthetic id.
    h.queue_manager.clear_active.assert_awaited_once_with("planner")
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_killed_scope_plan_consult_pokes_with_stashed_context():
    """Round-2 LOW (MEDIUM-4 follow-up): a heartbeat-killed consult's
    failure poke must recover mode + workstream_id (→ context_key) from
    the spawn-time stash instead of defaulting to roadmap/general_chat."""
    from src.handlers import _planner_consults

    h = await build_harness()

    # Spawn a scope_plan consult through the REAL handler so the stash
    # is written exactly where production writes it.
    consult_handler = next(
        c.args[1] for c in h.router.on.call_args_list
        if c.args[0] == "consult_planner"
    )
    h.supervisor.spawn_worker = AsyncMock(return_value=True)
    h.config_store.get_agent.return_value = {"name": "planner"}
    h.config_store.get_workstream.return_value = {}
    # Suppress the planner-heartbeat background task only.
    with patch("src.handlers.asyncio.create_task"):
        await consult_handler({
            "mode": "scope_plan",
            "objective": "plan the auth scope",
            "workstream_id": "ws-1",
            "scope_id": "scope-1",
        })
    task_data = h.supervisor.spawn_worker.call_args.args[2]
    synthetic_id = task_data["task_id"]
    assert _planner_consults[synthetic_id]["mode"] == "scope_plan"

    # Supervisor-SYNTHESIZED kill: no planner_consult marker on the event.
    event = {
        "type": "error",
        "fatal": True,
        "reason": "heartbeat_timeout",
        "task_id": synthetic_id,
    }
    await asyncio.wait_for(h.on_event("planner", event), timeout=1.0)
    await _drain_background()

    h.mgr.ingest_planner_result.assert_awaited_once()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    # The recovered marker rides the poke — ingest_planner_result derives
    # context_key "workstream:ws-1" + the scope_plan failure body from it.
    assert payload["planner_consult"]["mode"] == "scope_plan"
    assert payload["planner_consult"]["workstream_id"] == "ws-1"
    assert "killed" in payload["planner_error"]
    # Stash pruned on the exit path.
    assert synthetic_id not in _planner_consults


@pytest.mark.asyncio
async def test_killed_verify_consult_is_silent_no_poke(caplog):
    """Round-2 LOW: a killed BACKEND-fired verify consult must NOT poke
    the Manager (same verify-silence rule as the consult-drop path) —
    the stuck-verifying sweeper owns recovery. Cleanup still runs and
    the stash entry is pruned."""
    from src.handlers import _planner_consults

    h = await build_harness()
    _planner_consults["planner-verify1"] = {
        "mode": "verify",
        "objective": "",
        "workstream_id": "ws-1",
        "scope_id": "scope-1",
    }
    event = {
        "type": "error",
        "fatal": True,
        "reason": "heartbeat_timeout",
        "task_id": "planner-verify1",
    }
    with caplog.at_level("INFO", logger="cbcl.handlers"):
        await asyncio.wait_for(h.on_event("planner", event), timeout=1.0)
        await _drain_background()

    # NO Manager poke — the sweeper owns verify recovery.
    h.mgr.ingest_planner_result.assert_not_awaited()
    assert any(
        "stuck-verifying sweeper" in r.message for r in caplog.records
    )
    # Inline cleanup still ran; stash pruned.
    h.queue_manager.clear_active.assert_awaited_once_with("planner")
    assert "planner-verify1" not in _planner_consults


@pytest.mark.asyncio
async def test_planner_ingest_exception_is_logged_not_raised(caplog):
    """A crash inside the background ingest is logged, never propagated."""
    h = await build_harness()
    h.mgr.ingest_planner_result = AsyncMock(side_effect=RuntimeError("boom"))

    event = {
        "type": "task_complete",
        "task_id": "planner-abc123",
        "is_review_completion": True,
        "planner_consult": True,
    }
    with caplog.at_level("ERROR", logger="cbcl.handlers"):
        await h.on_event("planner", event)
        await _drain_background()

    assert any(
        "ingest_planner_result failed" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Executor task_complete routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_complete_routing_runs_in_background_with_slow_backend():
    """The executor task_complete callback returns fast even when the
    routing leg's backend fetch is slow; the reviewer dispatch still
    happens afterwards."""
    h = await build_harness()

    release = asyncio.Event()

    # Fast POST (the inline move), slow GET (the background routing fetch).
    client = MagicMock()
    post_resp = MagicMock(status_code=200)
    post_resp.json.return_value = {
        "old_status": "in_progress", "new_status": "review",
    }
    post_resp.text = ""
    client.post = AsyncMock(return_value=post_resp)

    get_resp = MagicMock(status_code=200)
    get_resp.json.return_value = {
        "reviewer": "editor", "readable_id": "WR-001.T01",
        "status": "review",
    }

    async def _slow_get(*a, **kw):
        await release.wait()
        return get_resp

    client.get = AsyncMock(side_effect=_slow_get)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)

    event = {
        "type": "task_complete",
        "task_id": "task-1",
        "status": "review",
        "is_review_completion": False,
        "comment": "done",
    }

    with patch("httpx.AsyncClient", MagicMock(return_value=cm)):
        # Returns fast: the inline move POST completes, the slow routing
        # GET is parked in a background task.
        await asyncio.wait_for(h.on_event("worker-1", event), timeout=1.0)

        # Move happened inline; routing hasn't (GET still blocked).
        client.post.assert_awaited_once()
        h.queue_manager.add_task.assert_not_awaited()

        # Agent idle was published after the move, before routing.
        idle_events = [
            c.args[0] for c in h.router.publish_event.call_args_list
            if c.args[0].get("type") == "agent_status_changed"
            and c.args[0].get("agent_name") == "worker-1"
        ]
        assert idle_events and idle_events[0]["status"] == "idle"

        # Unblock the backend — routing completes in the background.
        release.set()
        await _drain_background()

    h.queue_manager.add_task.assert_awaited_once()
    agent, payload = h.queue_manager.add_task.call_args[0]
    assert agent == "editor"
    assert payload["status"] == "review"
    h.dispatcher.dispatch_agent.assert_awaited_with("editor")


@pytest.mark.asyncio
async def test_task_complete_noop_move_spawns_no_routing():
    """old_status == new_status (the MCP tool already moved the task) →
    no background routing is spawned at all."""
    h = await build_harness()
    client, cls = _httpx_mock(
        {"reviewer": "editor", "readable_id": "WR-001.T01"},
        post_result={"old_status": "review", "new_status": "review"},
    )

    event = {
        "type": "task_complete",
        "task_id": "task-1",
        "status": "review",
        "is_review_completion": False,
    }
    with patch("httpx.AsyncClient", cls):
        await h.on_event("worker-1", event)
        await _drain_background()

    h.queue_manager.add_task.assert_not_awaited()
    client.get.assert_not_awaited()
