"""Durable claim-to-cleanup receipts, including a lost claim response."""

import json
import time


class WorkerJournalMixin:
    def initialize_worker_journal(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_executions (
                    office_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL, task_id TEXT NOT NULL,
                    request TEXT NOT NULL, receipt TEXT,
                    execution_marker TEXT NOT NULL DEFAULT '',
                    container_managed INTEGER NOT NULL DEFAULT 0,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (office_id, attempt_id)
                )
            """
            )

    def begin_worker_claim(self, name: str, task_id: str, request: dict) -> None:
        encoded = json.dumps(request, sort_keys=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request, task_id, agent_name FROM worker_executions WHERE office_id=? AND attempt_id=?",
                (self.office_id, request["attempt_id"]),
            ).fetchone()
            if row and (
                row["request"] != encoded
                or row["task_id"] != task_id
                or row["agent_name"] != name
            ):
                raise ValueError("Execution claim conflicts with its durable identity")
            connection.execute(
                "INSERT OR IGNORE INTO worker_executions (office_id, attempt_id, agent_name, task_id, request) VALUES (?, ?, ?, ?, ?)",
                (self.office_id, request["attempt_id"], name, task_id, encoded),
            )

    def record_worker_claim(self, attempt_id: str, receipt: dict) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE worker_executions SET receipt=? WHERE office_id=? AND attempt_id=?",
                (json.dumps(receipt, sort_keys=True), self.office_id, attempt_id),
            )
            self.bind_capacity_resume_claim(attempt_id, receipt, connection=connection)

    def record_worker_launch(
        self, attempt_id: str, marker: str, isolated: bool
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE worker_executions SET execution_marker=?, container_managed=? WHERE office_id=? AND attempt_id=?",
                (marker, int(isolated), self.office_id, attempt_id),
            )

    def record_worker_stop(self, attempt_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE worker_executions SET stop_requested=1 WHERE office_id=? AND attempt_id=?",
                (self.office_id, attempt_id),
            )

    def forget_worker_execution(self, attempt_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM worker_executions WHERE office_id=? AND attempt_id=?",
                (self.office_id, attempt_id),
            )
            connection.execute("UPDATE capacity_waits SET state='waiting',pending_resume_attempt_id=NULL,updated_at=? "
                               "WHERE office_id=? AND state='resuming' AND (attempt_id=? OR pending_resume_attempt_id=?)",
                               (time.time(), self.office_id, attempt_id, attempt_id))

    def pending_worker_executions(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM worker_executions WHERE office_id=? ORDER BY rowid",
                (self.office_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "request": json.loads(row["request"]),
                "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            }
            for row in rows
        ]
