"""Host-only admission control and durable execution recovery state."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from src.quota_recovery import QuotaStateMixin
from src.worker_journal import WorkerJournalMixin
from src.script_resource_state import ScriptResourceStateMixin


_active_admission: ContextVar[str | None] = ContextVar("runtime_admission", default=None)
_generation_controls: dict[str, "RuntimeState"] = {}


def register_generation_runtime(container_name: str, runtime_state: "RuntimeState") -> None:
    if container_name:
        _generation_controls[container_name] = runtime_state


def generation_runtime(container_name: str) -> "RuntimeState | None":
    return _generation_controls.get(container_name)


class AdmissionPaused(RuntimeError):
    """New execution admission is paused; existing work must not be killed."""


class QuotaPaused(AdmissionPaused):
    """Claude capacity pauses AI admission independently of maintenance."""


class RuntimeState(QuotaStateMixin, WorkerJournalMixin, ScriptResourceStateMixin):
    def __init__(self, database_path: Path, office_id: str) -> None:
        self.database_path = database_path
        self.office_id = str(office_id)
        self.instance_id = secrets.token_hex(16)
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS maintenance (
                    scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                    changed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admissions (
                    token TEXT PRIMARY KEY, office_id TEXT NOT NULL,
                    kind TEXT NOT NULL, task_id TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admission_owners (
                    token TEXT PRIMARY KEY, instance_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot_owners (
                    office_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_snapshots (
                    office_id TEXT PRIMARY KEY, observed_at REAL NOT NULL,
                    active_workers INTEGER NOT NULL, active_scripts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_cycles (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    cycle INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (office_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS recovery_attempts (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    cycle INTEGER NOT NULL, attempt_id TEXT NOT NULL,
                    PRIMARY KEY (office_id, task_id, cycle, attempt_id)
                );
                CREATE TABLE IF NOT EXISTS script_handoffs (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL, state TEXT NOT NULL, cycle INTEGER NOT NULL,
                    PRIMARY KEY (office_id, task_id, execution_id, cycle)
                );
                CREATE TABLE IF NOT EXISTS script_execution_owners (
                    office_id TEXT NOT NULL, execution_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    PRIMARY KEY (office_id, execution_id)
                );
                CREATE TABLE IF NOT EXISTS task_script_waits (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL, cycle INTEGER NOT NULL,
                    PRIMARY KEY (office_id, task_id, cycle)
                );
                CREATE TABLE IF NOT EXISTS script_invocations (
                    office_id TEXT NOT NULL, invocation_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, execution_id TEXT,
                    PRIMARY KEY (office_id, invocation_id)
                );
                CREATE TABLE IF NOT EXISTS review_recovery (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL, cycle INTEGER NOT NULL,
                    reviewer TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0, request_id TEXT,
                    PRIMARY KEY (office_id, task_id, cycle, reviewer)
                );
                CREATE TABLE IF NOT EXISTS review_attempts (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL, cycle INTEGER NOT NULL,
                    reviewer TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    PRIMARY KEY (office_id, task_id, cycle, reviewer, attempt_id)
                );
                CREATE TABLE IF NOT EXISTS worker_completions (
                    office_id TEXT NOT NULL, attempt_id TEXT NOT NULL, agent_name TEXT NOT NULL,
                    task_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (office_id, attempt_id)
                );
                CREATE TABLE IF NOT EXISTS worker_completion_cleanup (
                    office_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    execution_marker TEXT NOT NULL, container_managed INTEGER NOT NULL,
                    PRIMARY KEY (office_id, attempt_id)
                );
                CREATE TABLE IF NOT EXISTS review_recovery_v2 (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL, cycle INTEGER NOT NULL,
                    reviewer TEXT NOT NULL, epoch INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT, hold_kind TEXT NOT NULL DEFAULT 'legacy', closed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (office_id, task_id, cycle, reviewer, epoch)
                );
                CREATE TABLE IF NOT EXISTS review_attempts_v2 (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL, cycle INTEGER NOT NULL,
                    reviewer TEXT NOT NULL, epoch INTEGER NOT NULL, attempt_id TEXT NOT NULL,
                    PRIMARY KEY (office_id, task_id, cycle, reviewer, epoch, attempt_id)
                );
                INSERT OR IGNORE INTO review_recovery_v2
                    SELECT office_id, task_id, cycle, reviewer, 0, failures, request_id, 'legacy', 0 FROM review_recovery;
                INSERT OR IGNORE INTO review_attempts_v2
                    SELECT office_id, task_id, cycle, reviewer, 0, attempt_id FROM review_attempts ORDER BY rowid;
            """)
        os.chmod(database_path, 0o600)
        self.initialize_quota_state()
        self.initialize_worker_journal()
        self.initialize_script_resources()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def set_maintenance(self, enabled: bool) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO maintenance VALUES (?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled, "
                "changed_at=excluded.changed_at",
                (self.office_id, int(enabled), time.time()),
            )

    def retain_completion(
        self, agent_name: str, attempt_id: str, task_id: str, event: dict,
        *, cleanup: dict | None = None,
    ) -> None:
        """Retain a worker outcome (completion or fatal error) before cleanup.

        Cleanup identity is separate from the callback payload. Its original
        marker remains until acknowledgement so restart always confirms death,
        including when cleanup succeeded but callback delivery did not.
        """
        if not agent_name or not attempt_id or not task_id or event.get("task_id") != task_id:
            raise ValueError("Completion identity must match its owning task")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT agent_name, task_id, payload FROM worker_completions WHERE office_id=? AND attempt_id=?",
                (self.office_id, attempt_id),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["agent_name"] != agent_name
                    or receipt["task_id"] != task_id
                    or json.loads(receipt["payload"]) != event
                ):
                    raise RuntimeError("Completion attempt conflicts with an existing retained outcome")
                return
            connection.execute(
                "INSERT INTO worker_completions VALUES (?, ?, ?, ?, ?)",
                (self.office_id, attempt_id, agent_name, task_id, json.dumps(event)),
            )
            if cleanup and cleanup.get("execution_marker"):
                connection.execute(
                    "INSERT INTO worker_completion_cleanup VALUES (?, ?, ?, ?)",
                    (self.office_id, attempt_id, cleanup["execution_marker"], int(cleanup["container_managed"])),
                )

    def acknowledge_completion(self, attempt_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM worker_executions WHERE office_id=? AND attempt_id=?",
                (self.office_id, attempt_id),
            )
            connection.execute(
                "DELETE FROM worker_completions WHERE office_id=? AND attempt_id=?",
                (self.office_id, attempt_id),
            )
            connection.execute(
                "DELETE FROM worker_completion_cleanup WHERE office_id=? AND attempt_id=?",
                (self.office_id, attempt_id),
            )

    def pending_completions(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT outcomes.*, cleanup.execution_marker, cleanup.container_managed "
                "FROM worker_completions AS outcomes LEFT JOIN worker_completion_cleanup AS cleanup "
                "ON outcomes.office_id=cleanup.office_id AND outcomes.attempt_id=cleanup.attempt_id "
                "WHERE outcomes.office_id=?", (self.office_id,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def has_pending_completion(self, task_id: str) -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM worker_completions WHERE office_id=? AND task_id=? LIMIT 1",
                (self.office_id, task_id),
            ).fetchone() is not None

    @staticmethod
    def _review_epoch(epoch: int) -> int:
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("An authoritative nonnegative review retry epoch is required")
        return epoch

    def review_state(self, task_id: str, cycle: int, reviewer: str, *, epoch: int = 0) -> dict:
        with self._connection() as connection:
            receipt = connection.execute(
                "SELECT failures, request_id, hold_kind FROM review_recovery_v2 WHERE office_id=? AND task_id=? AND cycle=? AND reviewer=? AND epoch=? AND closed=0",
                (self.office_id, task_id, cycle, reviewer, self._review_epoch(epoch)),
            ).fetchone()
            return dict(receipt) if receipt else {"failures": 0, "request_id": None, "hold_kind": None}

    def record_review_attempt(self, task_id: str, cycle: int, reviewer: str, attempt_id: str, *, epoch: int = 0) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            identity = (self.office_id, task_id, cycle, reviewer, self._review_epoch(epoch))
            connection.execute("INSERT OR IGNORE INTO review_recovery_v2 VALUES (?, ?, ?, ?, ?, 0, NULL, 'legacy', 0)", identity)
            inserted = connection.execute("INSERT OR IGNORE INTO review_attempts_v2 VALUES (?, ?, ?, ?, ?, ?)", (*identity, attempt_id)).rowcount
            if inserted:
                connection.execute(
                    "UPDATE review_recovery_v2 SET failures=failures+1 WHERE office_id=? AND task_id=? AND cycle=? AND reviewer=? AND epoch=?",
                    identity,
                )
            return connection.execute(
                "SELECT failures FROM review_recovery_v2 WHERE office_id=? AND task_id=? AND cycle=? AND reviewer=? AND epoch=?",
                identity,
            ).fetchone()[0]

    def latest_review_attempt(self, task_id: str, cycle: int, reviewer: str, *, epoch: int = 0) -> str | None:
        with self._connection() as connection:
            receipt = connection.execute(
                "SELECT attempt_id FROM review_attempts_v2 WHERE office_id=? AND task_id=? AND cycle=? AND reviewer=? AND epoch=? ORDER BY rowid DESC LIMIT 1",
                (self.office_id, task_id, cycle, reviewer, self._review_epoch(epoch)),
            ).fetchone()
        try:
            return str(uuid.UUID(receipt["attempt_id"])) if receipt else None
        except (ValueError, TypeError, AttributeError):
            return None

    def hold_review(self, task_id: str, cycle: int, reviewer: str, request_id: str, *, epoch: int = 0, hold_kind: str = "legacy") -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO review_recovery_v2 VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0) "
                "ON CONFLICT(office_id, task_id, cycle, reviewer, epoch) DO UPDATE SET request_id=excluded.request_id, hold_kind=excluded.hold_kind",
                (self.office_id, task_id, cycle, reviewer, self._review_epoch(epoch), request_id, hold_kind),
            )

    def observe_review_phase(self, task_id: str, cycle: int, status: str, *, epoch: int = 0) -> None:
        if status not in {"backlog", "ready", "in_progress", "blocked", "done", "archived"} or self.current_cycle(task_id) != cycle:
            return
        with self._connection() as connection:
            connection.execute(
                "UPDATE review_recovery_v2 SET closed=1 WHERE office_id=? AND task_id=? AND cycle=? AND epoch=?",
                (self.office_id, task_id, cycle, self._review_epoch(epoch)),
            )

    def begin_script_invocation(self, invocation_id: str, fingerprint: str) -> dict:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT fingerprint, execution_id FROM script_invocations WHERE office_id=? AND invocation_id=?",
                (self.office_id, invocation_id),
            ).fetchone()
            if receipt is not None:
                if receipt["fingerprint"] != fingerprint:
                    raise ValueError("Script invocation identity cannot be reused for another request")
                return {"state": "completed" if receipt["execution_id"] else "pending", "execution_id": receipt["execution_id"]}
            connection.execute(
                "INSERT INTO script_invocations VALUES (?, ?, ?, NULL)",
                (self.office_id, invocation_id, fingerprint),
            )
            return {"state": "new", "execution_id": None}

    def finish_script_invocation(self, invocation_id: str, fingerprint: str, execution_id: str) -> None:
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError("A confirmed script execution identity is required")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT fingerprint, execution_id FROM script_invocations WHERE office_id=? AND invocation_id=?",
                (self.office_id, invocation_id),
            ).fetchone()
            if receipt is None or receipt["fingerprint"] != fingerprint:
                raise RuntimeError("Script invocation receipt could not be confirmed")
            if receipt["execution_id"] == execution_id:
                return
            if receipt["execution_id"] is not None:
                raise RuntimeError("Script invocation already belongs to another execution")
            cursor = connection.execute(
                "UPDATE script_invocations SET execution_id=? WHERE office_id=? AND invocation_id=? AND fingerprint=? AND execution_id IS NULL",
                (execution_id, self.office_id, invocation_id, fingerprint),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Script invocation receipt could not be confirmed")

    def abandon_unstarted_script_invocation(self, invocation_id: str, fingerprint: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM script_invocations WHERE office_id=? AND invocation_id=? AND fingerprint=? AND execution_id IS NULL",
                (self.office_id, invocation_id, fingerprint),
            )

    def _maintenance(self, connection) -> tuple[bool, float]:
        rows = connection.execute(
            "SELECT enabled, changed_at FROM maintenance WHERE scope IN ('*', ?)",
            (self.office_id,),
        ).fetchall()
        return any(row["enabled"] for row in rows), max(
            (row["changed_at"] for row in rows), default=0.0,
        )

    def admission_open(self) -> bool:
        with self._connection() as connection:
            return not self._maintenance(connection)[0]

    def reserve(self, kind: str, task_id: str = "", *, parent_token: str | None = None) -> str:
        token = secrets.token_hex(16)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inherited = parent_token and connection.execute(
                "SELECT 1 FROM admissions WHERE token=? AND office_id=?",
                (parent_token, self.office_id),
            ).fetchone()
            if kind in {"worker", "generation"} and self.quota_status()["state"] != "running":
                raise QuotaPaused("Claude usage limit reached; AI work is paused until capacity is verified")
            if self._maintenance(connection)[0] and not inherited:
                raise AdmissionPaused("Office maintenance pauses new worker and script execution")
            connection.execute(
                "INSERT INTO admissions VALUES (?, ?, ?, ?, ?)",
                (token, self.office_id, kind, task_id, time.time()),
            )
            connection.execute("INSERT INTO admission_owners VALUES (?, ?)", (token, self.instance_id))
            connection.execute(
                "UPDATE runtime_snapshots SET observed_at=0 WHERE office_id=?", (self.office_id,),
            )
        return token

    def release(self, token: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM admission_owners WHERE token IN (SELECT token FROM admissions WHERE token=? AND office_id=?)",
                (token, self.office_id),
            )
            connection.execute(
                "DELETE FROM admissions WHERE token=? AND office_id=?",
                (token, self.office_id),
            )
            connection.execute(
                "UPDATE runtime_snapshots SET observed_at=0 WHERE office_id=?", (self.office_id,),
            )

    def owns_reservation(self, token: str, kind: str | None = "worker") -> bool:
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM admissions WHERE token=? AND office_id=? AND (? IS NULL OR kind=?)",
                (token, self.office_id, kind, kind),
            ).fetchone() is not None

    @contextmanager
    def admission(self, kind: str):
        inherited = _active_admission.get()
        reservation = self.reserve(kind, parent_token=inherited)
        context_token = _active_admission.set(reservation)
        try:
            yield reservation
        finally:
            _active_admission.reset(context_token)
            self.release(reservation)

    def invalidate_snapshot(self) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE runtime_snapshots SET observed_at=0 WHERE office_id=?", (self.office_id,),
            )

    def snapshot(self, active_workers: int, active_scripts: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO snapshot_owners VALUES (?, ?) ON CONFLICT(office_id) DO UPDATE SET instance_id=excluded.instance_id",
                (self.office_id, self.instance_id),
            )
            connection.execute(
                "INSERT INTO runtime_snapshots VALUES (?, ?, ?, ?) "
                "ON CONFLICT(office_id) DO UPDATE SET observed_at=excluded.observed_at, "
                "active_workers=excluded.active_workers, active_scripts=excluded.active_scripts",
                (self.office_id, time.time(), active_workers, active_scripts),
            )

    def maintenance_status(self, *, max_age: float = 90.0) -> dict:
        with self._connection() as connection:
            enabled, changed_at = self._maintenance(connection)
            snapshot = connection.execute(
                "SELECT * FROM runtime_snapshots WHERE office_id=?", (self.office_id,),
            ).fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) FROM admissions WHERE office_id=?", (self.office_id,),
            ).fetchone()[0]
            snapshot_owner = connection.execute(
                "SELECT instance_id FROM snapshot_owners WHERE office_id=?", (self.office_id,),
            ).fetchone()
            reservations = connection.execute(
                "SELECT admission.kind, admission.task_id, admission.created_at, owner.instance_id "
                "FROM admissions AS admission LEFT JOIN admission_owners AS owner ON admission.token=owner.token "
                "WHERE admission.office_id=?", (self.office_id,),
            ).fetchall()
        fresh = bool(
            snapshot and snapshot["observed_at"] >= changed_at
            and 0 <= time.time() - snapshot["observed_at"] <= max_age
        )
        active_workers = snapshot["active_workers"] if snapshot else None
        active_scripts = snapshot["active_scripts"] if snapshot else None
        retained = [
            {
                "kind": row["kind"], "task_id": row["task_id"],
                "age_seconds": max(0, int(time.time() - row["created_at"])),
                "instance_id": row["instance_id"],
            }
            for row in reservations
            if snapshot_owner and row["instance_id"] != snapshot_owner["instance_id"]
        ]
        from src.docker.execution_ledger import read_execution_inventory

        inventory_error = False
        try:
            isolated = read_execution_inventory(self.database_path.with_name("execution-containers.sqlite"), self.office_id)
        except (OSError, sqlite3.Error, RuntimeError):
            isolated = []
            inventory_error = True
        isolated_unconfirmed = bool(isolated) and (
            not active_workers or any(row["state"] != "running" for row in isolated)
        )
        return {
            "office_id": self.office_id, "enabled": enabled,
            "state": (
                "unknown" if inventory_error else "open" if not enabled else "unknown" if not fresh else
                "reconciliation_required" if retained or isolated_unconfirmed else
                "draining" if pending or active_workers or active_scripts or isolated else "drained"
            ),
            "active_workers": active_workers, "active_scripts": active_scripts,
            "pending_admissions": pending, "fresh_acknowledgment": fresh,
            "retained_admissions": retained,
            "isolated_executions": isolated,
            "isolated_inventory_confirmed": not inventory_error,
            "reconciliation_message": (
                "Isolated execution ownership could not be read. Do not restart or change containment mode until its durable ledger is verified."
                if inventory_error else
                "Isolated executions remain retained. Reconcile their exact container identities; no work was automatically replayed or declared stopped."
                if isolated_unconfirmed else
                "Reservations from a previous daemon remain unconfirmed. Verify the old office executions and container identity before operator recovery; no work was killed or automatically replayed."
                if retained else None
            ),
        }

    def known_office_ids(self) -> set[str]:
        from src.docker.execution_ledger import read_execution_inventory

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT office_id FROM runtime_snapshots UNION SELECT office_id FROM admissions"
            ).fetchall()
        isolated = read_execution_inventory(self.database_path.with_name("execution-containers.sqlite"))
        return {row[0] for row in rows} | {row["office_id"] for row in isolated}

    def observe_cycle(self, task_id: str, cycle: int | None) -> None:
        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 0:
            return
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO recovery_cycles VALUES (?, ?, ?, 0)",
                (self.office_id, task_id, cycle),
            )
            connection.execute(
                "UPDATE recovery_cycles SET cycle=?, failures=0 "
                "WHERE office_id=? AND task_id=? AND cycle<?",
                (cycle, self.office_id, task_id, cycle),
            )

    def failure_count(self, task_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT failures FROM recovery_cycles WHERE office_id=? AND task_id=?",
                (self.office_id, task_id),
            ).fetchone()
        return row[0] if row else 0

    def record_failure(self, task_id: str, attempt_id: str, cycle: int | None = None) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO recovery_cycles VALUES (?, ?, 0, 0)",
                (self.office_id, task_id),
            )
            row = connection.execute(
                "SELECT cycle, failures FROM recovery_cycles WHERE office_id=? AND task_id=?",
                (self.office_id, task_id),
            ).fetchone()
            if cycle is not None and cycle != row["cycle"]:
                return row["failures"]
            inserted = connection.execute(
                "INSERT OR IGNORE INTO recovery_attempts VALUES (?, ?, ?, ?)",
                (self.office_id, task_id, row["cycle"], attempt_id),
            ).rowcount
            if inserted:
                connection.execute(
                    "UPDATE recovery_cycles SET failures=failures+1 WHERE office_id=? AND task_id=?",
                    (self.office_id, task_id),
                )
            return row["failures"] + inserted

    def note_script(self, task_id: str, execution_id: str, state: str, *, cycle: int | None = None) -> None:
        with self._connection() as connection:
            cycle = self._current_cycle(connection, task_id) if cycle is None else cycle
            connection.execute(
                "INSERT INTO script_handoffs VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(office_id, task_id, execution_id, cycle) DO UPDATE SET state=excluded.state",
                (self.office_id, task_id, execution_id, state, cycle),
            )

    def script_handoffs(self, task_id: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT execution_id, state FROM script_handoffs WHERE office_id=? AND task_id=? AND cycle=?",
                (self.office_id, task_id, self._current_cycle(connection, task_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def note_script_owner(self, execution_id: str, attempt_id: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT attempt_id FROM script_execution_owners WHERE office_id=? AND execution_id=?",
                (self.office_id, execution_id),
            ).fetchone()
            if receipt is not None:
                if receipt["attempt_id"] != attempt_id:
                    raise RuntimeError("Script execution already belongs to another worker attempt")
                return
            connection.execute(
                "INSERT INTO script_execution_owners VALUES (?, ?, ?)",
                (self.office_id, execution_id, attempt_id),
            )

    def execution_started_script(self, task_id: str, attempt_id: str) -> bool:
        if not attempt_id:
            return False
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM script_handoffs AS handoff JOIN script_execution_owners AS owner "
                "ON handoff.office_id=owner.office_id AND handoff.execution_id=owner.execution_id "
                "WHERE handoff.office_id=? AND handoff.task_id=? AND handoff.cycle=? AND owner.attempt_id=? LIMIT 1",
                (self.office_id, task_id, self._current_cycle(connection, task_id), attempt_id),
            ).fetchone() is not None

    def _current_cycle(self, connection, task_id: str) -> int:
        row = connection.execute(
            "SELECT cycle FROM recovery_cycles WHERE office_id=? AND task_id=?",
            (self.office_id, task_id),
        ).fetchone()
        return row[0] if row else 0

    def current_cycle(self, task_id: str) -> int:
        with self._connection() as connection:
            return self._current_cycle(connection, task_id)

    def park_script_handoff(self, task_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_script_waits VALUES (?, ?, ?)",
                (self.office_id, task_id, self._current_cycle(connection, task_id)),
            )

    def script_wait(self, task_id: str) -> dict | None:
        with self._connection() as connection:
            waiting = connection.execute(
                "SELECT 1 FROM task_script_waits WHERE office_id=? AND task_id=? AND cycle=?",
                (self.office_id, task_id, self._current_cycle(connection, task_id)),
            ).fetchone()
        if not waiting:
            return None
        executions = self.script_handoffs(task_id)
        pending = not executions or any(
            execution["state"]
            not in {
                "completed",
                "failed",
                "killed",
                "cancelled",
                "timeout",
                "timed_out",
            }
            for execution in executions
        )
        return {"state": "waiting" if pending else "resumable", "executions": executions}

    def resume_script_handoff(self, task_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM task_script_waits WHERE office_id=? AND task_id=? AND cycle=?",
                (self.office_id, task_id, self._current_cycle(connection, task_id)),
            )

    def unresolved_scripts(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT task_id, execution_id, state, cycle FROM script_handoffs "
                "WHERE office_id=? AND state NOT IN ('completed', 'failed', 'killed', 'cancelled', 'timeout', 'timed_out')",
                (self.office_id,),
            ).fetchall()
        return [dict(row) for row in rows]
