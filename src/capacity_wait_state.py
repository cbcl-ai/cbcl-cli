"""Durable task-phase capacity deferral, distinct from a started script receipt."""

import json
import time
import uuid

from src.operation_state import OperationConflict

ACTIVE_WAIT_STATES = {"waiting", "resuming"}
PHASE_STATUS = {"execute": "in_progress", "review": "review", "triage": "blocked"}


def _owner(task: dict, phase: str) -> str:
    if phase == "review":
        from src.review_routing import default_reviewer

        return task.get("reviewer") or default_reviewer(task)
    return (
        "manager-assistant"
        if phase == "triage"
        else task.get("assigned_agent") or "manager-assistant"
    )


def has_lineage(task: dict) -> bool:
    return (
        all(
            type(task.get(key)) is int and task[key] >= 0
            for key in (
                "execution_cycle",
                "execution_generation",
                "review_retry_epoch",
            )
        )
        and isinstance(task.get("status"), str)
        and task.get("status")
        in {"backlog", "ready", "in_progress", "review", "blocked", "done", "archived"}
        and all(
            task.get(key) is None or isinstance(task[key], str)
            for key in ("assigned_agent", "reviewer", "active_execution_attempt_id")
        )
        and "assigned_agent" in task
        and "active_execution_attempt_id" in task
    )


def matches_task(wait: dict, task: dict) -> bool:
    return has_lineage(task) and all(
        (
            str(task.get("task_id") or task.get("id")) == wait["task_id"],
            task["status"] == PHASE_STATUS[wait["phase"]],
            task["execution_cycle"] == wait["cycle"],
            task["execution_generation"] == wait["generation"],
            task["review_retry_epoch"] == wait["epoch"],
            task["active_execution_attempt_id"] == wait["attempt_id"],
            (task.get("assigned_agent") or "") == wait["assigned_agent"],
            _owner(task, wait["phase"]) == wait["agent_name"],
        )
    )


class CapacityWaitStateMixin:
    def initialize_capacity_waits(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capacity_waits (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    wait_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    cycle INTEGER NOT NULL, generation INTEGER NOT NULL, epoch INTEGER NOT NULL,
                    phase TEXT NOT NULL, agent_name TEXT NOT NULL, assigned_agent TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, pending_resume_attempt_id TEXT,
                    accepted_attempt_id TEXT NOT NULL,
                    state TEXT NOT NULL, resume_context TEXT NOT NULL,
                    next_check_at REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY (office_id, task_id), UNIQUE (office_id, wait_id)
                )
            """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_capacity_waits_active ON capacity_waits(office_id,task_id) WHERE state IN ('waiting','resuming')"
            )

    @staticmethod
    def _capacity_wait_row(row) -> dict | None:
        return (
            {**dict(row), "resume_context": json.loads(row["resume_context"])}
            if row
            else None
        )

    def capacity_wait(self, task_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM capacity_waits WHERE office_id=? AND task_id=?",
                (self.office_id, task_id),
            ).fetchone()
        return self._capacity_wait_row(row)

    def capacity_wait_for_task(self, task: dict) -> dict | None:
        wait = self.capacity_wait(str(task.get("task_id") or task.get("id") or ""))
        return (
            wait
            if wait and wait["state"] in ACTIVE_WAIT_STATES and matches_task(wait, task)
            else None
        )

    def active_capacity_waits(
        self, *, after_id: str = "", limit: int = 100
    ) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM capacity_waits WHERE office_id=? AND task_id>? AND state IN ('waiting','resuming') "
                "ORDER BY task_id LIMIT ?",
                (self.office_id, after_id, min(max(limit, 1), 100)),
            ).fetchall()
        return [self._capacity_wait_row(row) for row in rows]

    def register_capacity_wait(
        self,
        record: dict,
        task: dict,
        caller: dict,
        *,
        action: str,
        had_variable_overrides: bool
    ) -> dict | None:
        """Missing authority yields an ordinary refusal, never an accepted handoff."""
        phase = caller.get("task_mode")
        if (
            phase not in PHASE_STATUS
            or caller.get("role") != "worker"
            or not has_lineage(task)
            or not caller.get("attempt_id")
            or task.get("active_execution_attempt_id") != caller["attempt_id"]
            or any(
                type(caller.get(key, 0)) is not int
                for key in (
                    "execution_cycle",
                    "execution_generation",
                    "review_retry_epoch",
                )
            )
            or caller.get("execution_cycle") != task["execution_cycle"]
            or caller.get("execution_generation") != task["execution_generation"]
            or caller.get("review_retry_epoch", 0) != task["review_retry_epoch"]
            or str(task.get("id")) != caller.get("task_id")
            or task["status"] != PHASE_STATUS[phase]
            or _owner(task, phase) != caller.get("agent_name")
            or action not in {"start", "reconcile", "cancel"}
        ):
            return None
        if (
            record["task_id"] != caller["task_id"]
            or record["cycle"] != task["execution_cycle"]
            or record["phase"] != phase
            or not record["cleanup_confirmed"]
        ):
            raise OperationConflict(
                "Capacity deferral does not match a never-started observer"
            )
        context = {
            key: record[key]
            for key in (
                "operation_id",
                "operation_key",
                "script_name",
                "input_fingerprint",
                "phase",
            )
        }
        context.update(
            action=action, had_variable_overrides=bool(had_variable_overrides)
        )
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute(
                "SELECT * FROM capacity_waits WHERE office_id=? AND task_id=?",
                (self.office_id, caller["task_id"]),
            ).fetchone()
            if (
                old
                and old["state"] in ACTIVE_WAIT_STATES
                and old["operation_id"] != record["operation_id"]
            ):
                raise OperationConflict(
                    "This task already has a capacity handoff; resume or cancel that intent first"
                )
            wait_id = (
                old["wait_id"]
                if old and old["operation_id"] == record["operation_id"]
                else str(uuid.uuid4())
            )
            context["wait_id"] = wait_id
            connection.execute(
                """
                INSERT OR REPLACE INTO capacity_waits (
                    office_id,task_id,wait_id,operation_id,cycle,generation,epoch,phase,agent_name,
                    assigned_agent,attempt_id,accepted_attempt_id,pending_resume_attempt_id,state,
                    resume_context,next_check_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,'waiting',?,?,?)
            """,
                (
                    self.office_id,
                    caller["task_id"],
                    wait_id,
                    record["operation_id"],
                    task["execution_cycle"],
                    task["execution_generation"],
                    task["review_retry_epoch"],
                    phase,
                    caller["agent_name"],
                    task.get("assigned_agent") or "",
                    caller["attempt_id"],
                    caller["attempt_id"],
                    json.dumps(context, sort_keys=True),
                    now + 30,
                    now,
                ),
            )
        return self.capacity_wait(caller["task_id"])

    def capacity_completion_handoff(self, task: dict, event: dict) -> bool:
        wait = self.capacity_wait_for_task(task)
        caller = event.get("_caller") or {}
        return bool(
            wait
            and wait["state"] == "waiting"
            and caller.get("attempt_id") == wait["accepted_attempt_id"]
            and all(
                (
                    caller.get("attempt_id") == wait["attempt_id"],
                    task.get("active_execution_attempt_id") == wait["attempt_id"],
                    caller.get("task_id") == wait["task_id"],
                    caller.get("execution_generation") == wait["generation"],
                    caller.get("execution_cycle") == wait["cycle"],
                    caller.get("review_retry_epoch", 0) == wait["epoch"],
                    caller.get("task_mode") == wait["phase"],
                    caller.get("agent_name") == wait["agent_name"],
                )
            )
        )

    def delay_capacity_wait(self, wait_id: str, *, seconds: int = 30) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE capacity_waits SET next_check_at=? WHERE office_id=? AND wait_id=?",
                (time.time() + seconds, self.office_id, wait_id),
            )

    def retire_capacity_wait(self, wait_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE capacity_waits SET state='retired',updated_at=? WHERE office_id=? AND wait_id=?",
                (time.time(), self.office_id, wait_id),
            )

    def begin_capacity_resume_claim(
        self, task: dict, wait_id: str, attempt_id: str
    ) -> None:
        wait = self.capacity_wait_for_task(task)
        if not wait or wait["wait_id"] != wait_id:
            raise OperationConflict(
                "Capacity resume lineage changed before worker claim"
            )
        with self._connection() as connection:
            updated = connection.execute(
                "UPDATE capacity_waits SET pending_resume_attempt_id=?,state='resuming',updated_at=? "
                "WHERE office_id=? AND wait_id=? AND generation=? AND attempt_id=? AND state='waiting' "
                "AND pending_resume_attempt_id IS NULL",
                (
                    attempt_id,
                    time.time(),
                    self.office_id,
                    wait_id,
                    wait["generation"],
                    wait["attempt_id"],
                ),
            )
            if updated.rowcount != 1:
                raise OperationConflict("Capacity resume already claimed or retired")

    def bind_capacity_resume_claim(
        self, attempt_id: str, claim: dict, *, connection=None
    ) -> None:
        if connection is None:
            with self._connection() as current:
                return self.bind_capacity_resume_claim(
                    attempt_id, claim, connection=current
                )
        if (
            connection.execute(
                "SELECT 1 FROM capacity_waits WHERE office_id=? AND pending_resume_attempt_id=? AND state='resuming'",
                (self.office_id, attempt_id),
            ).fetchone()
            is None
        ):
            return
        updated = connection.execute(
            "UPDATE capacity_waits SET attempt_id=?,generation=?,pending_resume_attempt_id=NULL,updated_at=? "
            "WHERE office_id=? AND pending_resume_attempt_id=? AND state='resuming' AND cycle=? AND generation=? "
            "AND epoch=? AND agent_name=?",
            (
                attempt_id,
                claim["execution_generation"],
                time.time(),
                self.office_id,
                attempt_id,
                claim["execution_cycle"],
                claim["execution_generation"] - 1,
                claim.get("review_retry_epoch", 0),
                claim["agent_name"],
            ),
        )
        if updated.rowcount != 1:
            raise OperationConflict(
                "Capacity resume claim does not match its durable pending identity"
            )

    def complete_capacity_resume(self, task_id: str, attempt_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE capacity_waits SET state='resumed',updated_at=? WHERE office_id=? AND task_id=? "
                "AND attempt_id=? AND state='resuming'",
                (time.time(), self.office_id, task_id, attempt_id),
            )

    def reconcile_capacity_claim_gaps(self) -> None:
        """An absent claim journal proves this pending attempt never reached POST,
        or its exact cleanup/release already completed. Never infer live cleanup.
        """
        with self._connection() as connection:
            connection.execute(
                "UPDATE capacity_waits SET state='waiting',pending_resume_attempt_id=NULL,updated_at=? "
                "WHERE office_id=? AND state='resuming' AND NOT EXISTS ("
                "SELECT 1 FROM worker_executions w WHERE w.office_id=capacity_waits.office_id AND "
                "w.attempt_id=COALESCE(capacity_waits.pending_resume_attempt_id,capacity_waits.attempt_id))",
                (time.time(), self.office_id),
            )
