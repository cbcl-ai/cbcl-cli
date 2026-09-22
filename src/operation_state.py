"""Office-scoped operation identity; results are evidence, never task approval.

This extends the existing script lane. Only hashes and bounded references belong
here: script variables, credentials, response bodies and logs stay out of this DB.
"""

from __future__ import annotations

import json
import re
import time
import uuid


class OperationConflict(RuntimeError):
    """An existing intent or resource must be reconciled before new work."""


def operation_key(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", value):
        raise ValueError("Operation keys must be bounded nonsecret identifiers")
    return value


def operation_spec(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"key", "input_fingerprint", "resources", "stage"}:
        raise ValueError("Operation needs key, input_fingerprint and optional resources/stage")
    key = operation_key(value.get("key"))
    fingerprint = value.get("input_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("input_fingerprint must be a SHA-256 of relevant nonsecret inputs")
    resources = value.get("resources", [])
    if not isinstance(resources, list) or len(resources) > 16:
        raise ValueError("Operation resources must be a bounded list")
    stage = value.get("stage")
    if stage is not None and (not isinstance(stage, str) or stage not in {"preparation", "execution", "verification"}):
        raise ValueError("Operation stage must be preparation, execution or verification")
    return {"key": key, "input_fingerprint": fingerprint,
            "resources": sorted({operation_key(resource) for resource in resources}), "stage": stage}


def operation_result(value: object) -> dict:
    """Validate an adapter receipt, never arbitrary provider metadata."""
    if not isinstance(value, dict) or set(value) - {"external_ref", "state", "artifact_refs"}:
        raise ValueError("Invalid operation receipt fields")
    result = {}
    if "external_ref" in value:
        ref = value["external_ref"]
        if not isinstance(ref, dict) or set(ref) != {"service", "run_id"}:
            raise ValueError("External reference requires nonsecret service and run_id")
        result["external_ref"] = {key: operation_key(item) for key, item in ref.items()}
    if "state" in value:
        if not isinstance(value["state"], str) or value["state"] not in {"running", "succeeded", "failed", "cancelled", "unknown"}:
            raise ValueError("Invalid adapter outcome")
        result["state"] = value["state"]
    refs = value.get("artifact_refs", [])
    if not isinstance(refs, list) or len(refs) > 32:
        raise ValueError("Too many artifact references")
    for ref in refs:
        if (not isinstance(ref, str) or not ref or len(ref) > 500
                or ref.startswith("/") or ".." in ref.split("/")
                or any(c in ref for c in "\\\r\n\x00") or ":" in ref):
            raise ValueError("Artifacts must be relative nonsecret workspace references")
    result["artifact_refs"] = refs
    return result


class OperationStateMixin:
    def initialize_operations(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS managed_operations (
                    office_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, cycle INTEGER NOT NULL, phase TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'execution',
                    operation_key TEXT NOT NULL, fingerprint TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
                    observer_fingerprint TEXT,
                    script_name TEXT NOT NULL, origin_attempt_id TEXT NOT NULL, observer_attempt_id TEXT NOT NULL,
                    mechanism TEXT NOT NULL, resources TEXT NOT NULL,
                    execution_id TEXT, state TEXT NOT NULL,
                    cleanup_confirmed INTEGER NOT NULL DEFAULT 0,
                    external_ref TEXT, artifact_refs TEXT NOT NULL DEFAULT '[]',
                    exit_code INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY (office_id, operation_id),
                    UNIQUE (office_id, task_id, cycle, phase, operation_key)
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(managed_operations)")}
            if "stage" not in columns:
                connection.execute("ALTER TABLE managed_operations ADD COLUMN stage TEXT NOT NULL DEFAULT 'execution'")
                connection.execute("UPDATE managed_operations SET stage='verification' WHERE phase='review'")
            if "observer_fingerprint" not in columns:
                connection.execute("ALTER TABLE managed_operations ADD COLUMN observer_fingerprint TEXT")

    @staticmethod
    def _operation_row(row) -> dict:
        item = dict(row)
        for field in ("resources", "artifact_refs", "external_ref"):
            item[field] = json.loads(item[field]) if item[field] else None
        item["cleanup_confirmed"] = bool(item["cleanup_confirmed"])
        return item

    def get_operation(self, operation_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM managed_operations WHERE office_id=? AND operation_id=?",
                (self.office_id, operation_id),
            ).fetchone()
        return self._operation_row(row) if row else None

    def list_operations(self, task_id: str, *, limit: int = 100) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM managed_operations WHERE office_id=? AND task_id=? "
                "ORDER BY updated_at DESC LIMIT ?", (self.office_id, task_id, min(max(limit, 1), 100)),
            ).fetchall()
        return [self._operation_row(row) for row in rows]

    def operations_requiring_recovery(self, *, after_id: str | None = None, limit: int = 100) -> list[str]:
        """Bounded startup inventory; settled historical results need no scan."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM managed_operations WHERE office_id=? AND operation_id>? "
                "AND (cleanup_confirmed=0 OR state IN ('preparing','running')) "
                "ORDER BY operation_id LIMIT ?",
                (self.office_id, after_id or "", min(max(limit, 1), 100)),
            ).fetchall()
        return [row[0] for row in rows]

    def begin_operation(self, *, task_id: str, cycle: int, phase: str,
                        key: str, fingerprint: str, script_name: str,
                        attempt_id: str, mechanism: str, resources: list[str],
                        input_fingerprint: str | None = None, stage: str | None = None) -> tuple[dict, bool]:
        if phase not in {"execute", "review", "triage"} or not task_id or not attempt_id:
            raise ValueError("A task-owned operation requires a current phase and attempt")
        stage = stage or ("verification" if phase == "review" else "execution")
        if stage not in {"preparation", "execution", "verification"}:
            raise ValueError("Unknown declared operation stage")
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM managed_operations WHERE office_id=? AND task_id=? AND cycle=? "
                "AND phase=? AND operation_key=?", (self.office_id, task_id, cycle, phase, key),
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint or row["script_name"] != script_name:
                    raise OperationConflict("Operation inputs changed; inspect the original run and use a new intent key")
                if row["state"] != "queued" or not row["cleanup_confirmed"]:
                    return self._operation_row(row), False
            for existing in connection.execute(
                "SELECT resources FROM managed_operations WHERE office_id=? AND operation_id!=? AND "
                "(cleanup_confirmed=0 OR state IN ('preparing','running') OR (state='unknown' AND mechanism='external'))",
                (self.office_id, row["operation_id"] if row else ""),
            ):
                if set(resources).intersection(json.loads(existing["resources"])):
                    raise OperationConflict("Another operation owns a requested resource; inspect its result/cleanup first")
            if row:
                connection.execute(
                    "UPDATE managed_operations SET state='preparing',cleanup_confirmed=0,updated_at=? "
                    "WHERE office_id=? AND operation_id=?", (now, self.office_id, row["operation_id"]),
                )
                return {**self._operation_row(row), "state": "preparing", "cleanup_confirmed": False}, True
            operation_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO managed_operations (office_id,operation_id,task_id,cycle,phase,operation_key,"
                "fingerprint,input_fingerprint,script_name,origin_attempt_id,observer_attempt_id,mechanism,resources,stage,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'preparing',?,?)",
                (self.office_id, operation_id, task_id, cycle, phase, key, fingerprint, input_fingerprint or fingerprint,
                 script_name, attempt_id, attempt_id, mechanism, json.dumps(resources), stage, now, now),
            )
        return self.get_operation(operation_id), True

    def update_operation(self, operation_id: str, *, execution_id: str | None = None,
                         state: str | None = None, cleanup_confirmed: bool | None = None,
                         exit_code: int | None = None, receipt: dict | None = None,
                         observer_attempt_id: str | None = None, observer_fingerprint: str | None = None) -> dict:
        if state is not None and state not in {"queued", "preparing", "running", "succeeded", "failed", "cancelled", "unknown"}:
            raise ValueError("Invalid operation state")
        changes = {"updated_at": time.time()}
        if execution_id is not None:
            changes["execution_id"] = execution_id
        if observer_attempt_id is not None:
            changes["observer_attempt_id"] = observer_attempt_id
        if observer_fingerprint is not None:
            changes["observer_fingerprint"] = observer_fingerprint
        if state is not None:
            changes["state"] = state
        if cleanup_confirmed is not None:
            changes["cleanup_confirmed"] = int(cleanup_confirmed)
        if exit_code is not None:
            changes["exit_code"] = exit_code
        if receipt is not None:
            validated = operation_result(receipt)
            for field in ("external_ref", "artifact_refs"):
                if field in validated:
                    changes[field] = json.dumps(validated[field], sort_keys=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM managed_operations WHERE office_id=? AND operation_id=?",
                (self.office_id, operation_id),
            ).fetchone()
            if existing is None:
                raise ValueError("Unknown operation")
            if (existing["external_ref"] and changes.get("external_ref")
                    and existing["external_ref"] != changes["external_ref"]):
                raise OperationConflict("An operation cannot switch its external run identity")
            if all(existing[key] == value for key, value in changes.items() if key != "updated_at"):
                return self._operation_row(existing)
            connection.execute(
                "UPDATE managed_operations SET " + ",".join(f"{key}=?" for key in changes)
                + " WHERE office_id=? AND operation_id=?",
                (*changes.values(), self.office_id, operation_id),
            )
        return self.get_operation(operation_id)

    def claim_operation_observer(self, operation_id: str) -> dict:
        """One reconciliation/cancellation observer; never erase remote ownership."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM managed_operations WHERE office_id=? AND operation_id=?",
                (self.office_id, operation_id),
            ).fetchone()
            if row is None or not row["cleanup_confirmed"] or row["state"] not in {"unknown", "running"}:
                raise OperationConflict("Reconcile the existing observer cleanup before starting another")
            connection.execute(
                "UPDATE managed_operations SET state='preparing',cleanup_confirmed=0,"
                "execution_id=NULL,exit_code=NULL,updated_at=? "
                "WHERE office_id=? AND operation_id=?", (time.time(), self.office_id, operation_id),
            )
        return self.get_operation(operation_id)

    def cancel_queued_operation(self, operation_id: str) -> dict:
        """Cancel only a never-started intent, atomically against same-key retry."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM managed_operations WHERE office_id=? AND operation_id=?",
                (self.office_id, operation_id),
            ).fetchone()
            if (row is None or row["state"] != "queued" or not row["cleanup_confirmed"]
                    or row["external_ref"] or row["execution_id"]):
                raise OperationConflict("Queued operation changed; inspect the current owned run before cancelling")
            connection.execute(
                "UPDATE managed_operations SET state='cancelled',updated_at=? WHERE office_id=? AND operation_id=?",
                (time.time(), self.office_id, operation_id),
            )
        return self.get_operation(operation_id)
