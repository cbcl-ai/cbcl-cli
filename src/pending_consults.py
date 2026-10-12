"""Consults the office could not start yet: a durable queue, never a drop.

Stall incident 2026-09-29 (office Development_01): a backend-fired Planner
verify was refused four times while an unscoped board task held the office
workspace, and every refusal was dropped silently. A consult refused for a
reason that clears by itself (the Planner is busy with another consult, a
Claude usage limit, a maintenance pause, agent configuration still loading,
the process ceiling, a launch that failed once) is now PARKED here and
retried when the reason clears: on any agent exit, on quota recovery, when
configuration applies, and on a 30 s tick with exponential backoff.

Parked rows live in the host-only runtime ledger (``pending_consults``), so a
daemon restart keeps Manager-issued consults. Keys:

* ``verify:<scope_id>`` - one pending verification per scope; a backend or
  sweeper duplicate updates the parked row instead of queueing a second one.
* ``planner:<mode>:<workstream_id>:<scope_id or ->`` - one row per artifact
  (the spec, a scope's plan, its tasks): the newest request replaces the
  payload, so a Manager re-consult is absorbed, not duplicated, and the
  Manager is told when the replaced request asked for something else. Such
  a request is a new consult: its wait and its queued notices start over.
  Research adds ``:<objective fingerprint>``: questions are independent, so
  each keeps its own row and they run one after another.
* ``flow:<request_id>`` - one Flow Architect / Data Curator consult.

Honest give-up only: every consult fails after three launch failures in a
row (each keeps its backoff even when the pump is woken) or after its agent
configuration has been missing for 10 min (counted from the first
``config_missing`` refusal, not from the first refusal of any kind);
Manager-issued Planner modes and flow consults also fail after 6 h. A
give-up is decided right after a fresh refusal, never before a retry: the
reason a parked row carries may have cleared since. A parked verification
has no age limit: the backend stuck-verifying sweeper owns its escalation
(and fires again after a silent give-up), and the row is dropped when the
scope leaves ``verifying``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 300.0
TICK_SECONDS = 30.0
# A wake (an agent exit, quota recovery) waits this long before draining so
# the supervisor has flipped the finished agent to idle.
WAKE_SETTLE_SECONDS = 2.0
MANAGER_CONSULT_MAX_AGE_SECONDS = 6 * 3600.0
CONFIG_MISSING_MAX_AGE_SECONDS = 600.0
MAX_CONSECUTIVE_LAUNCH_FAILURES = 3
# Chat notice for a parked Planner consult: once it has waited a minute,
# again at 30 minutes and at 2 hours.
NOTICE_AFTER_SECONDS = (60.0, 1800.0, 7200.0)
# A parked flow consult re-sends "queued" progress this often, which also
# refreshes the backend's one-hour poll status.
FLOW_PROGRESS_INTERVAL_SECONDS = 600.0

LAUNCH_FAILURE_REASONS = frozenset({"launch_failed", "ready_timeout"})

_REASON_TEXT = {
    "agent_busy": "the {agent} to finish another consult",
    "quota_paused": "the Claude usage limit to reset",
    "maintenance": "the office maintenance pause to end",
    "config_not_ready": "the office configuration to finish applying",
    "config_missing": "the {agent} configuration to sync",
    "orchestrator_not_ready": "the office orchestrator to start",
    "max_agents": "a free agent process (the office is at its process limit)",
    "admission_closed": "the office to reopen admission",
    "suppressed": "a stop request to clear",
    "duplicate_task": "the same consult to finish",
    "launch_failed": "a retry after the session failed to launch",
    "ready_timeout": "a retry after the session did not start in time",
    "queued_behind": "an earlier queued {agent} consult",
    "board_pool_full": "a free worker slot",
    "resources": "a shared resource to be released",
    "execution_deferred": "execution to be reconciled",
}


def objective_fingerprint(objective: object) -> str:
    """A short stable id of what a consult was asked. Letter case and
    whitespace do not count, so a re-sent request keeps its fingerprint."""
    text = " ".join(str(objective or "").lower().split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def consult_key(
    kind: str,
    *,
    mode: str = "",
    workstream_id: str = "",
    scope_id: str = "",
    request_id: str = "",
    objective: object = "",
) -> str:
    if kind == "flow":
        return f"flow:{request_id}"
    if mode == "verify" and scope_id:
        return f"verify:{scope_id}"
    key = f"planner:{mode or 'specify'}:{workstream_id or '-'}:{scope_id or '-'}"
    if mode == "research":
        # Research questions are independent of each other. Without the
        # objective in the key a second question replaced the first one in
        # the queue. The other modes write ONE artifact per key, so there
        # the newest request stands for the queued one.
        key = f"{key}:{objective_fingerprint(objective)}"
    return key


def reason_text(reason: str, agent: str = "Planner") -> str:
    """The wait in plain words, for chat notices and progress text."""
    label = {
        "planner": "Planner",
        "flow-architect": "Flow Architect",
        "data-curator": "Data Curator",
    }.get(agent, agent or "Planner")
    template = _REASON_TEXT.get(reason, "the office to allow it")
    return template.format(agent=label)


def retry_delay(attempts: int) -> float:
    exponent = max(0, int(attempts) - 1)
    return min(RETRY_BASE_SECONDS * (2 ** min(exponent, 10)), RETRY_MAX_SECONDS)


def keep_manager_trigger(stored: dict | None, incoming: dict) -> dict:
    """``incoming`` with ``trigger: manager`` kept when ``stored`` had it.

    Several requests can be merged into one queued consult (a sweeper or
    capacity-release re-fire into a verify the Manager asked for). A failure
    is reported to the Manager when ANY of them was the Manager's own, so a
    later backend fire must not overwrite its trigger.
    """
    if (stored or {}).get("trigger") == "manager" and incoming.get("trigger") != "manager":
        return {**incoming, "trigger": "manager"}
    return dict(incoming)


def _priority(row: dict) -> tuple:
    if row["kind"] == "planner" and row["mode"] == "verify":
        rank = 0
    elif row["kind"] == "planner":
        rank = 1
    else:
        rank = 2
    return (rank, row["first_refused_at"], row["key"])


def _updated_row(
    existing: dict | None,
    *,
    key: str,
    kind: str,
    agent: str,
    mode: str,
    command: str,
    payload: dict,
    reason: str,
    detail: str,
    now: float,
) -> dict:
    if existing is None:
        attempts = 1
        first = now
        launch_failures = 1 if reason in LAUNCH_FAILURE_REASONS else 0
        notices, last_notice = 0, 0.0
        reason_changed, reason_since = True, now
        payload = dict(payload)
    else:
        payload = keep_manager_trigger(existing["payload"], payload)
        attempts = existing["attempts"] + 1
        first = existing["first_refused_at"]
        launch_failures = (
            existing["launch_failures"] + 1 if reason in LAUNCH_FAILURE_REASONS else 0
        )
        notices, last_notice = existing["notices_sent"], existing["last_notice_at"]
        reason_changed = existing["last_reason"] != reason
        reason_since = now if reason_changed else existing["reason_since"]
    return {
        "key": key,
        "kind": kind,
        "agent": agent,
        "mode": mode,
        "command": command,
        "payload": payload,
        "first_refused_at": first,
        "attempts": attempts,
        "next_retry_at": now + retry_delay(attempts),
        "last_reason": reason,
        "last_detail": str(detail or "")[:300],
        "launch_failures": launch_failures,
        "notices_sent": notices,
        "last_notice_at": last_notice,
        # Since when the consult has been refused for THIS reason without a
        # break. ``first_refused_at`` is the start of the whole wait.
        "reason_since": reason_since,
        # Not stored: tells the caller this park is news, not a repeat.
        "reason_changed": reason_changed,
    }


class PendingConsultStateMixin:
    """``pending_consults`` in the host-only runtime ledger (RuntimeState)."""

    def initialize_pending_consults(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_consults (
                    office_id TEXT NOT NULL, key TEXT NOT NULL,
                    kind TEXT NOT NULL, agent TEXT NOT NULL,
                    mode TEXT NOT NULL, command TEXT NOT NULL,
                    payload TEXT NOT NULL, first_refused_at REAL NOT NULL,
                    attempts INTEGER NOT NULL, next_retry_at REAL NOT NULL,
                    last_reason TEXT NOT NULL, last_detail TEXT NOT NULL,
                    launch_failures INTEGER NOT NULL DEFAULT 0,
                    notices_sent INTEGER NOT NULL DEFAULT 0,
                    last_notice_at REAL NOT NULL DEFAULT 0,
                    reason_since REAL,
                    PRIMARY KEY (office_id, key)
                )
                """
            )
            # Since when the consult has waited for its CURRENT reason. A
            # ledger from before the column existed counts from the first
            # refusal, the only start it recorded.
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(pending_consults)")
            }
            if "reason_since" not in columns:
                connection.execute(
                    "ALTER TABLE pending_consults ADD COLUMN reason_since REAL"
                )
            connection.execute(
                "UPDATE pending_consults SET reason_since=first_refused_at "
                "WHERE reason_since IS NULL"
            )
            # A research consult parked before each question had its own key
            # sits under ``planner:research:<workstream>:<scope>``. Its retry
            # computes today's key, so the launch would leave the old row
            # behind to start the same consult again. Give it today's key.
            for row in connection.execute(
                "SELECT key, payload FROM pending_consults "
                "WHERE office_id=? AND kind='planner' AND mode='research'",
                (self.office_id,),
            ).fetchall():
                payload = self._consult_row(row)["payload"]
                key = consult_key(
                    "planner", mode="research",
                    workstream_id=str(payload.get("workstream_id") or ""),
                    scope_id=str(payload.get("scope_id") or ""),
                    objective=payload.get("objective"),
                )
                if key != row["key"]:
                    connection.execute(
                        "UPDATE OR REPLACE pending_consults SET key=? "
                        "WHERE office_id=? AND key=?",
                        (key, self.office_id, row["key"]),
                    )

    @staticmethod
    def _consult_row(row) -> dict:
        data = dict(row)
        data.pop("office_id", None)
        try:
            data["payload"] = json.loads(data["payload"])
        except (TypeError, ValueError):
            data["payload"] = {}
        return data

    def get_pending_consult(self, key: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pending_consults WHERE office_id=? AND key=?",
                (self.office_id, key),
            ).fetchone()
        return self._consult_row(row) if row else None

    def list_pending_consults(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_consults WHERE office_id=?",
                (self.office_id,),
            ).fetchall()
        return sorted((self._consult_row(row) for row in rows), key=_priority)

    def park_consult(
        self,
        key: str,
        *,
        kind: str,
        agent: str,
        mode: str,
        command: str,
        payload: dict,
        reason: str,
        detail: str = "",
        now: float | None = None,
    ) -> dict:
        now = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pending_consults WHERE office_id=? AND key=?",
                (self.office_id, key),
            ).fetchone()
            updated = _updated_row(
                self._consult_row(row) if row else None,
                key=key, kind=kind, agent=agent, mode=mode, command=command,
                payload=payload, reason=reason, detail=detail, now=now,
            )
            connection.execute(
                "INSERT INTO pending_consults (office_id, key, kind, agent, mode, "
                "command, payload, first_refused_at, attempts, next_retry_at, "
                "last_reason, last_detail, launch_failures, notices_sent, "
                "last_notice_at, reason_since) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(office_id, key) DO UPDATE SET kind=excluded.kind, "
                "agent=excluded.agent, mode=excluded.mode, command=excluded.command, "
                "payload=excluded.payload, attempts=excluded.attempts, "
                "next_retry_at=excluded.next_retry_at, last_reason=excluded.last_reason, "
                "last_detail=excluded.last_detail, launch_failures=excluded.launch_failures, "
                "reason_since=excluded.reason_since",
                (
                    self.office_id, key, kind, agent, mode, command,
                    json.dumps(updated["payload"], default=str),
                    updated["first_refused_at"],
                    updated["attempts"], updated["next_retry_at"], reason,
                    updated["last_detail"], updated["launch_failures"],
                    updated["notices_sent"], updated["last_notice_at"],
                    updated["reason_since"],
                ),
            )
        return updated

    def absorb_pending_consult(
        self,
        key: str,
        payload: dict | None = None,
        *,
        restart_wait: bool = False,
        now: float | None = None,
    ) -> bool:
        """A duplicate request for a parked consult: newest payload, retry now
        — except after a launch failure, whose backoff a duplicate (a sweeper
        re-fire, a capacity-release nudge) must not skip. The parked
        request's Manager trigger survives the merge
        (``keep_manager_trigger``).

        ``restart_wait``: the payload asks for something else than the
        request it replaces, so it is a new consult and its wait starts now
        (``first_refused_at``, ``reason_since``, the queued notices).
        Otherwise its next refusal would fail it as "could not start for
        6 h", for the wait of the request it replaced. The retry count and a
        launch failure's count and backoff stay: they describe the office,
        whatever is asked."""
        now = time.time() if now is None else now
        launch_reasons = sorted(LAUNCH_FAILURE_REASONS)
        assignments = [
            "next_retry_at=CASE WHEN last_reason IN "
            f"({', '.join('?' for _ in launch_reasons)}) "
            "THEN next_retry_at ELSE ? END"
        ]
        values: list[Any] = [*launch_reasons, now]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM pending_consults WHERE office_id=? AND key=?",
                (self.office_id, key),
            ).fetchone()
            if row is None:
                return False
            if payload is not None:
                merged = keep_manager_trigger(
                    self._consult_row(row)["payload"], payload
                )
                assignments.append("payload=?")
                values.append(json.dumps(merged, default=str))
                if restart_wait:
                    assignments.append(
                        "first_refused_at=?, reason_since=?, notices_sent=0, "
                        "last_notice_at=0"
                    )
                    values += [now, now]
            connection.execute(
                f"UPDATE pending_consults SET {', '.join(assignments)} "
                "WHERE office_id=? AND key=?",
                (*values, self.office_id, key),
            )
            return True

    def drop_pending_consult(self, key: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM pending_consults WHERE office_id=? AND key=?",
                (self.office_id, key),
            )
            return cursor.rowcount > 0

    def note_pending_consult_notice(self, key: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._connection() as connection:
            connection.execute(
                "UPDATE pending_consults SET notices_sent=notices_sent+1, "
                "last_notice_at=? WHERE office_id=? AND key=?",
                (now, self.office_id, key),
            )


class InMemoryPendingConsults:
    """The same interface without a runtime ledger (tests, bare harness)."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def get_pending_consult(self, key: str) -> dict | None:
        row = self._rows.get(key)
        return {**row, "payload": dict(row["payload"])} if row else None

    def list_pending_consults(self) -> list[dict]:
        return sorted(
            ({**row, "payload": dict(row["payload"])} for row in self._rows.values()),
            key=_priority,
        )

    def park_consult(self, key: str, **kwargs: Any) -> dict:
        now = kwargs.pop("now", None)
        updated = _updated_row(
            self._rows.get(key), key=key,
            now=time.time() if now is None else now,
            detail=kwargs.pop("detail", ""), **kwargs,
        )
        self._rows[key] = {k: v for k, v in updated.items() if k != "reason_changed"}
        return updated

    def absorb_pending_consult(
        self,
        key: str,
        payload: dict | None = None,
        *,
        restart_wait: bool = False,
        now: float | None = None,
    ) -> bool:
        row = self._rows.get(key)
        if row is None:
            return False
        now = time.time() if now is None else now
        if row.get("last_reason") not in LAUNCH_FAILURE_REASONS:
            row["next_retry_at"] = now
        if payload is not None:
            row["payload"] = keep_manager_trigger(row["payload"], payload)
            if restart_wait:
                row.update(
                    first_refused_at=now, reason_since=now,
                    notices_sent=0, last_notice_at=0.0,
                )
        return True

    def drop_pending_consult(self, key: str) -> bool:
        return self._rows.pop(key, None) is not None

    def note_pending_consult_notice(self, key: str, *, now: float | None = None) -> None:
        row = self._rows.get(key)
        if row is not None:
            row["notices_sent"] += 1
            row["last_notice_at"] = time.time() if now is None else now


class PendingConsultPump:
    """Retries parked consults; one pass at a time, verify first.

    ``retry`` re-runs one parked row and returns True when it launched a
    session.
    """

    def __init__(
        self,
        store,
        retry: Callable[[dict], Awaitable[bool]],
        *,
        notify: Callable[[dict, float], Awaitable[None]] | None = None,
        tick_seconds: float = TICK_SECONDS,
        settle_seconds: float = WAKE_SETTLE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._retry = retry
        self._notify = notify
        self._tick = tick_seconds
        self._settle = settle_seconds
        self._clock = clock
        self._event = asyncio.Event()
        self._force = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def wake(self) -> None:
        """Something that can unblock a consult changed: retry everything."""
        self._force = True
        self._event.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="pending-consults")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        self._force = True
        while True:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self._tick)
                await asyncio.sleep(self._settle)
            except asyncio.TimeoutError:
                pass
            self._event.clear()
            force, self._force = self._force, False
            try:
                await self.drain(force=force)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pending consult drain failed; retrying next tick")

    async def drain(self, *, force: bool = False) -> None:
        async with self._lock:
            started_agents: set[str] = set()
            for listed in self.store.list_pending_consults():
                # A pass can take minutes (each launch waits for its session
                # to report ready) and a duplicate request may replace a
                # parked payload meanwhile: retry what the row holds NOW,
                # not the copy listed when the pass began.
                row = self.store.get_pending_consult(listed["key"])
                if row is None or row["agent"] in started_agents:
                    continue
                now = self._clock()
                # A wake does not skip a launch failure's backoff: the
                # failure's own error event wakes the pump, and three
                # back-to-back launches would spend the whole budget in
                # seconds (a short container restart must not fail it).
                due = row["next_retry_at"] <= now or (
                    force and row["last_reason"] not in LAUNCH_FAILURE_REASONS
                )
                if due:
                    started = False
                    try:
                        started = bool(await self._retry(row))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Pending consult retry failed for %s", row["key"])
                    if started:
                        # One session per agent per pass: the agent's next
                        # row would only be refused as busy. A row that left
                        # the queue without a launch (dropped, given up)
                        # does not hold the others back.
                        started_agents.add(row["agent"])
                    after = self.store.get_pending_consult(row["key"])
                    if after is None:
                        continue
                    row = after
                if self._notify is not None:
                    try:
                        await self._notify(row, self._clock())
                    except Exception:
                        logger.debug("Pending consult notice failed", exc_info=True)


def pending_notice_due(row: dict, now: float) -> bool:
    """A parked Planner consult's next chat notice is due."""
    sent = int(row.get("notices_sent") or 0)
    if sent >= len(NOTICE_AFTER_SECONDS):
        return False
    return now - float(row["first_refused_at"]) >= NOTICE_AFTER_SECONDS[sent]


def flow_progress_due(row: dict, now: float) -> bool:
    last = float(row.get("last_notice_at") or 0.0)
    return last == 0.0 or now - last >= FLOW_PROGRESS_INTERVAL_SECONDS


def give_up_reason(row: dict, now: float) -> str | None:
    """Why a consult that was just refused must now fail honestly, or None
    to keep waiting.

    Call it on the row a fresh refusal produced, never on a stored row
    before retrying it: the stored reason may have cleared since (the
    configuration synced, the usage limit reset), and the retry is how the
    consult finds out.
    """
    reason = row.get("last_reason") or ""
    if int(row.get("launch_failures") or 0) >= MAX_CONSECUTIVE_LAUNCH_FAILURES:
        return "the consult session failed to launch three times in a row"
    age = now - float(row["first_refused_at"])
    # The configuration must have been missing for the whole limit, not the
    # consult merely old: one queued for an hour behind a busy agent has
    # waited for its configuration only since that refusal.
    missing_for = now - float(row.get("reason_since") or row["first_refused_at"])
    if reason == "config_missing" and missing_for >= CONFIG_MISSING_MAX_AGE_SECONDS:
        return "the agent configuration is missing (restart cbcl to resync)"
    if row["kind"] == "planner" and row["mode"] == "verify":
        return None
    if age >= MANAGER_CONSULT_MAX_AGE_SECONDS:
        hours = int(age // 3600)
        return (
            f"it could not start for {hours} h: waiting for "
            f"{reason_text(reason, row.get('agent') or 'planner')}"
        )
    return None


def queued_notice_text(mode: str, reason: str, agent: str = "planner") -> str:
    label = {
        "verify": "verification",
        "specify": "spec drafting",
        "scope_plan": "scope planning",
        "materialize": "task authoring",
        "research": "research",
    }.get(mode, mode or "consult")
    return (
        f"Planner {label} is queued: waiting for {reason_text(reason, agent)}. "
        "It starts automatically; no action is needed."
    )
