"""Private durable reservations for scripts that may outlive their worker."""

import json


class ScriptResourceConflict(RuntimeError):
    """A declared resource is still owned by another physical execution."""


class ScriptResourceStateMixin:
    def initialize_script_resources(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS script_resource_leases (
                    office_id TEXT NOT NULL, lease_id TEXT NOT NULL,
                    script_name TEXT NOT NULL, task_id TEXT NOT NULL,
                    parent_attempt_id TEXT NOT NULL, resources TEXT NOT NULL,
                    state TEXT NOT NULL, execution_id TEXT NOT NULL,
                    marker TEXT NOT NULL, container_id TEXT NOT NULL,
                    preparation_started INTEGER NOT NULL DEFAULT 0,
                    launch_started INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (office_id, lease_id)
                )
            """
            )

    def begin_script_resource_lease(
        self,
        *,
        lease_id: str,
        script_name: str,
        task_id: str,
        parent_attempt_id: str,
        resources: list[str],
        execution_id: str,
        marker: str,
        container_id: str,
    ) -> None:
        from src.agent_execution_policy import execution_resources

        resources = execution_resources({"execution_resources": resources})
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT resources FROM script_resource_leases WHERE office_id=? AND state != 'released'",
                (self.office_id,),
            ).fetchall()
            if any(
                set(resources).intersection(json.loads(row["resources"]))
                for row in existing
            ):
                raise ScriptResourceConflict(
                    "Script launch deferred: another script still owns a shared resource. "
                    "Wait for its confirmed completion or stop, then retry."
                )
            connection.execute(
                "INSERT INTO script_resource_leases (office_id, lease_id, script_name, task_id, parent_attempt_id, resources, state, execution_id, marker, container_id) VALUES (?, ?, ?, ?, ?, ?, 'preparing', ?, ?, ?)",
                (
                    self.office_id,
                    lease_id,
                    script_name,
                    task_id,
                    parent_attempt_id,
                    json.dumps(resources),
                    execution_id,
                    marker,
                    container_id,
                ),
            )

    def set_script_resource_state(self, lease_id: str, state: str) -> None:
        if state not in {
            "preparing",
            "launching",
            "running",
            "uncertain",
            "stopped",
            "released",
        }:
            raise ValueError("Invalid script resource state")
        with self._connection() as connection:
            connection.execute(
                "UPDATE script_resource_leases SET state=? WHERE office_id=? AND lease_id=? AND state != 'released'",
                (state, self.office_id, lease_id),
            )

    def mark_script_resource_launch(
        self, lease_id: str, *, preparation: bool = False, started: bool = True
    ) -> None:
        field = "preparation_started" if preparation else "launch_started"
        with self._connection() as connection:
            connection.execute(
                f"UPDATE script_resource_leases SET {field}=? WHERE office_id=? AND lease_id=? AND state != 'released'",
                (int(started), self.office_id, lease_id),
            )

    def active_script_resources(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM script_resource_leases WHERE office_id=? AND state != 'released' ORDER BY rowid",
                (self.office_id,),
            ).fetchall()
        return [
            {**dict(row), "resources": json.loads(row["resources"])} for row in rows
        ]
