"""Durable launch-failure budget for Manager Assistant blocked-task triage.

A triage session that cannot even be launched (workspace refusal, process
launch failure, boot failure) is not a worker crash: the task stays Blocked
and the watchdog never meters it. Without a budget the dispatcher would claim
a new backend attempt for the same blocked task on every reconcile, forever.

The count is keyed by task and execution cycle. A new cycle (the task was
resumed and executed again) starts a fresh budget.
"""

from __future__ import annotations

# Launch failures of the same blocked task, in one execution cycle, before the
# dispatcher stops retrying triage and raises one visible escalation.
TRIAGE_LAUNCH_FAILURE_BUDGET = 3

# Failure reasons that mean the triage session never started work. Capacity,
# quota, claim-deferral and suppression refusals return before a launch and
# are never counted.
TRIAGE_LAUNCH_FAILURE_REASONS = frozenset(
    {"spawn_failed", "ready_failed", "ready_timeout", "assignment_failed"}
)

_CAUSE_MAX_CHARS = 300


class TriageLaunchStateMixin:
    def initialize_triage_launch_failures(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS triage_launch_failures (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    cycle INTEGER NOT NULL, failures INTEGER NOT NULL,
                    last_attempt_id TEXT NOT NULL, cause TEXT NOT NULL,
                    PRIMARY KEY (office_id, task_id)
                )
                """
            )

    def record_triage_launch_failure(
        self, task_id: str, cycle: int, attempt_id: str, cause: str
    ) -> int:
        """Count one failed triage launch; a repeated attempt id is counted once."""
        cause = " ".join(str(cause or "").split())[:_CAUSE_MAX_CHARS]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT cycle, failures, last_attempt_id FROM triage_launch_failures "
                "WHERE office_id=? AND task_id=?",
                (self.office_id, task_id),
            ).fetchone()
            if row is not None and row["cycle"] == cycle:
                if row["last_attempt_id"] == attempt_id:
                    return row["failures"]
                failures = row["failures"] + 1
            else:
                failures = 1
            connection.execute(
                "INSERT INTO triage_launch_failures VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(office_id, task_id) DO UPDATE SET cycle=excluded.cycle, "
                "failures=excluded.failures, last_attempt_id=excluded.last_attempt_id, "
                "cause=excluded.cause",
                (self.office_id, task_id, cycle, failures, attempt_id, cause),
            )
            return failures

    def triage_launch_failures(self, task_id: str, cycle: int) -> tuple[int, str]:
        """Return ``(failures, last cause)`` for this task's current cycle."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cycle, failures, cause FROM triage_launch_failures "
                "WHERE office_id=? AND task_id=?",
                (self.office_id, task_id),
            ).fetchone()
        if row is None or row["cycle"] != cycle:
            return 0, ""
        return row["failures"], row["cause"]

    def clear_triage_launch_failures(self, task_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM triage_launch_failures WHERE office_id=? AND task_id=?",
                (self.office_id, task_id),
            )
