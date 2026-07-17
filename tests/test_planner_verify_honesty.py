"""Tests for the post-verify honesty check + one-shot verdictless re-fire
(incident 2026-07-16).

Contract under test (``handlers._on_agent_event``, clean-completion planner
branch + ``handlers._verify_consult_verdict_recorded``):

(a)  a verify consult that exits cleanly while the scope is STILL
     ``verifying`` with a ``pending`` verification is VERDICTLESS — the
     ingest payload is stamped with ``planner_error`` so
     ``ingest_planner_result`` takes its honest verify FAILURE branch
     instead of the "has completed scope verification" success body;
(a2) the same branch re-fires the SAME verify consult exactly once — the
     re-fired consult's marker carries ``_verdictless_refire``, and a
     consult that ALREADY carries the flag is never re-fired again
     (loop guard; the backend stuck-verifying sweeper owns it from there);
(-)  a recorded verdict (scope left ``verifying``, or a FAIL past the cap
     that keeps state ``verifying`` but records ``failed``) keeps today's
     success poke, and a scope-fetch error FAILS OPEN (no failure poke, no
     re-fire) so a backend blip can't convert real successes into failures.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers import (
    _BACKGROUND_TASKS,
    _planner_cap_cooldown,
    _planner_consults,
    _planner_heartbeats,
    _verify_consult_verdict_recorded,
    _verify_refire_pending,
)
from tests.test_planner_ingest_background import _drain_background
from tests.test_review_circuit_breaker import build_harness


VERIFY_CONSULT = {
    "mode": "verify",
    "objective": "verify the auth scope",
    "workstream_id": "ws-1",
    "scope_id": "scope-1",
}


def _scope_httpx(scope_json: dict | None, *, status_code: int = 200,
                 raise_exc: Exception | None = None):
    """Return (client, AsyncClient-classmock) whose ``get`` serves a scope."""
    client = MagicMock()
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        resp = MagicMock(status_code=status_code)
        resp.json.return_value = scope_json or {}
        client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return client, MagicMock(return_value=cm)


def _verify_event(consult: dict | None = None) -> dict:
    return {
        "type": "task_complete",
        "task_id": "planner-verify1",
        "status": "planning",
        "is_review_completion": True,
        "planner_consult": dict(consult or VERIFY_CONSULT),
    }


async def _cleanup_background() -> None:
    """Cancel lingering fire-and-forget tasks (a re-fired consult spawns a
    75s planner-heartbeat) so tests don't leak tasks across cases."""
    for t in list(_BACKGROUND_TASKS):
        if not t.done():
            t.cancel()
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _clean_module_state():
    _planner_consults.clear()
    _planner_cap_cooldown.clear()
    _planner_heartbeats.clear()
    _verify_refire_pending.clear()
    yield
    _planner_consults.clear()
    _planner_cap_cooldown.clear()
    _planner_heartbeats.clear()
    _verify_refire_pending.clear()


def _arm_consult_spawn(h) -> None:
    """Let the REAL ``_handle_consult_planner`` spawn succeed."""
    h.supervisor.spawn_worker = AsyncMock(return_value=True)
    h.config_store.get_agent.return_value = {"name": "planner"}
    h.config_store.get_workstream.return_value = {}


# ---------------------------------------------------------------------------
# (a) verdictless clean exit → honest failure poke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verdictless_verify_stamps_planner_error_and_refires():
    """Scope still ``verifying`` + ``pending`` → the ingest payload carries
    ``planner_error`` (failure branch) AND the same consult is re-fired
    once with the ``_verdictless_refire`` loop-guard flag."""
    h = await build_harness()
    _arm_consult_spawn(h)
    client, cls = _scope_httpx({
        "state": "verifying",
        "execution_plan": {"verification": {"status": "pending"}},
    })

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _verify_event()), timeout=1.0
        )
        await _drain_background()

    try:
        client.get.assert_awaited_once()
        h.mgr.ingest_planner_result.assert_awaited_once()
        payload = h.mgr.ingest_planner_result.call_args[0][0]
        assert "WITHOUT recording a verdict" in payload["planner_error"]
        assert "complete_scope_verification" in payload["planner_error"]

        # (a2) one-shot re-fire of the SAME consult, flag threaded through.
        h.supervisor.spawn_worker.assert_awaited_once()
        agent_name, _cfg, task_data = h.supervisor.spawn_worker.call_args.args
        assert agent_name == "planner"
        marker = task_data["planner_consult"]
        assert marker["mode"] == "verify"
        assert marker["scope_id"] == "scope-1"
        assert marker["workstream_id"] == "ws-1"
        assert marker["_verdictless_refire"] is True
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_verdictless_refire_is_one_shot():
    """A consult whose marker ALREADY carries ``_verdictless_refire`` (the
    re-fired attempt itself ended verdictless) still gets the honest
    failure poke but is NEVER re-fired again — recovery is left to the
    backend stuck-verifying sweeper."""
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _scope_httpx({
        "state": "verifying",
        "execution_plan": {"verification": {"status": "pending"}},
    })
    consult = {**VERIFY_CONSULT, "_verdictless_refire": True}

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _verify_event(consult)), timeout=1.0
        )
        await _drain_background()

    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "WITHOUT recording a verdict" in payload["planner_error"]
    h.supervisor.spawn_worker.assert_not_awaited()  # loop guard


@pytest.mark.asyncio
async def test_verdictless_with_pending_spawns_names_the_workflow():
    """AREA-1 fix 3 (verify turn-end incident 2026-07-17): when the
    completion payload carries ``pending_spawns`` (spawn tool_use ids
    never acked by clean stream end), the honesty check's failure copy
    NAMES the turn-end trap instead of leaving a bare verdictless
    mystery."""
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _scope_httpx({
        "state": "verifying",
        "execution_plan": {"verification": {"status": "pending"}},
    })
    event = _verify_event()
    event["pending_spawns"] = 2

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", event), timeout=1.0
        )
        await _drain_background()

    try:
        payload = h.mgr.ingest_planner_result.call_args[0][0]
        assert "WITHOUT recording a verdict" in payload["planner_error"]
        assert "workflow still running" in payload["planner_error"]
        assert "2 unresolved subagent spawn(s)" in payload["planner_error"]
        assert "dies at turn end" in payload["planner_error"]
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_verdictless_without_pending_spawns_keeps_plain_copy():
    """Zero pending spawns (a background spawn may ack immediately —
    enrichment, not the gate) keeps the plain verdictless copy."""
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _scope_httpx({
        "state": "verifying",
        "execution_plan": {"verification": {"status": "pending"}},
    })
    event = _verify_event()
    event["pending_spawns"] = 0

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", event), timeout=1.0
        )
        await _drain_background()

    try:
        payload = h.mgr.ingest_planner_result.call_args[0][0]
        assert "WITHOUT recording a verdict" in payload["planner_error"]
        assert "workflow still running" not in payload["planner_error"]
    finally:
        await _cleanup_background()


# ---------------------------------------------------------------------------
# Recorded verdicts keep the success poke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorded_pass_keeps_success_poke():
    """Scope left ``verifying`` (verdict accepted → done) → no failure
    stamp, no re-fire — today's success poke goes out unchanged."""
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _scope_httpx({"state": "done", "execution_plan": {
        "verification": {"status": "passed"},
    }})

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _verify_event()), timeout=1.0
        )
        await _drain_background()

    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "planner_error" not in payload
    h.supervisor.spawn_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_recorded_fail_past_cap_keeps_success_poke():
    """A FAIL past the verify cap keeps state ``verifying`` but RECORDS
    ``failed`` — that is a real verdict, not a verdictless exit."""
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _scope_httpx({
        "state": "verifying",
        "execution_plan": {"verification": {"status": "failed"}},
    })

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _verify_event()), timeout=1.0
        )
        await _drain_background()

    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "planner_error" not in payload
    h.supervisor.spawn_worker.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fail-open + non-verify modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_fetch_error_fails_open():
    """A backend blip during the honesty check must NOT convert a real
    success into a failure poke — no stamp, no re-fire."""
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _scope_httpx(None, raise_exc=ConnectionError("down"))

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _verify_event()), timeout=1.0
        )
        await _drain_background()

    h.mgr.ingest_planner_result.assert_awaited_once()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "planner_error" not in payload
    h.supervisor.spawn_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_verify_mode_takes_the_outcome_gate_instead():
    """Non-verify consults never run the VERIFY honesty check — since
    FIX P3 they take the mode-specific OUTCOME gate instead: a clean
    roadmap completion fetches the workstream PLAN (not the scope), and
    an existing plan keeps the success poke unchanged. The full outcome-
    gate matrix lives in ``test_planner_consult_outcome_gates.py``."""
    h = await build_harness()
    client, cls = _scope_httpx({"revision": 2, "planned_scopes": []})
    event = _verify_event({
        "mode": "roadmap", "objective": "", "workstream_id": "ws-1",
        "scope_id": "",
    })

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(h.on_event("planner", event), timeout=1.0)
        await _drain_background()

    # Exactly one fetch — the plan endpoint, never the scope endpoint.
    client.get.assert_awaited_once()
    assert "/workstreams/ws-1/plan" in client.get.call_args.args[0]
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "planner_error" not in payload
    h.supervisor.spawn_worker.assert_not_called()


# ---------------------------------------------------------------------------
# _verify_consult_verdict_recorded unit cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verdict_check_without_scope_id_is_none():
    assert await _verify_consult_verdict_recorded(
        {"mode": "verify", "scope_id": ""},
        platform_url="http://x", office_id="o", security_token="",
    ) is None


@pytest.mark.asyncio
async def test_verdict_check_non_200_is_none():
    _client, cls = _scope_httpx({}, status_code=404)
    with patch("httpx.AsyncClient", cls):
        assert await _verify_consult_verdict_recorded(
            dict(VERIFY_CONSULT),
            platform_url="http://x", office_id="o", security_token="",
        ) is None


@pytest.mark.asyncio
async def test_verdict_check_null_plan_counts_as_pending():
    """A Manager-planned small scope can have ``execution_plan: null`` —
    with no verification block at all, nothing was recorded → verdictless."""
    _client, cls = _scope_httpx({"state": "verifying", "execution_plan": None})
    with patch("httpx.AsyncClient", cls):
        assert await _verify_consult_verdict_recorded(
            dict(VERIFY_CONSULT),
            platform_url="http://x", office_id="o", security_token="",
        ) is False
