"""Refused consults are queued and retried, never dropped (stall hotfix A3).

Incident 2026-09-29: a backend-fired Planner verify was refused by board
admission, the daemon dropped it silently, and the program stalled. A
refused Planner or Flow consult is now parked in the host-only runtime ledger
(``pending_consults``) and retried by ``PendingConsultPump`` when the reason
clears, with a visible queued notice and a ``consult_status`` event.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers import _planner_consults
from src.pending_consults import (
    CONFIG_MISSING_MAX_AGE_SECONDS,
    MANAGER_CONSULT_MAX_AGE_SECONDS,
    NOTICE_AFTER_SECONDS,
    InMemoryPendingConsults,
    PendingConsultPump,
    consult_key,
    give_up_reason,
    pending_notice_due,
    queued_notice_text,
    retry_delay,
)
from src.runtime_state import RuntimeState
from tests.test_planner_ingest_background import _drain_background
from tests.test_review_circuit_breaker import build_harness

VERIFY = {
    "mode": "verify",
    "objective": "verify the scope",
    "workstream_id": "ws-1",
    "scope_id": "scope-1",
}
SPECIFY = {
    "mode": "specify",
    "objective": "draft the spec",
    "workstream_id": "ws-1",
    "scope_id": "",
}


def _park(store, key="verify:scope-1", *, mode="verify", reason="agent_busy",
          kind="planner", agent="planner", now=1000.0, payload=None):
    return store.park_consult(
        key, kind=kind, agent=agent, mode=mode, command="consult_planner",
        payload=payload or dict(VERIFY), reason=reason, now=now,
    )


@pytest.fixture(autouse=True)
def _clean_consults():
    _planner_consults.clear()
    yield
    _planner_consults.clear()


# ---------------------------------------------------------------------------
# Store and retry policy
# ---------------------------------------------------------------------------


def test_consult_keys_identify_one_consult_each():
    assert consult_key("planner", mode="verify", scope_id="s1") == "verify:s1"
    assert consult_key(
        "planner", mode="specify", workstream_id="w1",
    ) == "planner:specify:w1:-"
    assert consult_key("flow", request_id="r1") == "flow:r1"
    # One artifact per key: what is asked does not change a specify key ...
    assert consult_key(
        "planner", mode="specify", workstream_id="w1", objective="anything",
    ) == "planner:specify:w1:-"


def test_research_questions_get_a_key_each():
    """... but research questions are independent: each has its own key,
    and the same question re-sent (case, spacing) keeps it."""

    def key(objective, scope_id=""):
        return consult_key(
            "planner", mode="research", workstream_id="w1", scope_id=scope_id,
            objective=objective,
        )

    assert key("compare vendors") != key("GDPR rules")
    assert key("compare vendors") == key("  Compare   VENDORS ")
    assert key("compare vendors").startswith("planner:research:w1:-:")
    assert key("compare vendors", "s1").startswith("planner:research:w1:s1:")
    assert len(key("compare vendors")) < 200  # the backend stores 200 chars


def test_runtime_ledger_keeps_a_parked_consult_across_restart(tmp_path):
    path = tmp_path / "runtime.sqlite"
    first = RuntimeState(path, "office")
    row = _park(first)
    assert row["attempts"] == 1 and row["reason_changed"]

    reopened = RuntimeState(path, "office")
    kept = reopened.get_pending_consult("verify:scope-1")
    assert kept["payload"]["scope_id"] == "scope-1"
    assert kept["last_reason"] == "agent_busy"
    # Another office never sees it.
    assert RuntimeState(path, "other").list_pending_consults() == []


def test_backoff_grows_and_launch_failures_reset_on_other_reasons(tmp_path):
    store = RuntimeState(tmp_path / "runtime.sqlite", "office")
    first = _park(store, reason="launch_failed", now=1000.0)
    second = _park(store, reason="launch_failed", now=1010.0)
    assert second["attempts"] == 2 and second["launch_failures"] == 2
    assert second["next_retry_at"] == 1010.0 + retry_delay(2)
    assert first["first_refused_at"] == second["first_refused_at"] == 1000.0
    third = _park(store, reason="resources", now=1020.0)
    assert third["launch_failures"] == 0 and third["reason_changed"]
    assert retry_delay(50) == 300.0


@pytest.mark.parametrize("store_kind", ["runtime", "memory"])
def test_a_duplicate_keeps_a_launch_failures_backoff(tmp_path, store_kind):
    """A duplicate (a sweeper re-fire, a release nudge) takes the newest
    payload but never skips a launch failure's backoff."""
    store = (
        RuntimeState(tmp_path / "runtime.sqlite", "office")
        if store_kind == "runtime"
        else InMemoryPendingConsults()
    )
    row = _park(store, reason="launch_failed", now=1000.0)
    assert store.absorb_pending_consult(
        "verify:scope-1", {**VERIFY, "objective": "newer"}, now=1001.0,
    )
    kept = store.get_pending_consult("verify:scope-1")
    assert kept["payload"]["objective"] == "newer"
    assert kept["next_retry_at"] == row["next_retry_at"] > 1001.0


@pytest.mark.parametrize("store_kind", ["runtime", "memory"])
def test_a_replacing_request_starts_its_own_wait(tmp_path, store_kind):
    """A request that asks for something else than the queued one it
    replaces is a new consult: its wait and its notices start now. What the
    row knows about the office stays: the retry count, and a launch
    failure's count and backoff."""
    store = (
        RuntimeState(tmp_path / "runtime.sqlite", "office")
        if store_kind == "runtime"
        else InMemoryPendingConsults()
    )
    key = "planner:specify:ws-1:-"
    _park(store, key, mode="specify", payload=dict(SPECIFY),
          reason="launch_failed", now=1000.0)
    row = _park(store, key, mode="specify", payload=dict(SPECIFY),
                reason="launch_failed", now=1010.0)
    store.note_pending_consult_notice(key, now=1070.0)
    waiting = "planner:scope_plan:ws-2:scope-5"
    _park(store, waiting, mode="scope_plan", now=2000.0)
    assert [r["key"] for r in store.list_pending_consults()] == [key, waiting]
    newer = {**SPECIFY, "objective": "add SSO requirements"}

    assert store.absorb_pending_consult(key, newer, restart_wait=True, now=5000.0)
    # It queues behind what was already waiting, like any new request.
    assert [r["key"] for r in store.list_pending_consults()] == [waiting, key]
    kept = store.get_pending_consult(key)
    assert kept["payload"]["objective"] == "add SSO requirements"
    assert kept["first_refused_at"] == kept["reason_since"] == 5000.0
    assert kept["notices_sent"] == 0 and kept["last_notice_at"] == 0.0
    assert kept["attempts"] == 2 and kept["launch_failures"] == 2
    assert kept["next_retry_at"] == row["next_retry_at"]
    assert kept["last_reason"] == "launch_failed"

    # The same request sent again is the same consult: its wait goes on.
    store.note_pending_consult_notice(key, now=5060.0)
    assert store.absorb_pending_consult(key, dict(newer), now=6000.0)
    again = store.get_pending_consult(key)
    assert again["first_refused_at"] == again["reason_since"] == 5000.0
    assert again["notices_sent"] == 1 and again["last_notice_at"] == 5060.0
    # A retry trigger without a payload replaces nothing and restarts nothing.
    assert store.absorb_pending_consult(key, restart_wait=True, now=7000.0)
    assert store.get_pending_consult(key)["first_refused_at"] == 5000.0


def test_a_duplicate_request_takes_the_newest_payload_and_retries_now(tmp_path):
    store = RuntimeState(tmp_path / "runtime.sqlite", "office")
    _park(store, now=1000.0)
    assert store.absorb_pending_consult(
        "verify:scope-1", {**VERIFY, "objective": "newer"}, now=1005.0,
    )
    row = store.get_pending_consult("verify:scope-1")
    assert row["payload"]["objective"] == "newer"
    assert row["next_retry_at"] == 1005.0
    assert not store.absorb_pending_consult("verify:missing")


@pytest.mark.parametrize("store_kind", ["runtime", "memory"])
def test_a_merged_request_never_loses_the_managers_trigger(tmp_path, store_kind):
    """Several requests are merged into one queued consult. If any of them
    was the Manager's, the consult stays the Manager's."""
    store = (
        RuntimeState(tmp_path / "runtime.sqlite", "office")
        if store_kind == "runtime"
        else InMemoryPendingConsults()
    )
    _park(store, payload={**VERIFY, "trigger": "manager"}, now=1000.0)
    assert store.absorb_pending_consult(
        "verify:scope-1", {**VERIFY, "trigger": "release", "objective": "newer"},
        now=1001.0,
    )
    kept = store.get_pending_consult("verify:scope-1")["payload"]
    assert kept["trigger"] == "manager" and kept["objective"] == "newer"
    again = _park(store, payload={**VERIFY, "trigger": "sweeper"}, now=1002.0)
    assert again["payload"]["trigger"] == "manager"
    assert store.get_pending_consult("verify:scope-1")["payload"]["trigger"] == (
        "manager"
    )
    # The other way round, the Manager's request takes a backend fire over.
    backend = {**VERIFY, "scope_id": "scope-2", "trigger": "auto"}
    _park(store, "verify:scope-2", payload=backend, now=1000.0)
    store.absorb_pending_consult("verify:scope-2", {**backend, "trigger": "manager"})
    assert store.get_pending_consult("verify:scope-2")["payload"]["trigger"] == (
        "manager"
    )
    # A backend fire merged into a backend fire stays a backend fire.
    _park(store, "verify:scope-3", payload=backend, now=1000.0)
    store.absorb_pending_consult("verify:scope-3", {**backend, "trigger": "sweeper"})
    assert store.get_pending_consult("verify:scope-3")["payload"]["trigger"] == (
        "sweeper"
    )


def test_verify_consults_are_retried_first():
    store = InMemoryPendingConsults()
    _park(store, "flow:r1", kind="flow", agent="flow-architect", mode="design",
          now=1.0)
    _park(store, "planner:specify:ws-1:-", mode="specify", now=2.0)
    _park(store, now=3.0)
    assert [r["key"] for r in store.list_pending_consults()] == [
        "verify:scope-1", "planner:specify:ws-1:-", "flow:r1",
    ]


async def test_drain_retries_only_due_rows_unless_woken():
    store = InMemoryPendingConsults()
    _park(store, now=1000.0)
    retried: list[str] = []

    async def retry(row):
        retried.append(row["key"])

    pump = PendingConsultPump(store, retry, clock=lambda: 1001.0)
    await pump.drain()
    assert retried == []
    await pump.drain(force=True)
    assert retried == ["verify:scope-1"]


async def test_one_consult_starts_per_agent_per_pass():
    store = InMemoryPendingConsults()
    _park(store, now=1.0)
    _park(store, "planner:specify:ws-1:-", mode="specify", now=2.0)
    started: list[str] = []

    async def retry(row):
        started.append(row["key"])
        store.drop_pending_consult(row["key"])
        return True

    await PendingConsultPump(store, retry).drain(force=True)
    assert started == ["verify:scope-1"]
    assert store.get_pending_consult("planner:specify:ws-1:-") is not None


async def test_a_row_that_left_without_a_launch_does_not_hold_its_agent():
    """A row dropped or given up started nothing: the agent's next row is
    still retried in the same pass."""
    store = InMemoryPendingConsults()
    _park(store, now=1.0)
    _park(store, "planner:specify:ws-1:-", mode="specify", now=2.0)
    retried: list[str] = []

    async def retry(row):
        retried.append(row["key"])
        store.drop_pending_consult(row["key"])
        return row["key"] != "verify:scope-1"

    await PendingConsultPump(store, retry).drain(force=True)
    assert retried == ["verify:scope-1", "planner:specify:ws-1:-"]


async def test_the_pump_retries_what_each_row_holds_now():
    """A pass can take minutes: a payload replaced, or a row removed, while
    an earlier row launches is retried as it is now, not as it was listed."""
    store = InMemoryPendingConsults()
    _park(store, now=1.0)
    _park(store, "planner:specify:ws-1:-", mode="specify",
          payload=dict(SPECIFY), now=2.0)
    _park(store, "flow:r1", kind="flow", agent="flow-architect", mode="design",
          payload={"request_id": "r1"}, now=3.0)
    seen: list[tuple] = []

    async def retry(row):
        seen.append((row["key"], row["payload"].get("objective")))
        if row["key"] == "verify:scope-1":
            store.absorb_pending_consult(
                "planner:specify:ws-1:-", {**SPECIFY, "objective": "newer"},
            )
            store.drop_pending_consult("flow:r1")
        return False

    await PendingConsultPump(store, retry).drain(force=True)
    assert seen == [
        ("verify:scope-1", "verify the scope"),
        ("planner:specify:ws-1:-", "newer"),
    ]


def test_give_up_rules():
    now = 10_000.0
    row = {"kind": "planner", "mode": "specify", "agent": "planner",
           "first_refused_at": now, "last_reason": "agent_busy",
           "launch_failures": 0}
    assert give_up_reason(row, now) is None
    assert "three times" in give_up_reason({**row, "launch_failures": 3}, now)
    aged = {**row, "first_refused_at": now - MANAGER_CONSULT_MAX_AGE_SECONDS}
    assert "could not start for 6 h" in give_up_reason(aged, now)
    # A verify is never given up by age: its scope waits on it.
    assert give_up_reason({**aged, "mode": "verify"}, now) is None
    # A missing configuration counts from its own first refusal ...
    missing = {**row, "last_reason": "config_missing",
               "reason_since": now - CONFIG_MISSING_MAX_AGE_SECONDS}
    assert "configuration is missing" in give_up_reason(missing, now)
    # ... not from the consult's first refusal of any kind.
    queued_long = {**row, "last_reason": "config_missing",
                   "first_refused_at": now - 3 * CONFIG_MISSING_MAX_AGE_SECONDS,
                   "reason_since": now - 1}
    assert give_up_reason(queued_long, now) is None


@pytest.mark.parametrize("store_kind", ["runtime", "memory"])
def test_a_reason_keeps_its_start_until_it_changes(tmp_path, store_kind):
    store = (
        RuntimeState(tmp_path / "runtime.sqlite", "office")
        if store_kind == "runtime"
        else InMemoryPendingConsults()
    )
    first = _park(store, reason="agent_busy", now=1000.0)
    again = _park(store, reason="agent_busy", now=1700.0)
    assert first["reason_since"] == again["reason_since"] == 1000.0
    assert first["reason_changed"] and not again["reason_changed"]
    changed = _park(store, reason="config_missing", now=1800.0)
    assert changed["reason_changed"] and changed["reason_since"] == 1800.0
    kept = store.get_pending_consult("verify:scope-1")
    assert kept["reason_since"] == 1800.0 and kept["first_refused_at"] == 1000.0
    assert "reason_changed" not in kept


def test_a_ledger_from_before_reason_since_counts_from_the_first_refusal(tmp_path):
    """The column is added to a ledger the stall hotfix created; its rows
    count their reason from the first refusal, the only start recorded."""
    import sqlite3

    path = tmp_path / "runtime.sqlite"
    RuntimeState(path, "office")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE pending_consults")
        connection.execute(
            """
            CREATE TABLE pending_consults (
                office_id TEXT NOT NULL, key TEXT NOT NULL,
                kind TEXT NOT NULL, agent TEXT NOT NULL,
                mode TEXT NOT NULL, command TEXT NOT NULL,
                payload TEXT NOT NULL, first_refused_at REAL NOT NULL,
                attempts INTEGER NOT NULL, next_retry_at REAL NOT NULL,
                last_reason TEXT NOT NULL, last_detail TEXT NOT NULL,
                launch_failures INTEGER NOT NULL DEFAULT 0,
                notices_sent INTEGER NOT NULL DEFAULT 0,
                last_notice_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (office_id, key)
            )
            """
        )
        connection.execute(
            "INSERT INTO pending_consults VALUES "
            "('office', 'verify:scope-1', 'planner', 'planner', 'verify', "
            "'consult_planner', '{\"scope_id\": \"scope-1\"}', 500.0, 4, 900.0, "
            "'agent_busy', '', 0, 1, 560.0)"
        )

    store = RuntimeState(path, "office")
    kept = store.get_pending_consult("verify:scope-1")
    assert kept["reason_since"] == 500.0 and kept["attempts"] == 4
    again = _park(store, reason="agent_busy", now=1000.0)
    assert again["attempts"] == 5 and again["reason_since"] == 500.0
    assert store.get_pending_consult("verify:scope-1")["notices_sent"] == 1


def test_a_research_consult_queued_under_the_old_key_is_rekeyed(tmp_path):
    """Research used to share one key per workstream and scope. A row parked
    by that build must get the per-question key, or its retry would launch
    the consult, drop nothing (the keys differ) and launch it again."""
    import json
    import sqlite3

    path = tmp_path / "runtime.sqlite"
    store = RuntimeState(path, "office")
    question = {"mode": "research", "objective": "compare vendors",
                "workstream_id": "ws-1", "scope_id": ""}
    _park(store, "planner:research:ws-1:-", mode="research", payload=question,
          now=700.0)
    # Another office's row is its own office's business.
    other = RuntimeState(path, "other")
    _park(other, "planner:research:ws-1:-", mode="research", payload=question,
          now=700.0)

    reopened = RuntimeState(path, "office")
    key = consult_key("planner", mode="research", workstream_id="ws-1",
                      objective="compare vendors")
    assert [row["key"] for row in reopened.list_pending_consults()] == [key]
    row = reopened.get_pending_consult(key)
    assert row["payload"] == question and row["first_refused_at"] == 700.0
    # Rows that already carry today's key are left alone on the next start.
    assert [r["key"] for r in RuntimeState(path, "office").list_pending_consults()] == [key]
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT office_id, key, payload FROM pending_consults ORDER BY office_id"
        ).fetchall()
    assert [(office, row_key) for office, row_key, _ in stored] == [
        ("office", key), ("other", "planner:research:ws-1:-"),
    ]
    assert json.loads(stored[0][2]) == question


def test_queued_notices_follow_the_schedule():
    row = {"first_refused_at": 0.0, "notices_sent": 0}
    assert not pending_notice_due(row, NOTICE_AFTER_SECONDS[0] - 1)
    assert pending_notice_due(row, NOTICE_AFTER_SECONDS[0])
    assert not pending_notice_due(
        {**row, "notices_sent": len(NOTICE_AFTER_SECONDS)}, 10**9,
    )
    text = queued_notice_text("verify", "resources")
    assert text.startswith("Planner verification is queued: waiting for")
    assert "no action is needed" in text


# ---------------------------------------------------------------------------
# Handler: consult_planner
# ---------------------------------------------------------------------------


def _handler(h, name):
    return {c.args[0]: c.args[1] for c in h.router.on.call_args_list}[name]


def _statuses(h) -> list[dict]:
    return [
        c.args[0] for c in h.router.publish_event.call_args_list
        if c.args and c.args[0].get("type") == "consult_status"
    ]


def _arm(h):
    h.supervisor.spawn_worker = AsyncMock(return_value=True)
    h.config_store.get_agent.return_value = {"name": "planner"}
    h.config_store.get_workstream.return_value = {}


async def test_busy_planner_queues_a_backend_verify_instead_of_dropping_it():
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    await _handler(h, "consult_planner")({**VERIFY, "trigger": "auto"})

    h.supervisor.spawn_worker.assert_not_awaited()
    h.mgr.ingest_planner_result.assert_not_called()
    store = h.supervisor.pending_consult_pump.store
    assert store.get_pending_consult("verify:scope-1")["last_reason"] == "agent_busy"
    status = _statuses(h)[-1]
    assert status["state"] == "queued" and status["reason"] == "agent_busy"
    assert status["key"] == "verify:scope-1" and status["trigger"] == "auto"

    # The Planner frees: the pump starts the parked verify.
    h.supervisor.is_agent_busy.return_value = False
    _client, cls = _scope_state_client("verifying")
    with patch("httpx.AsyncClient", cls):
        await h.supervisor.pending_consult_pump.drain(force=True)
    h.supervisor.spawn_worker.assert_awaited_once()
    assert store.get_pending_consult("verify:scope-1") is None
    assert _statuses(h)[-1]["state"] == "started"


def _scope_state_client(state: str):
    client = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"state": state}
    client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return client, MagicMock(return_value=cm)


async def test_a_parked_verify_is_dropped_once_its_scope_left_verifying():
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    await _handler(h, "consult_planner")(dict(VERIFY))
    h.supervisor.is_agent_busy.return_value = False

    _client, cls = _scope_state_client("done")
    with patch("httpx.AsyncClient", cls):
        await h.supervisor.pending_consult_pump.drain(force=True)

    h.supervisor.spawn_worker.assert_not_awaited()
    assert h.supervisor.pending_consult_pump.store.list_pending_consults() == []
    dropped = _statuses(h)[-1]
    assert dropped["state"] == "dropped"
    assert dropped["reason"] == "scope_left_verifying"


async def test_a_second_planner_consult_waits_behind_the_parked_one():
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    await _handler(h, "consult_planner")(dict(VERIFY))
    h.supervisor.is_agent_busy.return_value = False
    await _handler(h, "consult_planner")(dict(SPECIFY))

    h.supervisor.spawn_worker.assert_not_awaited()
    row = h.supervisor.pending_consult_pump.store.get_pending_consult(
        "planner:specify:ws-1:-",
    )
    assert row["last_reason"] == "queued_behind"


async def test_a_duplicate_consult_request_is_absorbed():
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler(dict(VERIFY))
    await handler({**VERIFY, "objective": "again"})
    rows = h.supervisor.pending_consult_pump.store.list_pending_consults()
    assert len(rows) == 1 and rows[0]["payload"]["objective"] == "again"
    assert rows[0]["attempts"] == 1
    # A scope has one verification, whatever the fire's wording: no note.
    h.mgr.ingest_planner_result.assert_not_called()


async def test_the_consult_is_stashed_before_its_spawn_returns():
    """A5: a session that dies during spawn finds its marker, so the error
    path can clean up instead of treating it as an unknown agent."""
    h = await build_harness()
    _arm(h)
    seen: list[dict] = []

    async def spawn(name, config, task):
        seen.append(dict(_planner_consults.get(task["task_id"]) or {}))
        return True

    h.supervisor.spawn_worker = AsyncMock(side_effect=spawn)
    await _handler(h, "consult_planner")(dict(SPECIFY))
    assert seen and seen[0].get("_spawning") is True
    (marker,) = _planner_consults.values()
    assert "_spawning" not in marker


async def test_launch_failures_of_a_manager_verify_poke_the_manager():
    """A4: a verify the Manager asked for reports its failure; one the
    backend fired stays silent (the backend sweeper owns it)."""
    for trigger, pokes in (("manager", True), ("auto", False)):
        h = await build_harness()
        _arm(h)
        h.supervisor.spawn_worker = AsyncMock(return_value=False)
        handler = _handler(h, "consult_planner")
        msg = {**VERIFY, "trigger": trigger}
        await handler(dict(msg))
        for _ in range(2):
            await handler({**msg, "_pending_retry": True})
        assert h.mgr.ingest_planner_result.called is pokes
        assert _statuses(h)[-1]["state"] == "failed"
        _planner_consults.clear()



# ---------------------------------------------------------------------------
# Review fixes (stall hotfix review, 2026-09-30)
# ---------------------------------------------------------------------------


MATERIALIZE = {
    "mode": "materialize",
    "objective": "author the scope's tasks",
    "workstream_id": "ws-1",
    "scope_id": "scope-9",
    "trigger": "manager",
}


@pytest.mark.parametrize("planner_busy", [False, True])
async def test_a_parked_consult_in_stall_cooldown_is_refused_once_and_dropped(
    planner_busy,
):
    """A consult parked under the key of one that then hit the stall cap is
    refused once and dropped: it is not re-run on every tick (one Manager
    poke each) and never spawned when the cooldown lapses. That holds while
    the Planner is still busy too: the cooldown is checked before the busy
    check, so the row is not parked again behind the Planner to start, late,
    once the cooldown has lapsed."""
    import time as _time

    from src.handlers import _planner_cap_cooldown

    _planner_cap_cooldown.clear()
    try:
        h = await build_harness()
        _arm(h)
        h.supervisor.is_agent_busy.return_value = True
        await _handler(h, "consult_planner")(dict(MATERIALIZE))
        pump = h.supervisor.pending_consult_pump
        key = "planner:materialize:ws-1:scope-9"
        assert pump.store.get_pending_consult(key) is not None

        _planner_cap_cooldown[("ws-1", "scope-9", "materialize")] = _time.monotonic()
        h.supervisor.is_agent_busy.return_value = planner_busy
        for tick in range(1, 5):
            pump._clock = lambda tick=tick: _time.time() + 31 * tick
            await pump.drain()
        await pump.drain(force=True)

        assert h.mgr.ingest_planner_result.call_count == 1
        assert pump.store.get_pending_consult(key) is None
        h.supervisor.spawn_worker.assert_not_awaited()
        failed = _statuses(h)[-1]
        assert failed["state"] == "failed" and failed["reason"] == "stall_cooldown"
    finally:
        _planner_cap_cooldown.clear()


async def test_a_reconsult_in_stall_cooldown_is_refused_with_its_queued_row():
    """A request for a consult in its stall cooldown is refused at once,
    while the Planner is busy, and so is the row queued for that consult.
    It used to be merged into the row and reported queued, then refused or
    started only when the Planner freed."""
    import time as _time

    from src.handlers import _planner_cap_cooldown

    _planner_cap_cooldown.clear()
    try:
        h = await build_harness()
        _arm(h)
        h.supervisor.is_agent_busy.return_value = True
        handler = _handler(h, "consult_planner")
        await handler(dict(MATERIALIZE))
        pump = h.supervisor.pending_consult_pump
        key = "planner:materialize:ws-1:scope-9"
        assert pump.store.get_pending_consult(key) is not None
        _planner_cap_cooldown[("ws-1", "scope-9", "materialize")] = _time.monotonic()
        reported = len(_statuses(h))

        await handler(dict(MATERIALIZE))

        assert pump.store.list_pending_consults() == []
        (call,) = h.mgr.ingest_planner_result.call_args_list
        assert "STALLED repeatedly" in call.args[0]["planner_error"]
        assert [
            (status["state"], status["reason"]) for status in _statuses(h)[reported:]
        ] == [("failed", "stall_cooldown")]
        h.supervisor.spawn_worker.assert_not_awaited()
    finally:
        _planner_cap_cooldown.clear()


async def test_a_reconsult_of_a_running_consult_starts_no_second_session():
    """The same consult (mode, workstream, scope) already running: an
    identical request is absorbed, a different one refused with a note —
    neither queues a second full Planner session."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler(dict(SPECIFY))
    assert h.supervisor.spawn_worker.await_count == 1
    h.supervisor.is_agent_busy.return_value = True
    # The Planner is running THIS session (liveness is per session).
    h.supervisor.get_agent_current_task.return_value = next(iter(_planner_consults))

    await handler(dict(SPECIFY))
    h.mgr.ingest_planner_result.assert_not_called()

    await handler({**SPECIFY, "objective": "draft it for a different audience"})
    (call,) = h.mgr.ingest_planner_result.call_args_list
    assert call.args[0]["planner_duplicate"] is True
    assert call.args[0]["running_objective"] == SPECIFY["objective"]

    pump = h.supervisor.pending_consult_pump
    assert pump.store.list_pending_consults() == []
    h.supervisor.is_agent_busy.return_value = False
    await pump.drain(force=True)
    assert h.supervisor.spawn_worker.await_count == 1


async def test_a_leftover_stash_does_not_swallow_a_new_consult():
    """A stash left for the same key (an office re-init) while the Planner
    runs a DIFFERENT session is not "already running": the new request is
    queued for the busy Planner, never absorbed and lost."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler(dict(SPECIFY))
    assert h.supervisor.spawn_worker.await_count == 1
    h.supervisor.is_agent_busy.return_value = True
    h.supervisor.get_agent_current_task.return_value = "planner-someotherconsult"

    await handler(dict(SPECIFY))
    h.mgr.ingest_planner_result.assert_not_called()
    pump = h.supervisor.pending_consult_pump
    assert pump.store.get_pending_consult("planner:specify:ws-1:-") is not None


async def test_a_row_under_a_running_consults_key_is_taken_out_of_the_queue():
    """A guard, not a known path: nothing should park a row under the key
    of a consult that is running. If one is ever left there it would start
    the same consult a second time once the Planner is free, so a request
    that meets the running consult removes it."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler(dict(SPECIFY))
    (sid,) = _planner_consults
    h.supervisor.is_agent_busy.return_value = True
    h.supervisor.get_agent_current_task.return_value = sid
    pump = h.supervisor.pending_consult_pump
    _park(pump.store, "planner:specify:ws-1:-", mode="specify",
          payload=dict(SPECIFY), now=time.time())

    await handler(dict(SPECIFY))

    assert pump.store.list_pending_consults() == []
    h.mgr.ingest_planner_result.assert_not_called()
    h.supervisor.is_agent_busy.return_value = False
    await pump.drain(force=True)
    assert h.supervisor.spawn_worker.await_count == 1


async def test_a_consult_whose_twin_finished_starts_normally():
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler(dict(SPECIFY))
    _planner_consults.clear()  # the first session completed
    await handler(dict(SPECIFY))
    assert h.supervisor.spawn_worker.await_count == 2


async def test_every_session_of_a_key_reports_its_start():
    """The next session of the same key (a new verifying episode, a
    re-fire) sends its own ``started``; only repeated ``queued`` reports
    with the same reason are suppressed."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    _planner_consults.clear()
    await handler({**VERIFY, "trigger": "auto"})
    assert [s["state"] for s in _statuses(h)] == ["started", "started"]


async def test_an_absorbed_duplicate_reports_the_verify_queued_again():
    """A backend re-fire absorbed into a parked verify gets a fresh
    ``queued`` report, so the backend sees its latest fire explained."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    await handler({**VERIFY, "trigger": "sweeper"})
    states = [(s["state"], s["reason"]) for s in _statuses(h)]
    assert states == [("queued", "agent_busy"), ("queued", "agent_busy")]
    assert _statuses(h)[-1]["trigger"] == "sweeper"


async def test_a_verify_request_for_a_running_verify_reports_it_running():
    """The scope's verify is running: another request (a sweeper, release or
    reconnect fire) starts no second session, and the running one is
    reported ``started`` again, so the backend does not count the fire as one
    nobody explained."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    (sid,) = _planner_consults
    h.supervisor.is_agent_busy.return_value = True
    h.supervisor.get_agent_current_task.return_value = sid

    await handler({**VERIFY, "trigger": "sweeper"})

    assert h.supervisor.spawn_worker.await_count == 1
    assert [(s["state"], s["detail"]) for s in _statuses(h)] == [
        ("started", ""),
        ("started", "a verify for this scope is already running"),
    ]
    assert _statuses(h)[-1]["key"] == "verify:scope-1"


async def test_a_verify_still_launching_reports_only_its_own_start():
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    _planner_consults["planner-launching"] = {**VERIFY, "_spawning": True}

    await handler({**VERIFY, "trigger": "sweeper"})

    h.supervisor.spawn_worker.assert_not_awaited()
    assert _statuses(h) == []


async def test_a_woken_pump_keeps_a_launch_failures_backoff():
    """A launch failure wakes the pump through its own error event; the
    forced drain must not retry it before its backoff, or three launches in
    a row would fail a consult during a short container restart."""
    import time as _time

    h = await build_harness()
    _arm(h)
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    await _handler(h, "consult_planner")({**SPECIFY, "trigger": "manager"})
    pump = h.supervisor.pending_consult_pump
    row = pump.store.get_pending_consult("planner:specify:ws-1:-")
    assert row["last_reason"] == "launch_failed"

    await pump.drain(force=True)
    assert h.supervisor.spawn_worker.await_count == 1

    pump._clock = lambda: _time.time() + 31
    await pump.drain()
    assert h.supervisor.spawn_worker.await_count == 2


async def test_a_cancelled_spawn_leaves_no_consult_behind():
    import asyncio

    h = await build_harness()
    _arm(h)
    h.supervisor.spawn_worker = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _handler(h, "consult_planner")(dict(VERIFY))
    assert _planner_consults == {}


async def test_a_stale_stash_does_not_block_the_scopes_verify():
    """A stash left by an office re-init (no Planner process behind it) must
    not make every later verify of the scope look already running."""
    h = await build_harness()
    _arm(h)
    _planner_consults["planner-stale"] = {"mode": "verify", "scope_id": "scope-1"}
    h.supervisor.is_agent_busy.return_value = False
    await _handler(h, "consult_planner")({**VERIFY, "trigger": "sweeper"})
    h.supervisor.spawn_worker.assert_awaited_once()


# ---------------------------------------------------------------------------
# A consult gives up on a fresh refusal, never before a retry (review 2026-10)
# ---------------------------------------------------------------------------


def _age(store, key: str, seconds: float, *, reason_too: bool = True) -> None:
    """Make a parked row ``seconds`` older (the in-memory test store)."""
    row = store._rows[key]
    row["first_refused_at"] -= seconds
    if reason_too:
        row["reason_since"] -= seconds


async def test_a_long_queued_consult_survives_a_brief_config_gap():
    """Queued 11 minutes behind a busy Planner, then refused once because
    the agent configuration had not synced yet (a daemon restart): the sync
    wakes the pump and the consult STARTS. The 10-minute limit counts the
    missing configuration, not the whole wait, and is checked after a
    refusal, not before the retry."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    await _handler(h, "consult_planner")({**SPECIFY, "trigger": "manager"})
    pump = h.supervisor.pending_consult_pump
    key = "planner:specify:ws-1:-"
    _age(pump.store, key, 660)

    h.supervisor.is_agent_busy.return_value = False
    h.config_store.get_agent.return_value = None
    await pump.drain(force=True)
    assert pump.store.get_pending_consult(key)["last_reason"] == "config_missing"
    h.mgr.ingest_planner_result.assert_not_called()

    h.config_store.get_agent.return_value = {"name": "planner"}
    await pump.drain(force=True)
    h.supervisor.spawn_worker.assert_awaited_once()
    assert pump.store.get_pending_consult(key) is None
    h.mgr.ingest_planner_result.assert_not_called()
    assert _statuses(h)[-1]["state"] == "started"


async def test_a_configuration_missing_for_ten_minutes_fails_the_consult():
    h = await build_harness()
    _arm(h)
    h.config_store.get_agent.return_value = None
    await _handler(h, "consult_planner")({**SPECIFY, "trigger": "manager"})
    pump = h.supervisor.pending_consult_pump
    key = "planner:specify:ws-1:-"

    _age(pump.store, key, CONFIG_MISSING_MAX_AGE_SECONDS - 60)
    await pump.drain(force=True)
    assert pump.store.get_pending_consult(key) is not None
    h.mgr.ingest_planner_result.assert_not_called()

    _age(pump.store, key, 60)
    await pump.drain(force=True)
    assert pump.store.get_pending_consult(key) is None
    (call,) = h.mgr.ingest_planner_result.call_args_list
    assert "configuration is missing" in call.args[0]["planner_error"]
    failed = _statuses(h)[-1]
    assert failed["state"] == "failed" and failed["reason"] == "gave_up"
    h.supervisor.spawn_worker.assert_not_awaited()


async def test_a_consult_older_than_the_age_limit_still_starts_when_it_can():
    """The usage limit reset after 7 hours: the wake retries the consult and
    it starts. It is not failed for its age without being tried."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    await _handler(h, "consult_planner")({**SPECIFY, "trigger": "manager"})
    pump = h.supervisor.pending_consult_pump
    key = "planner:specify:ws-1:-"
    _age(pump.store, key, MANAGER_CONSULT_MAX_AGE_SECONDS + 3600)

    h.supervisor.is_agent_busy.return_value = False
    await pump.drain(force=True)
    h.supervisor.spawn_worker.assert_awaited_once()
    h.mgr.ingest_planner_result.assert_not_called()
    assert pump.store.get_pending_consult(key) is None


async def test_a_consult_older_than_the_age_limit_fails_on_its_next_refusal():
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    await _handler(h, "consult_planner")({**SPECIFY, "trigger": "manager"})
    pump = h.supervisor.pending_consult_pump
    key = "planner:specify:ws-1:-"
    _age(pump.store, key, MANAGER_CONSULT_MAX_AGE_SECONDS)

    await pump.drain(force=True)
    assert pump.store.get_pending_consult(key) is None
    (call,) = h.mgr.ingest_planner_result.call_args_list
    assert "could not start for 6 h" in call.args[0]["planner_error"]
    assert "Planner to finish another consult" in call.args[0]["planner_error"]
    h.supervisor.spawn_worker.assert_not_awaited()


# ---------------------------------------------------------------------------
# Two requests for one consult meeting during a launch (review 2026-10)
# ---------------------------------------------------------------------------


async def test_a_request_arriving_while_a_retry_launches_is_answered_not_lost():
    """The pump relaunches a parked consult and reads the backend before the
    spawn. A different request for the same consult landing in that read was
    merged into the parked row and deleted with it when the spawn succeeded.
    The launch is now claimed first, so the request gets the "already
    running" answer."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    first = {**SPECIFY, "trigger": "manager"}
    await handler(dict(first))
    pump = h.supervisor.pending_consult_pump
    h.supervisor.is_agent_busy.return_value = False

    second = {**first, "objective": "draft it for a different audience"}
    landed: list[bool] = []

    async def snapshot(*args, **kwargs):
        if not landed:
            landed.append(True)
            await handler(dict(second))

    with patch("src.handlers._fetch_consult_outcome_state", side_effect=snapshot):
        await pump.drain(force=True)

    h.supervisor.spawn_worker.assert_awaited_once()
    launched = h.supervisor.spawn_worker.call_args.args[2]["planner_consult"]
    assert launched["objective"] == SPECIFY["objective"]
    (call,) = h.mgr.ingest_planner_result.call_args_list
    assert call.args[0]["planner_duplicate"] is True
    assert call.args[0]["planner_consult"]["objective"] == second["objective"]
    assert call.args[0]["running_objective"] == SPECIFY["objective"]
    assert pump.store.list_pending_consults() == []


async def test_two_requests_racing_the_outcome_snapshot_start_one_session():
    """Without a parked row the same window let both requests reach the
    spawn; the second then queued a full duplicate session behind the
    first."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    landed: list[bool] = []

    async def snapshot(*args, **kwargs):
        if not landed:
            landed.append(True)
            await handler(dict(SPECIFY))

    with patch("src.handlers._fetch_consult_outcome_state", side_effect=snapshot):
        await handler(dict(SPECIFY))

    h.supervisor.spawn_worker.assert_awaited_once()
    h.mgr.ingest_planner_result.assert_not_called()
    assert h.supervisor.pending_consult_pump.store.list_pending_consults() == []
    assert len(_planner_consults) == 1


async def test_a_duplicate_during_a_failing_launch_keeps_the_rows_history():
    """The row a launch came from stays that launch's own until it succeeds:
    a duplicate request must not delete it, or a failed launch would restart
    the three-strikes count and the backoff from zero."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    msg = {**SPECIFY, "trigger": "manager"}
    await handler(dict(msg))
    pump = h.supervisor.pending_consult_pump
    key = "planner:specify:ws-1:-"
    assert pump.store.get_pending_consult(key)["launch_failures"] == 1

    async def failing_spawn(name, config, task):
        await handler(dict(msg))
        return False

    h.supervisor.spawn_worker = AsyncMock(side_effect=failing_spawn)
    pump._clock = lambda: time.time() + 31
    await pump.drain()

    row = pump.store.get_pending_consult(key)
    assert row["attempts"] == 2 and row["launch_failures"] == 2
    h.mgr.ingest_planner_result.assert_not_called()
    assert _planner_consults == {}


async def test_a_verify_retry_launches_the_payload_merged_while_its_scope_was_read():
    """The pump reads the scope over HTTP before retrying a verify. A fire
    merged into the row during that read must be the one launched."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    h.supervisor.is_agent_busy.return_value = False
    pump = h.supervisor.pending_consult_pump

    client, cls = _scope_state_client("verifying")
    answer = client.get.return_value

    async def read_scope(*args, **kwargs):
        await handler({**VERIFY, "trigger": "manager", "objective": "verify it now"})
        return answer

    client.get = AsyncMock(side_effect=read_scope)
    with patch("httpx.AsyncClient", cls):
        await pump.drain(force=True)

    h.supervisor.spawn_worker.assert_awaited_once()
    marker = h.supervisor.spawn_worker.call_args.args[2]["planner_consult"]
    assert marker["trigger"] == "manager" and marker["objective"] == "verify it now"
    assert pump.store.get_pending_consult("verify:scope-1") is None


async def test_a_dropped_verify_does_not_delay_the_next_planner_consult():
    """A verify dropped because its scope left ``verifying`` started no
    session: the consult queued behind it starts in the same pass."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler(dict(VERIFY))
    await handler({**SPECIFY, "trigger": "manager"})
    h.supervisor.is_agent_busy.return_value = False
    pump = h.supervisor.pending_consult_pump

    _client, cls = _scope_state_client("done")
    with patch("httpx.AsyncClient", cls):
        await pump.drain(force=True)

    h.supervisor.spawn_worker.assert_awaited_once()
    launched = h.supervisor.spawn_worker.call_args.args[2]["planner_consult"]
    assert launched["mode"] == "specify"
    assert pump.store.list_pending_consults() == []


# ---------------------------------------------------------------------------
# A verify the Manager asked for keeps reporting to it (review 2026-10)
# ---------------------------------------------------------------------------


_KILLED = {"type": "error", "fatal": True, "reason": "heartbeat_timeout"}


@pytest.mark.parametrize("backend_trigger", ["release", "sweeper", "reconnect"])
async def test_a_backend_fire_merged_into_the_managers_queued_verify_still_reports(
    backend_trigger,
):
    """The Manager's verify is queued; a capacity-release nudge, the sweeper
    or the reconnect re-fire is merged into it. When the consult then gives
    up, the Manager is told: the merge must not turn its verify into a
    silent backend one."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "manager"})
    await handler({**VERIFY, "trigger": backend_trigger})
    pump = h.supervisor.pending_consult_pump
    assert pump.store.get_pending_consult("verify:scope-1")["payload"]["trigger"] == (
        "manager"
    )

    h.supervisor.is_agent_busy.return_value = False
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    _client, cls = _scope_state_client("verifying")
    with patch("httpx.AsyncClient", cls):
        for attempt in range(1, 4):
            pump._clock = lambda attempt=attempt: time.time() + 400 * attempt
            await pump.drain()

    assert pump.store.get_pending_consult("verify:scope-1") is None
    (call,) = h.mgr.ingest_planner_result.call_args_list
    assert "three times" in call.args[0]["planner_error"]
    assert call.args[0]["planner_consult"]["trigger"] == "manager"
    assert _statuses(h)[-1]["trigger"] == "manager"


async def test_a_killed_verify_the_manager_asked_for_reports_to_the_manager():
    """A supervisor kill carries no marker; the record of a verify the
    Manager issued must not be read as a backend fire and kept silent."""
    for trigger, pokes in (("manager", True), ("auto", False)):
        h = await build_harness()
        _arm(h)
        await _handler(h, "consult_planner")({**VERIFY, "trigger": trigger})
        (sid,) = _planner_consults
        await h.on_event("planner", {**_KILLED, "task_id": sid})
        await _drain_background()

        assert h.mgr.ingest_planner_result.called is pokes
        if pokes:
            payload = h.mgr.ingest_planner_result.call_args.args[0]
            assert payload["planner_consult"]["mode"] == "verify"
            assert "killed" in payload["planner_error"]
        assert _planner_consults == {}


async def test_a_managers_verify_merged_into_a_running_one_hears_about_its_kill():
    """Single-flight drops the Manager's verify behind a running backend
    verify of the scope. If that session is then killed the Manager, who was
    told "engaged", must hear about it."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    (sid,) = _planner_consults
    h.supervisor.is_agent_busy.return_value = True
    h.supervisor.get_agent_current_task.return_value = sid

    await handler({**VERIFY, "trigger": "manager"})
    assert h.supervisor.spawn_worker.await_count == 1
    assert h.supervisor.pending_consult_pump.store.list_pending_consults() == []
    assert _planner_consults[sid]["trigger"] == "manager"
    # A later backend duplicate does not take it back.
    await handler({**VERIFY, "trigger": "sweeper"})
    assert _planner_consults[sid]["trigger"] == "manager"

    await h.on_event("planner", {**_KILLED, "task_id": sid})
    await _drain_background()
    h.mgr.ingest_planner_result.assert_awaited_once()
    payload = h.mgr.ingest_planner_result.call_args.args[0]
    assert payload["planner_consult"]["trigger"] == "manager"


async def test_a_backend_duplicate_behind_a_backend_verify_leaves_it_silent():
    """Only a verify the MANAGER asked for makes the merged consult the
    Manager's. A sweeper duplicate dropped behind a running backend verify
    leaves it a backend one: when that session is killed, the Manager, who
    asked for nothing, is not poked (the backend sweeper owns the verify)."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    (sid,) = _planner_consults
    h.supervisor.is_agent_busy.return_value = True
    h.supervisor.get_agent_current_task.return_value = sid

    await handler({**VERIFY, "trigger": "sweeper"})
    assert h.supervisor.spawn_worker.await_count == 1
    assert h.supervisor.pending_consult_pump.store.list_pending_consults() == []
    assert _planner_consults[sid]["trigger"] == "auto"

    await h.on_event("planner", {**_KILLED, "task_id": sid})
    await _drain_background()
    h.mgr.ingest_planner_result.assert_not_called()
    assert _planner_consults == {}


async def test_a_merged_managers_verify_is_reported_through_every_handover():
    """The whole chain: a backend verify runs, the Manager's verify is
    merged into it, the session ends without a verdict, and its re-fire
    cannot launch. Each handover (the session's record, the re-fire, the
    queued row) must keep the Manager's trigger, so the give-up reaches it."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    (sid,) = _planner_consults
    h.supervisor.is_agent_busy.return_value = True
    h.supervisor.get_agent_current_task.return_value = sid
    await handler({**VERIFY, "trigger": "manager"})

    # The session ends verdictless (the worker echoes the marker it started
    # with), and from now on no session can launch.
    h.supervisor.is_agent_busy.return_value = False
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    pump = h.supervisor.pending_consult_pump
    _client, cls = _scope_state_client("verifying")
    with patch("httpx.AsyncClient", cls):
        await h.on_event("planner", {
            "type": "task_complete", "task_id": sid, "status": "planning",
            "is_review_completion": True,
            "planner_consult": {**VERIFY, "trigger": "auto"},
        })
        await _drain_background()
        row = pump.store.get_pending_consult("verify:scope-1")
        assert row["last_reason"] == "launch_failed"
        assert row["payload"]["_verdictless_refire"] is True
        assert row["payload"]["trigger"] == "manager"
        # The verdictless session's own poke; the give-up comes on top.
        reported = h.mgr.ingest_planner_result.call_count
        for attempt in (1, 2):
            pump._clock = lambda attempt=attempt: time.time() + 400 * attempt
            await pump.drain()

    assert pump.store.get_pending_consult("verify:scope-1") is None
    (give_up,) = h.mgr.ingest_planner_result.call_args_list[reported:]
    assert "three times" in give_up.args[0]["planner_error"]
    assert give_up.args[0]["planner_consult"]["trigger"] == "manager"
    assert _statuses(h)[-1]["state"] == "failed"
    assert _statuses(h)[-1]["trigger"] == "manager"


async def test_a_managers_verify_merged_into_a_failing_launch_is_queued_as_its_own():
    """The Manager's verify lands while a backend verify of the scope is
    being launched; the launch fails. The queued retry is the Manager's."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")

    async def failing_spawn(name, config, task):
        await handler({**VERIFY, "trigger": "manager"})
        return False

    h.supervisor.spawn_worker = AsyncMock(side_effect=failing_spawn)
    await handler({**VERIFY, "trigger": "auto"})

    row = h.supervisor.pending_consult_pump.store.get_pending_consult("verify:scope-1")
    assert row["last_reason"] == "launch_failed"
    assert row["payload"]["trigger"] == "manager"
    assert _statuses(h)[-1]["trigger"] == "manager"
    assert _planner_consults == {}


async def test_a_managers_verify_during_a_refire_window_marks_the_queued_verify():
    """While the daemon prepares a re-fire, single-flight drops every verify
    of the scope. If the scope's verify is already queued, the Manager's
    request leaves its trigger on that row."""
    from src.handlers import _verify_refire_manager, _verify_refire_pending

    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    store = h.supervisor.pending_consult_pump.store
    _verify_refire_pending.add("scope-1")
    try:
        await handler({**VERIFY, "trigger": "manager"})
        row = store.get_pending_consult("verify:scope-1")
        assert row["payload"]["trigger"] == "manager"
        assert row["attempts"] == 1
        assert "scope-1" not in _verify_refire_manager
        h.supervisor.spawn_worker.assert_not_awaited()
    finally:
        _verify_refire_pending.discard("scope-1")
        _verify_refire_manager.discard("scope-1")


async def test_a_queued_verify_never_stores_the_monotonic_start_stamp():
    """``_verify_first_started`` is a monotonic clock reading that the daemon
    threads through its own verify re-fires. The queue outlives the process
    (the runtime ledger) and the reading means nothing to the next one, so a
    queued verify never stores it: not when a re-fire is parked, and not
    when one is merged into a row already queued. The attempt ordinal, which
    does survive a restart, is kept."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    store = h.supervisor.pending_consult_pump.store
    refire = {
        **VERIFY, "trigger": "auto", "_verdictless_refire": True,
        "_verify_first_started": 12345.0, "_verify_attempt": 2,
    }

    await handler(dict(refire))
    parked = store.get_pending_consult("verify:scope-1")["payload"]
    assert "_verify_first_started" not in parked
    assert parked["_verify_attempt"] == 2 and parked["_verdictless_refire"] is True

    await handler({**refire, "_verify_attempt": 3})
    merged = store.get_pending_consult("verify:scope-1")["payload"]
    assert "_verify_first_started" not in merged
    assert merged["_verify_attempt"] == 3
    h.supervisor.spawn_worker.assert_not_awaited()


async def test_a_merged_managers_verify_survives_a_daemon_restart(
    tmp_path, monkeypatch,
):
    """The same rule on the real runtime ledger, across a restart: the
    Manager's queued verify absorbs a backend re-fire, the daemon restarts,
    and the new daemon relaunches the row as the Manager's consult. (The
    other handler tests run on the in-memory queue.)"""
    ledger = tmp_path / "runtime.sqlite"
    monkeypatch.setattr(
        "src.pending_consults.InMemoryPendingConsults",
        lambda: RuntimeState(ledger, "office-1"),
    )
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "manager"})
    await handler({**VERIFY, "trigger": "sweeper", "objective": "verify it again"})
    assert isinstance(h.supervisor.pending_consult_pump.store, RuntimeState)

    # The daemon restarts: its records of running consults are gone, the
    # ledger is not.
    _planner_consults.clear()
    restarted = await build_harness()
    _arm(restarted)
    pump = restarted.supervisor.pending_consult_pump
    row = pump.store.get_pending_consult("verify:scope-1")
    assert row["payload"]["objective"] == "verify it again"
    assert row["payload"]["trigger"] == "manager"
    assert row["attempts"] == 1 and row["last_reason"] == "agent_busy"

    restarted.supervisor.spawn_worker = AsyncMock(return_value=False)
    _client, cls = _scope_state_client("verifying")
    with patch("httpx.AsyncClient", cls):
        for attempt in range(1, 4):
            pump._clock = lambda attempt=attempt: time.time() + 400 * attempt
            await pump.drain()

    assert pump.store.list_pending_consults() == []
    h.mgr.ingest_planner_result.assert_not_called()
    (call,) = restarted.mgr.ingest_planner_result.call_args_list
    assert "three times" in call.args[0]["planner_error"]
    assert call.args[0]["planner_consult"]["trigger"] == "manager"
    assert _statuses(restarted)[-1]["state"] == "failed"


# ---------------------------------------------------------------------------
# Two different requests under one key (review 2026-10)
# ---------------------------------------------------------------------------


RESEARCH = {
    "mode": "research",
    "objective": "compare vendors",
    "workstream_id": "ws-1",
    "scope_id": "",
    "trigger": "manager",
}


def _launched(h) -> dict:
    return h.supervisor.spawn_worker.call_args.args[2]["planner_consult"]


async def test_two_research_questions_queue_side_by_side_and_both_run():
    """Two unscoped research questions shared one key, so the second
    replaced the first in the queue and only one ever ran."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler(dict(RESEARCH))
    await handler({**RESEARCH, "objective": "GDPR rules for the EU launch"})
    pump = h.supervisor.pending_consult_pump
    assert [r["payload"]["objective"] for r in pump.store.list_pending_consults()] == [
        "compare vendors", "GDPR rules for the EU launch",
    ]
    h.mgr.ingest_planner_result.assert_not_called()

    h.supervisor.is_agent_busy.return_value = False
    await pump.drain(force=True)
    assert h.supervisor.spawn_worker.await_count == 1
    assert _launched(h)["objective"] == "compare vendors"
    _planner_consults.clear()  # the first session reported back
    await pump.drain(force=True)
    assert h.supervisor.spawn_worker.await_count == 2
    assert _launched(h)["objective"] == "GDPR rules for the EU launch"
    assert pump.store.list_pending_consults() == []


async def test_a_second_research_question_waits_for_the_running_one():
    """A different question is not a duplicate of the running research: it
    queues behind it. The same question again is answered by the run."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler(dict(RESEARCH))
    (sid,) = _planner_consults
    h.supervisor.is_agent_busy.return_value = True
    h.supervisor.get_agent_current_task.return_value = sid

    await handler({**RESEARCH, "objective": "GDPR rules for the EU launch"})
    await handler({**RESEARCH, "objective": "Compare  Vendors"})

    (row,) = h.supervisor.pending_consult_pump.store.list_pending_consults()
    assert row["payload"]["objective"] == "GDPR rules for the EU launch"
    assert row["last_reason"] == "agent_busy"
    h.mgr.ingest_planner_result.assert_not_called()
    assert h.supervisor.spawn_worker.await_count == 1


async def test_a_different_request_replacing_a_queued_one_tells_the_manager():
    """specify, scope_plan and materialize keep one request per key. A
    request that asks for something else replaces the queued one, and the
    Manager is told the earlier one will not run instead of waiting for
    both."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**SPECIFY, "trigger": "manager"})
    await handler({**SPECIFY, "trigger": "manager", "objective": "add SSO requirements"})

    pump = h.supervisor.pending_consult_pump
    (row,) = pump.store.list_pending_consults()
    assert row["payload"]["objective"] == "add SSO requirements"
    (call,) = h.mgr.ingest_planner_result.call_args_list
    note = call.args[0]
    assert note["planner_superseded"] == "queued"
    assert note["replaced_objective"] == SPECIFY["objective"]
    assert note["planner_consult"]["objective"] == "add SSO requirements"
    assert "planner_duplicate" not in note and "planner_error" not in note
    # The replaced request never ran.
    assert note["replaced_interrupted"] is False

    # The same request again is absorbed without another note.
    await handler({**SPECIFY, "trigger": "manager", "objective": "Add  SSO requirements"})
    assert h.mgr.ingest_planner_result.call_count == 1
    assert len(pump.store.list_pending_consults()) == 1


@pytest.mark.parametrize("rerun_flag", ["_infra_refire", "_restart_count"])
async def test_replacing_the_queued_rerun_of_an_interrupted_consult_says_so(
    rerun_flag,
):
    """The queued request can itself be the daemon's re-run of a session
    that was interrupted. A request for something else replaces it like any
    queued request, but that one did run once: the note is marked, so the
    Manager is not told that nothing changed on the board."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**SPECIFY, "trigger": "manager", rerun_flag: 1})
    pump = h.supervisor.pending_consult_pump
    (row,) = pump.store.list_pending_consults()
    assert row["payload"][rerun_flag] == 1
    h.mgr.ingest_planner_result.assert_not_called()

    newer = {**SPECIFY, "trigger": "manager", "objective": "add SSO requirements"}
    await handler(dict(newer))

    (row,) = pump.store.list_pending_consults()
    assert row["payload"]["objective"] == "add SSO requirements"
    assert rerun_flag not in row["payload"]
    (call,) = h.mgr.ingest_planner_result.call_args_list
    note = call.args[0]
    assert note["planner_superseded"] == "queued"
    assert note["replaced_objective"] == SPECIFY["objective"]
    assert note["replaced_interrupted"] is True


async def test_a_request_replacing_a_long_queued_one_gets_its_own_wait():
    """The Manager's specify had waited six hours when a request for
    something else replaced it; the Manager was told the new one "is queued
    and starts automatically". Its next refusal used to fail it as "could
    not start for 6 h", minutes later: that was the replaced request's wait.
    The new request has its own six hours and its own queued notices."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**SPECIFY, "trigger": "manager"})
    pump = h.supervisor.pending_consult_pump
    key = "planner:specify:ws-1:-"
    _age(pump.store, key, MANAGER_CONSULT_MAX_AGE_SECONDS)
    pump.store._rows[key]["notices_sent"] = len(NOTICE_AFTER_SECONDS)

    newer = {**SPECIFY, "trigger": "manager", "objective": "add SSO requirements"}
    replaced_at = time.time()
    await handler(dict(newer))
    (note,) = [call.args[0] for call in h.mgr.ingest_planner_result.call_args_list]
    assert note["planner_superseded"] == "queued"

    # The Planner is still busy: the new request is refused, and waits on.
    await pump.drain(force=True)
    row = pump.store.get_pending_consult(key)
    assert row is not None, "failed for the wait of the request it replaced"
    assert row["payload"]["objective"] == "add SSO requirements"
    assert row["first_refused_at"] >= replaced_at
    assert row["reason_since"] >= replaced_at
    assert h.mgr.ingest_planner_result.call_count == 1
    assert _statuses(h)[-1]["state"] == "queued"
    # Its queued notices follow its own wait.
    assert row["notices_sent"] == 0
    assert not pending_notice_due(row, replaced_at + NOTICE_AFTER_SECONDS[0] - 1)
    assert pending_notice_due(row, time.time() + NOTICE_AFTER_SECONDS[0])
    h.supervisor.spawn_worker.assert_not_awaited()


async def test_the_same_request_sent_again_keeps_its_wait():
    """Only a request for something else is a new consult. The same request
    sent again is absorbed into the queued one, whose six hours go on."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**SPECIFY, "trigger": "manager"})
    pump = h.supervisor.pending_consult_pump
    key = "planner:specify:ws-1:-"
    _age(pump.store, key, MANAGER_CONSULT_MAX_AGE_SECONDS)

    await handler({**SPECIFY, "trigger": "manager", "objective": "Draft  the spec"})
    h.mgr.ingest_planner_result.assert_not_called()
    await pump.drain(force=True)

    assert pump.store.get_pending_consult(key) is None
    (call,) = h.mgr.ingest_planner_result.call_args_list
    assert "could not start for 6 h" in call.args[0]["planner_error"]


async def test_a_dropped_rerun_leaves_the_newer_queued_requests_wait_alone():
    """The daemon's re-run of an earlier request is dropped for the newer
    queued one. Nothing was replaced, so the queued request's wait and
    notices are as they were."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    newer = {**SPECIFY, "trigger": "manager", "objective": "add SSO requirements"}
    await handler(dict(newer))
    pump = h.supervisor.pending_consult_pump
    key = "planner:specify:ws-1:-"
    _age(pump.store, key, 3600)
    pump.store._rows[key]["notices_sent"] = 2
    before = pump.store.get_pending_consult(key)

    await handler({**SPECIFY, "trigger": "manager", "_infra_refire": True})

    after = pump.store.get_pending_consult(key)
    assert after["payload"]["objective"] == "add SSO requirements"
    assert after["first_refused_at"] == before["first_refused_at"]
    assert after["reason_since"] == before["reason_since"]
    assert after["notices_sent"] == 2


@pytest.mark.parametrize("rerun_flag", ["_infra_refire", "_restart_count"])
async def test_a_daemon_rerun_never_replaces_a_newer_queued_request(rerun_flag):
    """The daemon re-runs an interrupted consult by itself (an infra
    re-fire, a stall restart). If the Manager queued a newer request for
    the same consult meanwhile, that one stands: the re-run is dropped and
    the Manager is told the earlier request will not run."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    newer = {**SPECIFY, "trigger": "manager", "objective": "add SSO requirements"}
    await handler(dict(newer))

    await handler({**SPECIFY, "trigger": "manager", rerun_flag: 1})

    pump = h.supervisor.pending_consult_pump
    (row,) = pump.store.list_pending_consults()
    assert row["payload"]["objective"] == "add SSO requirements"
    assert rerun_flag not in row["payload"]
    (call,) = h.mgr.ingest_planner_result.call_args_list
    assert call.args[0]["planner_superseded"] == "rerun"
    assert call.args[0]["replaced_objective"] == SPECIFY["objective"]
    assert not call.args[0].get("kept_running")

    # A re-run of the SAME request is merged into the queued one, silently.
    await handler({**newer, rerun_flag: 1})
    assert h.mgr.ingest_planner_result.call_count == 1
    (row,) = pump.store.list_pending_consults()
    assert row["payload"][rerun_flag] == 1


async def test_a_daemon_rerun_meeting_a_running_newer_request_is_dropped():
    """The re-fire of an interrupted consult used to queue behind a newer
    session of the same consult and then run after it, overwriting the
    newer result with the older request. It is dropped; the Manager is told
    when the two asked for different things."""
    h = await build_harness()
    _arm(h)
    handler = _handler(h, "consult_planner")
    await handler({**SPECIFY, "trigger": "manager", "objective": "add SSO requirements"})
    (sid,) = _planner_consults
    h.supervisor.is_agent_busy.return_value = True
    h.supervisor.get_agent_current_task.return_value = sid
    pump = h.supervisor.pending_consult_pump

    await handler({**SPECIFY, "trigger": "manager", "_infra_refire": True})
    assert pump.store.list_pending_consults() == []
    (call,) = h.mgr.ingest_planner_result.call_args_list
    assert call.args[0]["planner_superseded"] == "rerun"
    assert call.args[0]["replaced_objective"] == SPECIFY["objective"]
    assert call.args[0]["kept_running"] is True

    # The re-run of the request that IS running adds nothing: no second
    # session queued behind it, no note.
    await handler({
        **SPECIFY, "trigger": "manager", "objective": "add SSO requirements",
        "_infra_refire": True,
    })
    assert pump.store.list_pending_consults() == []
    assert h.mgr.ingest_planner_result.call_count == 1
    assert h.supervisor.spawn_worker.await_count == 1


# ---------------------------------------------------------------------------
# Queue order and noise (review 2026-10)
# ---------------------------------------------------------------------------


async def test_a_verify_does_not_wait_behind_a_queued_planning_consult():
    """Verification runs before new planning, so a queued specify does not
    hold a new verify back: it starts when the Planner is free instead of
    being parked as "waiting behind an earlier consult"."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**SPECIFY, "trigger": "manager"})
    h.supervisor.is_agent_busy.return_value = False

    await handler({**VERIFY, "trigger": "auto"})

    h.supervisor.spawn_worker.assert_awaited_once()
    assert _launched(h)["mode"] == "verify"
    store = h.supervisor.pending_consult_pump.store
    assert [row["key"] for row in store.list_pending_consults()] == [
        "planner:specify:ws-1:-",
    ]


async def test_a_verify_reports_what_it_really_waits_for():
    """With the Planner busy the verify waits for the Planner, and says
    so; a queued planning consult is not its reason."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**SPECIFY, "trigger": "manager"})
    await handler({**VERIFY, "trigger": "auto"})

    status = _statuses(h)[-1]
    assert status["key"] == "verify:scope-1"
    assert (status["state"], status["reason"]) == ("queued", "agent_busy")


async def test_verifies_queue_behind_each_other_in_order():
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    await handler({**VERIFY, "trigger": "auto"})
    h.supervisor.is_agent_busy.return_value = False

    await handler({**VERIFY, "scope_id": "scope-2", "trigger": "auto"})

    h.supervisor.spawn_worker.assert_not_awaited()
    row = h.supervisor.pending_consult_pump.store.get_pending_consult("verify:scope-2")
    assert row["last_reason"] == "queued_behind"


async def test_a_repeated_park_is_not_logged_as_a_warning(caplog):
    """Every agent exit wakes the pump and a still-blocked consult is parked
    again. Only a new wait, or a new reason, is a WARNING."""
    h = await build_harness()
    _arm(h)
    h.supervisor.is_agent_busy.return_value = True
    handler = _handler(h, "consult_planner")
    pump = h.supervisor.pending_consult_pump

    def queued_warnings() -> int:
        return sum(
            record.levelname == "WARNING" and "consult" in record.getMessage()
            and "queued (attempt" in record.getMessage()
            for record in caplog.records
        )

    with caplog.at_level("DEBUG", logger="cbcl.handlers"):
        await handler({**SPECIFY, "trigger": "manager"})
        assert queued_warnings() == 1
        for _ in range(3):
            await pump.drain(force=True)
        assert queued_warnings() == 1
        assert pump.store.get_pending_consult("planner:specify:ws-1:-")["attempts"] == 4

        h.supervisor.is_agent_busy.return_value = False
        h.config_store.get_agent.return_value = None
        await pump.drain(force=True)
        assert queued_warnings() == 2


async def test_a_missing_planner_configuration_warns_once_per_wait(caplog):
    """A consult waiting for the Planner's configuration is retried on every
    agent exit and backoff tick, for up to 10 minutes. The handler used to
    log its own WARNING on each retry, beside the queue's. The wait is one
    WARNING, the queue's, and it carries the hint to restart cbcl."""
    h = await build_harness()
    _arm(h)
    h.config_store.get_agent.return_value = None
    pump = h.supervisor.pending_consult_pump
    caplog.clear()  # the harness logs its own start-up warnings

    with caplog.at_level("DEBUG", logger="cbcl.handlers"):
        await _handler(h, "consult_planner")({**SPECIFY, "trigger": "manager"})
        for _ in range(4):
            await pump.drain(force=True)

    row = pump.store.get_pending_consult("planner:specify:ws-1:-")
    assert (row["attempts"], row["last_reason"]) == (5, "config_missing")
    h.supervisor.spawn_worker.assert_not_awaited()
    mine = [r for r in caplog.records if r.name == "cbcl.handlers"]
    (warning,) = [r.getMessage() for r in mine if r.levelname == "WARNING"]
    assert "queued (attempt 1)" in warning and "restart cbcl" in warning
    retries = [r for r in mine if "queued (attempt" in r.getMessage()]
    assert [r.levelname for r in retries] == ["WARNING"] + ["DEBUG"] * 4


async def test_a_consult_waiting_at_the_process_ceiling_warns_once(caplog):
    """A consult the supervisor refuses (the process ceiling, a usage limit)
    is retried on every agent exit and refused again while the reason
    stands. The wait is one WARNING, not one per retry; each retry is still
    traceable at DEBUG."""
    from src.orchestrator.agent_supervisor import SpawnRefusal

    h = await build_harness()
    _arm(h)
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    h.supervisor.spawn_refusal.return_value = SpawnRefusal(
        "max_agents", "20 agent processes running",
    )
    pump = h.supervisor.pending_consult_pump
    caplog.clear()  # the harness logs its own start-up warnings

    with caplog.at_level("DEBUG", logger="cbcl.handlers"):
        # ``materialize`` reads nothing from the backend before its spawn.
        await _handler(h, "consult_planner")(dict(MATERIALIZE))
        for _ in range(3):
            await pump.drain(force=True)

    assert h.supervisor.spawn_worker.await_count == 4
    mine = [r for r in caplog.records if r.name == "cbcl.handlers"]
    (warning,) = [r.getMessage() for r in mine if r.levelname == "WARNING"]
    assert "queued (attempt 1)" in warning and "process limit" in warning
    assert "20 agent processes running" in warning
    not_started = [r for r in mine if "session not started" in r.getMessage()]
    assert len(not_started) == 4
    assert {r.levelname for r in not_started} == {"DEBUG"}


async def test_a_refusal_the_queue_cannot_wait_out_is_a_warning(caplog):
    """A refusal that is not known to clear by itself fails the consult at
    once, and that is logged as a WARNING."""
    from src.orchestrator.agent_supervisor import SpawnRefusal

    h = await build_harness()
    _arm(h)
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    h.supervisor.spawn_refusal.return_value = SpawnRefusal("unheard_of", "why")

    with caplog.at_level("DEBUG", logger="cbcl.handlers"):
        await _handler(h, "consult_planner")(dict(MATERIALIZE))

    assert h.supervisor.pending_consult_pump.store.list_pending_consults() == []
    (line,) = [
        r for r in caplog.records
        if r.name == "cbcl.handlers" and "session not started" in r.getMessage()
    ]
    assert line.levelname == "WARNING" and "unheard_of (why)" in line.getMessage()
    assert _statuses(h)[-1]["state"] == "failed"


async def test_a_consult_refused_at_the_process_ceiling_is_queued_with_that_reason():
    """Consults have no reserved process slot (a known limit). At the
    ceiling a consult is not dropped: it is queued and says what it waits
    for, in its status and in the chat notice."""
    from src.orchestrator.agent_supervisor import SpawnRefusal

    h = await build_harness()
    _arm(h)
    h.supervisor.spawn_worker = AsyncMock(return_value=False)
    h.supervisor.spawn_refusal.return_value = SpawnRefusal(
        "max_agents", "20 agent processes running",
    )
    await _handler(h, "consult_planner")({**VERIFY, "trigger": "auto"})

    row = h.supervisor.pending_consult_pump.store.get_pending_consult("verify:scope-1")
    assert row["last_reason"] == "max_agents" and row["launch_failures"] == 0
    status = _statuses(h)[-1]
    assert (status["state"], status["reason"]) == ("queued", "max_agents")
    assert status["detail"] == "20 agent processes running"
    assert "process limit" in queued_notice_text("verify", "max_agents")
    h.mgr.ingest_planner_result.assert_not_called()
