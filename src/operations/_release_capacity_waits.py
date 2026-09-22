"""Bounded release observations of deferred model intents, never admission."""

from __future__ import annotations


def capacity_wait_inventory(connection, schema: dict) -> dict:
    """Read within the caller's snapshot; absent is supported only before init."""
    empty = {
        "schema_present": False,
        "counts": {},
        "active_offices": [],
        "active_offices_complete": True,
        "active_waits": [],
        "active_waits_limit": 100,
        "in_flight_resume_count": 0,
    }
    if "capacity_waits" not in schema:
        return empty
    fields = (
        "office_id",
        "task_id",
        "wait_id",
        "operation_id",
        "cycle",
        "generation",
        "epoch",
        "phase",
        "agent_name",
        "attempt_id",
        "pending_resume_attempt_id",
        "state",
        "next_check_at",
        "updated_at",
    )
    if not set(fields) <= schema["capacity_waits"]:
        raise ValueError("Capacity wait inspection schema is incomplete")
    if connection.execute(
        "SELECT 1 FROM capacity_waits WHERE state IS NULL OR state NOT IN "
        "('waiting','resuming','resumed','retired') LIMIT 1"
    ).fetchone():
        raise ValueError("Unknown capacity wait state requires reconciliation")
    counts = dict(
        connection.execute("SELECT state,COUNT(*) FROM capacity_waits GROUP BY state")
    )
    offices = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT office_id FROM capacity_waits WHERE state IN ('waiting','resuming') "
            "ORDER BY office_id LIMIT 1001"
        )
    ]
    rows = connection.execute(
        "SELECT " + ",".join(fields) + " FROM capacity_waits "
        "WHERE state IN ('waiting','resuming') ORDER BY office_id,task_id LIMIT 100"
    ).fetchall()
    in_flight = connection.execute(
        "SELECT COUNT(*) FROM capacity_waits WHERE state='resuming' "
        "OR (state='waiting' AND pending_resume_attempt_id IS NOT NULL)"
    ).fetchone()[0]
    return {
        **empty,
        "schema_present": True,
        "counts": counts,
        "active_offices": offices[:1000],
        "active_offices_complete": len(offices) <= 1000,
        "active_waits": [dict(zip(fields, row, strict=True)) for row in rows],
        "in_flight_resume_count": in_flight,
    }


def require_wait_office_inventory(inventory: dict, expected_offices: set[str]) -> None:
    if (
        not inventory["active_offices_complete"]
        or not set(inventory["active_offices"]) <= expected_offices
    ):
        raise ValueError("Expected health inventory omits active capacity-wait offices")
