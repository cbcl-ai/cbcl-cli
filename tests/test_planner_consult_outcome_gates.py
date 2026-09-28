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
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.handlers as handlers_mod
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.handlers import (
    _BACKGROUND_TASKS,
    _consult_outcome_advanced,
    _planner_cap_cooldown,
    _planner_consults,
    _research_file_outcome_state,
)
from src.orchestrator.planner_prompt import build_planner_prompt, planner_research_path
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
async def test_first_write_on_fresh_scope_keeps_success_poke(
    _no_refire_backoff,
):
    """Incident 2026-08-04 (Presale Office, FO-002.S03): a scope_plan
    consult whose spawn-time snapshot said the plan DID NOT EXIST and
    whose completion finds revision 1 wrote the FIRST revision — the
    absolute happy path for every new scope. The gate must read that as
    advanced (success poke, NO refire). Pre-fix it returned
    not-advanced (scope_plan's fetch carries no ``updated_at`` and the
    ``revision: None`` snapshot defeats the int comparison), silently
    refiring a full redundant 25-40 min ultracode consult on EVERY
    fresh scope — the fuel of the observed kill/refire loop."""
    h = await build_harness()
    _arm_consult_spawn(h)
    consult = {
        **SCOPE_PLAN_CONSULT,
        "_pre_outcome": {
            "exists": False, "revision": None, "updated_at": None,
        },
    }
    _client, cls = _httpx_get({"execution_plan": {"revision": 1}})

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(consult)), timeout=1.0,
        )
        await _drain_background()

    try:
        h.supervisor.spawn_worker.assert_not_awaited()
        payload = h.mgr.ingest_planner_result.call_args[0][0]
        assert "planner_error" not in payload
    finally:
        await _cleanup_background()


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


def test_advanced_absent_to_exists_passes():
    """Incident 2026-08-04: absent → exists IS the advance. The
    revision-bearing shape (scope_plan: ``updated_at`` always None,
    snapshot revision None) previously fell through every positive
    branch and returned False for a consult that wrote the FIRST plan
    revision on a fresh scope."""
    assert _consult_outcome_advanced(
        {"exists": False, "revision": None, "updated_at": None},
        {"exists": True, "revision": 1, "updated_at": None},
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


# ---------------------------------------------------------------------------
# (P3) research outcome gate — C3c-G9
# ---------------------------------------------------------------------------

RESEARCH_WORKSTREAM = {"name": "Market Study"}
RENAMED_WORKSTREAM = {"name": "Renamed Study"}
# Synced rows as a current backend sends them (the map needs a UUID id).
STUDY_WORKSTREAM = {
    "id": str(uuid.UUID(int=41)),
    "name": "Market Study",
    "short_code": "MS",
    "workspace_dir": "market-study",
}
RENAMED_STUDY_WORKSTREAM = {
    **STUDY_WORKSTREAM,
    "name": "Renamed Study",
    "workspace_dir": "renamed-study",
}
UNSCOPED_RESEARCH = {
    "mode": "research",
    "objective": "compare vendors",
    "workstream_id": "ws-1",
    "scope_id": "",
}
# The findings file the prompt names for consult ``planner-outcome1``; the
# spawn stores it on the consult marker as ``_research_path``.
PROMPTED_PATH = planner_research_path("planner-outcome1", RESEARCH_WORKSTREAM)
UNSCOPED_RESEARCH_MARKER = {**UNSCOPED_RESEARCH, "_research_path": PROMPTED_PATH}


def _findings_file(workspace):
    """The host path of the findings file the prompt names for this consult."""
    return workspace / PROMPTED_PATH.removeprefix("/workspace/")


async def _research_harness(workspace):
    h = await build_harness(workspace_path=str(workspace))
    _arm_consult_spawn(h)
    h.config_store.get_workstream.return_value = dict(RESEARCH_WORKSTREAM)
    return h


@pytest.mark.asyncio
async def test_unscoped_research_without_findings_file_refires_once(
    _no_refire_backoff, tmp_path,
):
    """A research consult that exits cleanly without writing its findings
    file no longer gets the success poke pointing at a missing file."""
    h = await _research_harness(tmp_path)

    await asyncio.wait_for(
        h.on_event("planner", _consult_event(UNSCOPED_RESEARCH_MARKER)),
        timeout=1.0,
    )
    await _drain_background()

    try:
        h.supervisor.spawn_worker.assert_awaited_once()
        _agent, _cfg, task_data = h.supervisor.spawn_worker.call_args.args
        assert task_data["planner_consult"]["mode"] == "research"
        assert task_data["planner_consult"]["_infra_refire"] is True
        h.mgr.ingest_planner_result.assert_not_awaited()
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_unscoped_research_missing_after_refire_gets_failure_poke(
    _no_refire_backoff, tmp_path,
):
    """With no findings anywhere, the failure poke points at the file under
    the workstream's current directory, where a rename has moved it."""
    h = await _research_harness(tmp_path)
    h.config_store.get_workstream.return_value = dict(RENAMED_WORKSTREAM)
    consult = {**UNSCOPED_RESEARCH_MARKER, "_infra_refire": True}

    await asyncio.wait_for(
        h.on_event("planner", _consult_event(consult)), timeout=1.0,
    )
    await _drain_background()

    h.supervisor.spawn_worker.assert_not_awaited()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert payload["planner_error"] == (
        "the Planner session ended WITHOUT persisting the research output "
        "(no findings file was written)"
    )
    assert payload["planner_consult"]["_research_path"] == planner_research_path(
        "planner-outcome1", RENAMED_WORKSTREAM
    )


@pytest.mark.asyncio
async def test_unscoped_research_with_findings_file_keeps_success_poke(
    _no_refire_backoff, tmp_path,
):
    findings = _findings_file(tmp_path)
    findings.parent.mkdir(parents=True)
    findings.write_text("# Findings\n\nVendor A leads on price.\n")
    h = await _research_harness(tmp_path)

    await asyncio.wait_for(
        h.on_event("planner", _consult_event(UNSCOPED_RESEARCH_MARKER)),
        timeout=1.0,
    )
    await _drain_background()

    h.supervisor.spawn_worker.assert_not_awaited()
    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "planner_error" not in payload


async def _spawn_unscoped_research(h, workstream: dict) -> dict:
    """Spawn an unscoped research consult through the real handler and
    return its task data (the prompt names ``_research_path``)."""
    h.config_store.get_workstream.return_value = dict(workstream)
    handler = {
        c.args[0]: c.args[1] for c in h.router.on.call_args_list
    }["consult_planner"]
    await handler({**UNSCOPED_RESEARCH, "workstream_id": workstream["id"]})
    _agent, _cfg, task_data = h.supervisor.spawn_worker.call_args.args
    prompted = task_data["planner_consult"]["_research_path"]
    assert prompted == planner_research_path(task_data["task_id"], workstream)
    assert f"`{prompted}`" in build_planner_prompt(task_data)
    return task_data


def _write_findings(workspace, container_path: str) -> None:
    findings = workspace / container_path.removeprefix("/workspace/")
    findings.parent.mkdir(parents=True, exist_ok=True)
    findings.write_text("# Findings\n\nVendor A leads on price.\n")


@pytest.mark.asyncio
async def test_unscoped_research_finds_findings_the_rename_moved(
    _no_refire_backoff,
    tmp_path,
):
    """A rename while the consult runs moves the workstream directory,
    findings included, in the same sync that updates the ConfigStore row.
    The gate finds them under the current directory (no re-fire, no false
    "no findings file" poke) and the success poke names that file."""
    writer = ClaudeMdWriter(str(tmp_path))
    writer.sync_workstream_directories([STUDY_WORKSTREAM])
    h = await _research_harness(tmp_path)
    task_data = await _spawn_unscoped_research(h, STUDY_WORKSTREAM)
    prompted = task_data["planner_consult"]["_research_path"]
    _write_findings(tmp_path, prompted)

    writer.sync_workstream_directories([RENAMED_STUDY_WORKSTREAM])
    h.config_store.get_workstream.return_value = dict(RENAMED_STUDY_WORKSTREAM)
    moved = planner_research_path(task_data["task_id"], RENAMED_STUDY_WORKSTREAM)
    assert not (tmp_path / prompted.removeprefix("/workspace/")).exists()
    assert (tmp_path / moved.removeprefix("/workspace/")).is_file()

    event = {
        **_consult_event(task_data["planner_consult"]),
        "task_id": task_data["task_id"],
    }
    await asyncio.wait_for(h.on_event("planner", event), timeout=1.0)
    await _drain_background()

    try:
        h.supervisor.spawn_worker.assert_awaited_once()  # no re-fire
        payload = h.mgr.ingest_planner_result.call_args[0][0]
        assert "planner_error" not in payload
        assert payload["planner_consult"]["_research_path"] == moved
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_unscoped_research_gate_ignores_another_consults_findings(
    _no_refire_backoff,
    tmp_path,
):
    """Only this consult's findings file counts: another consult's file in
    the workstream's current research folder does not satisfy the gate."""
    h = await _research_harness(tmp_path)
    h.config_store.get_workstream.return_value = dict(RENAMED_WORKSTREAM)
    _write_findings(
        tmp_path,
        planner_research_path("planner-another1", RENAMED_WORKSTREAM),
    )

    await asyncio.wait_for(
        h.on_event("planner", _consult_event(UNSCOPED_RESEARCH_MARKER)),
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
async def test_killed_research_consult_poke_follows_the_renamed_directory(
    _no_refire_backoff,
    tmp_path,
):
    """A research session the supervisor killed after a rename: the failure
    poke points at the workstream's current directory, not the stored one."""
    writer = ClaudeMdWriter(str(tmp_path))
    writer.sync_workstream_directories([STUDY_WORKSTREAM])
    h = await _research_harness(tmp_path)
    task_data = await _spawn_unscoped_research(h, STUDY_WORKSTREAM)
    writer.sync_workstream_directories([RENAMED_STUDY_WORKSTREAM])
    h.config_store.get_workstream.return_value = dict(RENAMED_STUDY_WORKSTREAM)

    event = {
        "type": "error",
        "fatal": True,
        "reason": "heartbeat_timeout",
        "task_id": task_data["task_id"],
    }
    await asyncio.wait_for(h.on_event("planner", event), timeout=1.0)
    await _drain_background()

    try:
        payload = h.mgr.ingest_planner_result.call_args[0][0]
        assert "killed" in payload["planner_error"]
        assert payload["planner_consult"]["_research_path"] == (
            planner_research_path(task_data["task_id"], RENAMED_STUDY_WORKSTREAM)
        )
    finally:
        await _cleanup_background()


@pytest.mark.asyncio
async def test_failed_spawn_poke_names_no_research_file(
    _no_refire_backoff, tmp_path, monkeypatch,
):
    """f4 item 10: no Planner ran for a consult whose session failed to
    start, so its findings file cannot exist. The failure poke points at
    the research directory, not at that file."""
    import src.orchestrator._manager_action_requests as mar

    h = await _research_harness(tmp_path)
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    handler = {
        c.args[0]: c.args[1] for c in h.router.on.call_args_list
    }["consult_planner"]

    await handler(dict(UNSCOPED_RESEARCH))

    payload = h.mgr.ingest_planner_result.call_args[0][0]
    assert "_research_path" not in payload["planner_consult"]
    assert "failed to start" in payload["planner_error"]

    monkeypatch.setattr(mar, "build_script_context_data", lambda c, k: {})
    controller = MagicMock()
    controller._config.get_workstream = MagicMock(
        return_value=dict(RESEARCH_WORKSTREAM),
    )
    controller.handle_chat_message = AsyncMock()
    await mar.ingest_planner_result(controller, payload)
    body = controller.handle_chat_message.await_args.args[0]["user_message"]
    assert "check the workstream folder's `research/` directory first" in body
    assert "/research/planner-" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("revision,advanced", [(2, False), (3, True)])
async def test_scoped_research_is_gated_on_the_plan_revision(
    _no_refire_backoff, revision, advanced,
):
    """Scoped research writes its findings into the scope's execution plan,
    so the gate compares the plan revision with the spawn-time snapshot."""
    h = await build_harness()
    _arm_consult_spawn(h)
    consult = {
        **UNSCOPED_RESEARCH,
        "scope_id": "scope-1",
        "_pre_outcome": {"exists": True, "revision": 2, "updated_at": None},
    }
    _client, cls = _httpx_get({"execution_plan": {"revision": revision}})

    with patch("httpx.AsyncClient", cls):
        await asyncio.wait_for(
            h.on_event("planner", _consult_event(consult)), timeout=1.0,
        )
        await _drain_background()

    try:
        if advanced:
            h.supervisor.spawn_worker.assert_not_awaited()
            payload = h.mgr.ingest_planner_result.call_args[0][0]
            assert "planner_error" not in payload
        else:
            h.supervisor.spawn_worker.assert_awaited_once()
            h.mgr.ingest_planner_result.assert_not_awaited()
    finally:
        await _cleanup_background()


def test_research_file_state_reads_the_prompted_path(tmp_path):
    findings = _findings_file(tmp_path)
    assert _research_file_outcome_state(tmp_path, PROMPTED_PATH) == {
        "exists": False,
        "revision": None,
        "updated_at": None,
        "path": PROMPTED_PATH,
    }

    findings.parent.mkdir(parents=True)
    findings.write_text("")
    assert _research_file_outcome_state(
        tmp_path, PROMPTED_PATH,
    )["exists"] is False  # an empty file is not findings

    findings.write_text("findings")
    assert _research_file_outcome_state(
        tmp_path, PROMPTED_PATH,
    )["exists"] is True


def test_research_file_state_checks_the_stored_and_current_paths(tmp_path):
    """Findings count at either path; ``path`` names the one holding them,
    else the current one. A write into the old directory after the rename
    stays at the stored path until the next sync merges it forward."""
    current = planner_research_path("planner-outcome1", RENAMED_WORKSTREAM)
    state = _research_file_outcome_state(tmp_path, PROMPTED_PATH, current)
    assert state["exists"] is False
    assert state["path"] == current

    _write_findings(tmp_path, PROMPTED_PATH)
    state = _research_file_outcome_state(tmp_path, PROMPTED_PATH, current)
    assert state["exists"] is True
    assert state["path"] == PROMPTED_PATH

    _findings_file(tmp_path).unlink()
    _write_findings(tmp_path, current)
    state = _research_file_outcome_state(tmp_path, PROMPTED_PATH, current)
    assert state["exists"] is True
    assert state["path"] == current


@pytest.mark.parametrize("stored", [None, "", "/tmp/elsewhere.md"])
def test_research_file_state_fails_open_without_a_prompted_path(
    tmp_path, stored,
):
    """No exact file named in the prompt (a path-unsafe consult id stores
    ``None``) or a path outside the workspace: the gate cannot judge."""
    assert _research_file_outcome_state(tmp_path, stored) is None
