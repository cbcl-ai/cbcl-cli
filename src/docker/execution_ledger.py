"""Host-only durable ownership and aggregate admission for isolated executions."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid


class ExecutionAdmissionError(RuntimeError):
    pass


class ExecutionCapacityUnavailable(ExecutionAdmissionError):
    """Reservation refused before any new Docker launch was admitted."""


@dataclass(frozen=True)
class ExecutionResources:
    cpu_millis: int
    memory_bytes: int
    pids: int

    def __post_init__(self):
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in asdict(self).values()):
            raise ValueError("Execution resource limits must be positive integers")


@dataclass(frozen=True)
class ExecutionBudget:
    max_workers: int
    cpu_millis: int
    memory_bytes: int
    pids: int

    def __post_init__(self):
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in asdict(self).values()):
            raise ValueError("Execution budget limits must be positive integers")


class ExecutionLedger:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).absolute()
        parent = self.database_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for ancestor in (parent, *parent.parents):
            if ancestor.is_symlink():
                raise ExecutionAdmissionError("Execution ledger must not traverse symbolic links")
        metadata = parent.stat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ExecutionAdmissionError("Execution ledger requires a private host-owned directory")
        descriptor = os.open(self.database_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.geteuid():
                raise ExecutionAdmissionError("Execution ledger ownership is unsafe")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS execution_budgets (
                    office_id TEXT PRIMARY KEY, budget TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_containers (
                    office_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    attempt_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    reservation_id TEXT NOT NULL, office_container_id TEXT NOT NULL,
                    image_id TEXT NOT NULL, container_id TEXT UNIQUE,
                    state TEXT NOT NULL, cpu_millis INTEGER NOT NULL,
                    memory_bytes INTEGER NOT NULL, pids INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    spec_json TEXT NOT NULL DEFAULT ''
                );
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(execution_containers)")}
            if "spec_json" not in columns:
                connection.execute("ALTER TABLE execution_containers ADD COLUMN spec_json TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve(self, *, office_id: str, task_id: str, attempt_id: str, fingerprint: str,
                office_container_id: str, image_id: str, resources: ExecutionResources,
                budget: ExecutionBudget, launch_spec: dict | None = None) -> tuple[dict, bool]:
        office_id, task_id, attempt_id = (str(uuid.UUID(value)) for value in (office_id, task_id, attempt_id))
        encoded_budget = json.dumps(asdict(budget), sort_keys=True)
        encoded_spec = json.dumps(launch_spec, sort_keys=True) if launch_spec is not None else ""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM execution_containers WHERE attempt_id=?", (attempt_id,)).fetchone()
            if existing is not None:
                if any(existing[key] != value for key, value in {
                    "office_id": office_id, "task_id": task_id, "fingerprint": fingerprint,
                    "office_container_id": office_container_id, "image_id": image_id,
                    "spec_json": encoded_spec,
                    **asdict(resources),
                }.items()):
                    raise ExecutionAdmissionError("An execution attempt cannot change its retained launch identity")
                return dict(existing), False
            if connection.execute("SELECT 1 FROM execution_containers WHERE office_id=? AND task_id=? AND state!='stopped' LIMIT 1", (office_id, task_id)).fetchone():
                raise ExecutionCapacityUnavailable("This task already has an unresolved isolated execution")
            current_budget = connection.execute("SELECT budget FROM execution_budgets WHERE office_id=?", (office_id,)).fetchone()
            usage = connection.execute(
                "SELECT COUNT(*) AS workers, COALESCE(SUM(cpu_millis),0) AS cpu_millis, "
                "COALESCE(SUM(memory_bytes),0) AS memory_bytes, COALESCE(SUM(pids),0) AS pids "
                "FROM execution_containers WHERE office_id=? AND state!='stopped'", (office_id,),
            ).fetchone()
            if current_budget is not None and current_budget["budget"] != encoded_budget and usage["workers"]:
                raise ExecutionCapacityUnavailable("Drain retained executions before changing their aggregate budget")
            if usage["workers"] >= budget.max_workers or any(usage[key] + value > getattr(budget, key) for key, value in asdict(resources).items()):
                raise ExecutionCapacityUnavailable("Isolated execution budget is full, including unconfirmed reservations")
            connection.execute("INSERT INTO execution_budgets VALUES (?, ?) ON CONFLICT(office_id) DO UPDATE SET budget=excluded.budget", (office_id, encoded_budget))
            now = time.time()
            connection.execute(
                "INSERT INTO execution_containers (office_id,task_id,attempt_id,fingerprint,reservation_id,office_container_id,image_id,container_id,state,cpu_millis,memory_bytes,pids,created_at,updated_at,spec_json) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'reserved', ?, ?, ?, ?, ?, ?)",
                (office_id, task_id, attempt_id, fingerprint, str(uuid.uuid4()), office_container_id, image_id,
                 resources.cpu_millis, resources.memory_bytes, resources.pids, now, now, encoded_spec),
            )
            return dict(connection.execute("SELECT * FROM execution_containers WHERE attempt_id=?", (attempt_id,)).fetchone()), True

    def bind_container(self, attempt_id: str, reservation_id: str, container_id: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise ValueError("An immutable full container ID is required")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM execution_containers WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["reservation_id"] != reservation_id or row["state"] == "stopped":
                raise ExecutionAdmissionError("Execution reservation is no longer available")
            if row["container_id"] not in (None, container_id):
                raise ExecutionAdmissionError("Execution reservation already belongs to another container")
            connection.execute("UPDATE execution_containers SET container_id=?, updated_at=? WHERE attempt_id=?", (container_id, time.time(), attempt_id))
            return dict(connection.execute("SELECT * FROM execution_containers WHERE attempt_id=?", (attempt_id,)).fetchone())

    def transition(self, attempt_id: str, container_id: str | None, state: str) -> None:
        if state not in {"created", "starting", "running_unready", "running", "uncertain", "stopped"}:
            raise ValueError("Unknown execution container state")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM execution_containers WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["container_id"] != container_id:
                raise ExecutionAdmissionError("Execution state update does not own the retained container")
            if row["state"] == "stopped" and state != "stopped":
                raise ExecutionAdmissionError("A terminated execution cannot be resurrected")
            if state == "stopped" and not container_id:
                raise ExecutionAdmissionError("A missing container is not termination proof")
            connection.execute("UPDATE execution_containers SET state=?, updated_at=? WHERE attempt_id=?", (state, time.time(), attempt_id))

    def get(self, attempt_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM execution_containers WHERE attempt_id=?", (attempt_id,)).fetchone()
            return dict(row) if row else None

    def unresolved(self, office_id: str) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM execution_containers WHERE office_id=? AND state!='stopped' ORDER BY created_at", (office_id,)).fetchall()]

    def task_available(self, office_id: str, task_id: str) -> bool:
        with self.connection() as connection:
            return connection.execute("SELECT 1 FROM execution_containers WHERE office_id=? AND task_id=? AND state!='stopped' LIMIT 1", (office_id, task_id)).fetchone() is None

    def available(self, office_id: str, resources: ExecutionResources, budget: ExecutionBudget) -> bool:
        rows = self.unresolved(office_id)
        return len(rows) < budget.max_workers and all(sum(row[key] for row in rows) + value <= getattr(budget, key) for key, value in asdict(resources).items())


def read_execution_inventory(database_path: Path, office_id: str | None = None) -> list[dict]:
    """Read unresolved ownership without creating or migrating a database."""
    database_path = Path(database_path).absolute()
    if not database_path.exists() and not database_path.is_symlink():
        return []
    for ancestor in (database_path, *database_path.parents):
        if ancestor.is_symlink():
            raise ExecutionAdmissionError("Execution inventory must not traverse symbolic links")
    parent_metadata = database_path.parent.stat()
    if parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) & 0o077:
        raise ExecutionAdmissionError("Execution inventory requires a private host-owned directory")
    descriptor = os.open(database_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ExecutionAdmissionError("Execution inventory ownership is unsafe")
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        selection = "SELECT office_id,task_id,attempt_id,container_id,state,cpu_millis,memory_bytes,pids,created_at,updated_at FROM execution_containers WHERE state!='stopped'"
        if office_id is not None:
            rows = connection.execute(selection + " AND office_id=?", (office_id,)).fetchall()
        else:
            rows = connection.execute(selection).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
