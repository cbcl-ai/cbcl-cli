"""FIX P2/P3 (blink-resilience) — consult infra re-fire + outcome gates.

Contract under test (``handlers._on_agent_event`` planner branch +
``handlers._refire_consult_infra`` / ``_fetch_consult_outcome_state`` /
``_consult_outcome_advanced``):

* (P2) a NON-verify consult whose worker session died retry-exhausted on
  a transient infra class (``details.error_class`` ∈ 529/429/timeout/
  drop) is re-fired ONCE daemon-side INSTEAD of the failure poke; the
  re-fired consult's marker carries ``_infra_refire``, and a consult
  that ALREADY carries the flag falls through to the honest poke
  (loop guard — never fires twice);
* (P3) a CLEAN non-verify completion is outcome-gated: the mode's ONE
  expected write must have landed before the success poke goes out. A
  missing outcome takes the same one-shot re-fire, then (flagged) the
  honest "ended WITHOUT persisting" failure poke;
* both gates FAIL OPEN — a fetch error keeps today's success poke, so a
  backend blip can't convert real successes into failure pokes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.handlers as handlers_mod
from src.handlers import (
    _BACKGROUND_TASKS,
    _consult_outcome_advanced,
    _planner_cap_cooldown,
    _planner_consults,
)
from tests.test_planner_ingest_background import _drain_background
from tests.test_review_circuit_breaker import build_harness


# Pivot-1 T6: ``roadmap`` retired (removed from _OUTCOME_GATED_MODES; the
# backend refuses new roadmap consults) — scope_plan is the gate vehicle now.
SCOPE_PLAN_CONSULT = {
    "mode": "scope_plan",
    "objective": "plan the scope",
    "workstream_id": "ws-1",
    "scope_id": "scope-1",
}


def _consult_event(
    consult: dict,
    *,
    status: str = "planning",
    error_class: str | None = None,
) -> dict:
    event = {
        "type": "task_complete",
        "task_id": "planner-outcome1",
        "status": status,
        "is_review_completion": True,
        "planner_consult": dict(consult),
    }
    if error_class:
        event["details"] = {
            "error_class": error_class,
            "escalation_message": "retries exhausted",
        }
    return event


def _httpx_get(json_body: dict | list | None, *, status_code: int = 200,
               raise_exc: Exception | None = None):
    """(client, AsyncClient-classmock) whose ``get`` serves one shape."""
    client = MagicMock()
    if raise_exc is not None:
        client.get = AsyncMock(side_effect=raise_exc)
    else:
        resp = MagicMock(status_code=status_code)
        resp.json.return_value = json_body if json_body is not None else {}
        client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return client, MagicMock(return_value=cm)


@pytest.fixture(autouse=True)
def _clean_module_state():
    _planner_consults.clear()
    _planner_cap_cooldown.clear()
    yield
    _planner_consults.clear()
    _planner_cap_cooldown.clear()


@pytest.fixture(autouse=True)
def _no_refire_backoff(monkeypatch):
    """The infra re-fire sleeps the class backoff (180s for a 529) —
    record instead of waiting."""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        if s >= 1.0:
            slept.append(s)
            return
        await real_sleep(s)

    monkeypatch.setattr(handlers_mod.asyncio, "sleep", fake_sleep)
    return slept


def _arm_consult_spawn(h) -> None:
    h.supervisor.spawn_worker = AsyncMock(return_value=True)
    h.config_store.get_agent.return_value = {"name": "planner"}
    h.config_store.get_workstream.return_value = {}


async def _cleanup_background() -> None:
    for t in list(_BACKGROUND_TASKS):
        if not t.done():
            t.cancel()
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# (P2) infra-classed consult death → one-shot silent re-fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infra_death_refires_once_and_suppresses_failure_poke(
    _no_refire_backoff,
):
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _httpx_get({"revision": 1})

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(
                SCOPE_PLAN_CONSULT, status="blocked",
                error_class="api_overloaded",
            )),
            timeout=1.0,
        )
        await _drain_background()

    try:
        # Re-fired the SAME consult with the loop-guard flag…
        h.supervisor.spawn_worker.assert_awaited_once()
        _agent, _cfg, task_data = h.supervisor.spawn_worker.call_args.args
        marker = task_data["planner_consult"]
        assert marker["mode"] == "scope_plan"
        assert marker["workstream_id"] == "ws-1"
        assert marker["_infra_refire"] is True
        # …after the class's backoff…
        assert 180.0 in _no_refire_backoff
        # …and the failure poke was SUPPRESSED (the re-fired consult's
        # own completion poke is the next Manager contact).
        h.mgr.ingest_planner_result.assert_not_awaited()
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_infra_refire_is_one_shot(_no_refire_backoff):
    """A consult already carrying ``_infra_refire`` gets the honest
    failure poke, never a second re-fire."""
    h = await build_harness()
    _arm_consult_spawn(h)
    consult = {**SCOPE_PLAN_CONSULT, "_infra_refire": True}

    await asyncio.wait_for(
        h.on_event("planner", _consult_event(
            consult, status="blocked", error_class="api_overloaded",
        )),
        timeout=1.0,
    )
    await _drain_background()

    h.supervisor.spawn_worker.assert_not_awaited()
    h.mgr.ingest_planner_result.assert_awaited_once()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert payload["status"] == "blocked"  # real ingest takes the
    # failure branch off the status; no refire happened.


@pytest.mark.asyncio
async def test_non_infra_death_pokes_without_refire(_no_refire_backoff):
    """A non-infra escalation class (e.g. auth_failed) keeps today's
    failure poke — waiting doesn't fix credentials."""
    h = await build_harness()
    _arm_consult_spawn(h)

    await asyncio.wait_for(
        h.on_event("planner", _consult_event(
            SCOPE_PLAN_CONSULT, status="blocked", error_class="auth_failed",
        )),
        timeout=1.0,
    )
    await _drain_background()

    h.supervisor.spawn_worker.assert_not_awaited()
    h.mgr.ingest_planner_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_mode_keeps_its_own_path(_no_refire_backoff):
    """An infra-dead VERIFY consult never takes the P2 re-fire — its
    recovery is the verdict-shaped honesty check + the backend
    stuck-verifying sweeper."""
    h = await build_harness()
    _arm_consult_spawn(h)
    consult = {**SCOPE_PLAN_CONSULT, "mode": "verify", "scope_id": "scope-1"}
    _client, cls = _httpx_get({
        "state": "verifying",
        "execution_plan": {"verification": {"status": "pending"}},
    })

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(
                consult, status="blocked", error_class="api_overloaded",
            )),
            timeout=1.0,
        )
        await _drain_background()

    try:
        # The verify honesty path fired (verdictless → refire with the
        # VERIFY flag, not the infra one) and the poke went out.
        h.mgr.ingest_planner_result.assert_awaited_once()
        _agent, _cfg, task_data = h.supervisor.spawn_worker.call_args.args
        assert task_data["planner_consult"]["_verdictless_refire"] is True
        assert "_infra_refire" not in task_data["planner_consult"]
    finally:
        await _cleanup_background()


# ---------------------------------------------------------------------------
# (P3) outcome gates on clean completions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_outcome_refires_once(_no_refire_backoff):
    """Clean scope_plan exit but the scope has NO execution_plan → the
    consult ended without its expected write; one silent re-fire."""
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _httpx_get({"state": "executing"})

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(SCOPE_PLAN_CONSULT)),
            timeout=1.0,
        )
        await _drain_background()

    try:
        h.supervisor.spawn_worker.assert_awaited_once()
        _agent, _cfg, task_data = h.supervisor.spawn_worker.call_args.args
        assert task_data["planner_consult"]["_infra_refire"] is True
        h.mgr.ingest_planner_result.assert_not_awaited()
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_missing_outcome_after_refire_gets_honest_failure_poke(
    _no_refire_backoff,
):
    h = await build_harness()
    _arm_consult_spawn(h)
    consult = {**SCOPE_PLAN_CONSULT, "_infra_refire": True}
    _client, cls = _httpx_get({"state": "executing"})

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(consult)), timeout=1.0,
        )
        await _drain_background()

    h.supervisor.spawn_worker.assert_not_awaited()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "WITHOUT persisting the scope_plan output" in (
        payload["planner_error"]
    )


@pytest.mark.asyncio
async def test_outcome_present_keeps_success_poke(_no_refire_backoff):
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _httpx_get({"execution_plan": {"revision": 2}})

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(SCOPE_PLAN_CONSULT)),
            timeout=1.0,
        )
        await _drain_background()

    h.supervisor.spawn_worker.assert_not_awaited()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "planner_error" not in payload


@pytest.mark.asyncio
async def test_outcome_fetch_error_fails_open(_no_refire_backoff):
    """A backend blip during the outcome gate keeps the success poke —
    no failure stamp, no re-fire."""
    h = await build_harness()
    _arm_consult_spawn(h)
    _client, cls = _httpx_get(None, raise_exc=ConnectionError("down"))

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(SCOPE_PLAN_CONSULT)),
            timeout=1.0,
        )
        await _drain_background()

    h.supervisor.spawn_worker.assert_not_awaited()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "planner_error" not in payload


@pytest.mark.asyncio
async def test_materialize_gate_requires_a_contracted_task(
    _no_refire_backoff,
):
    """Materialize's outcome is existence-shaped: ≥1 task in the scope
    with a COMPLETE brief. A scope with only contract-less rows fails
    the gate."""
    h = await build_harness()
    _arm_consult_spawn(h)
    consult = {**SCOPE_PLAN_CONSULT, "mode": "materialize",
               "scope_id": "scope-1", "_infra_refire": True}
    _client, cls = _httpx_get([{"brief_is_complete": False}])

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(consult)), timeout=1.0,
        )
        await _drain_background()

    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "WITHOUT persisting the materialize output" in (
        payload["planner_error"]
    )


# ---------------------------------------------------------------------------
# _consult_outcome_advanced unit cases
# ---------------------------------------------------------------------------


def test_advanced_none_current_fails_open():
    assert _consult_outcome_advanced({"revision": 1}, None) is None


def test_advanced_absent_target_is_false():
    assert _consult_outcome_advanced(
        None, {"exists": False, "revision": None, "updated_at": None},
    ) is False


def test_advanced_no_snapshot_passes_on_existence():
    assert _consult_outcome_advanced(
        None, {"exists": True, "revision": 1, "updated_at": "t1"},
    ) is True


def test_advanced_revision_growth_passes():
    assert _consult_outcome_advanced(
        {"exists": True, "revision": 1, "updated_at": "t1"},
        {"exists": True, "revision": 2, "updated_at": "t1"},
    ) is True


def test_advanced_updated_at_change_passes():
    """A specify that edits a draft IN PLACE keeps its revision —
    ``updated_at`` is the load-bearing signal there."""
    assert _consult_outcome_advanced(
        {"exists": True, "revision": 1, "updated_at": "t1"},
        {"exists": True, "revision": 1, "updated_at": "t2"},
    ) is True


def test_advanced_untouched_target_is_false():
    assert _consult_outcome_advanced(
        {"exists": True, "revision": 1, "updated_at": "t1"},
        {"exists": True, "revision": 1, "updated_at": "t1"},
    ) is False


def test_advanced_existence_shaped_target_passes():
    """Materialize's fetch carries neither revision nor updated_at —
    existence alone decides."""
    assert _consult_outcome_advanced(
        {"exists": True, "revision": None, "updated_at": None},
        {"exists": True, "revision": None, "updated_at": None},
    ) is True
