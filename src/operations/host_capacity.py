"""Optional admission for managed operations sharing one physical host/service.

These are configured operation costs, not inferred CPU reservations. All daemons
sharing a resource must use the same private ledger and policy. Process ownership
remains with the script/operation ledger; elapsed time never releases capacity.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time

import yaml


class HostCapacityUnavailable(RuntimeError):
    """No process was admitted; retain the operation's queue identity for retry."""


WAIT_PRIORITY_SECONDS = 300


def _positive(value: object) -> bool:
    return type(value) is int and 1 <= value <= 100000


def validate_policy(policy: dict) -> dict:
    allowed = {"enabled", "budgets", "default_costs", "resource_costs"}
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise ValueError("Invalid host_capacity policy")
    if type(policy.get("enabled", False)) is not bool:
        raise ValueError("host_capacity.enabled must be boolean")
    budgets = policy.get("budgets", {})
    if not isinstance(budgets, dict) or not 1 <= len(budgets) <= 32:
        raise ValueError("Configure 1..32 host_capacity budgets")
    for name, budget in budgets.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise ValueError("Invalid budget name")
        if not isinstance(budget, dict) or set(budget) != {"limit", "per_office"}:
            raise ValueError("Each budget requires limit and per_office")
        if not all(_positive(budget[key]) for key in budget):
            raise ValueError("Budget limits must be positive integers")
        if budget["per_office"] > budget["limit"]:
            raise ValueError("Per-office budget exceeds shared budget")
    mappings = policy.get("resource_costs", {})
    if not isinstance(mappings, dict) or len(mappings) > 128:
        raise ValueError("Invalid resource_costs mapping")
    if any(not isinstance(key, str) or not 1 <= len(key) <= 120 for key in mappings):
        raise ValueError("Invalid execution resource name")
    for costs in [policy.get("default_costs"), *mappings.values()]:
        if not isinstance(costs, dict) or not costs or set(costs) - budgets.keys():
            raise ValueError("Costs must name configured budgets")
        for name, cost in costs.items():
            if not _positive(cost) or cost > budgets[name]["per_office"]:
                raise ValueError("An operation cost exceeds its per-office budget")
    return json.loads(json.dumps(policy, sort_keys=True))


def load_host_capacity(config_path: Path, database_path: Path):
    data = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("Communicator configuration must be a mapping")
    policy = data.get("host_capacity")
    if policy is None or (
        isinstance(policy, dict) and policy.get("enabled", False) is False
    ):
        # A disabled/new process cannot bypass held resources or orphan queued
        # intents by completing them outside their original capacity ledger.
        # Opening the existing ledger is read-only here.
        if database_path.exists():
            with sqlite3.connect(
                f"{database_path.absolute().as_uri()}?mode=ro", uri=True
            ) as connection:
                if connection.execute(
                    "SELECT 1 FROM capacity_leases WHERE state!='released' LIMIT 1"
                ).fetchone():
                    raise ValueError(
                        "Unsettled host capacity prevents disabling its policy"
                    )
        return None
    return HostCapacity(database_path, validate_policy(policy))


class HostCapacity:
    def __init__(self, database_path: Path, policy: dict):
        self.database_path = Path(database_path).absolute()
        self.policy = validate_policy(policy)
        if any(
            path.is_symlink()
            for path in [self.database_path, *self.database_path.parents]
        ):
            raise ValueError("Capacity ledger cannot traverse symbolic links")
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        import os
        import stat

        descriptor = os.open(
            self.database_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
            ):
                raise ValueError("Unsafe capacity ledger ownership")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS capacity_policy (id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_leases (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE, office_id TEXT NOT NULL,
                    resources TEXT NOT NULL, costs TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS capacity_leases_state_sequence ON capacity_leases(state,sequence);
                CREATE INDEX IF NOT EXISTS capacity_leases_office_state_id ON capacity_leases(office_id,state,operation_id);
                CREATE INDEX IF NOT EXISTS capacity_leases_wait_freshness ON capacity_leases(state,updated_at);
            """
            )
            connection.execute("BEGIN IMMEDIATE")
            encoded = json.dumps(self.policy, sort_keys=True)
            previous = connection.execute(
                "SELECT policy FROM capacity_policy WHERE id=1"
            ).fetchone()
            if previous and previous[0] != encoded:
                if connection.execute(
                    "SELECT 1 FROM capacity_leases WHERE state!='released' LIMIT 1"
                ).fetchone():
                    raise ValueError(
                        "Settle host capacity reservations and waiters before changing policy"
                    )
            connection.execute(
                "INSERT OR REPLACE INTO capacity_policy VALUES (1, ?)", (encoded,)
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _costs(self, resources: list[str] | None) -> dict:
        if resources is None:
            resources = ["shared-workspace"]
        if (
            not isinstance(resources, list)
            or len(resources) > 16
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", value) is None
                for value in resources
            )
            or len(set(resources)) != len(resources)
        ):
            raise ValueError("Invalid declared execution resources")
        selected = [
            self.policy["default_costs"],
            *[
                self.policy.get("resource_costs", {})[value]
                for value in resources
                if value in self.policy.get("resource_costs", {})
            ],
        ]
        # Several declarations may describe the same physical operation.
        return {
            name: max(costs.get(name, 0) for costs in selected)
            for name in set().union(*selected)
        }

    def _assert_policy(self, connection) -> None:
        # Old daemon objects cannot use an obsolete in-memory policy.
        current = connection.execute(
            "SELECT policy FROM capacity_policy WHERE id=1"
        ).fetchone()
        if current is None or current[0] != json.dumps(self.policy, sort_keys=True):
            raise ValueError("Host capacity policy changed; reload before admission")

    @staticmethod
    def _ensure_priority_slot(connection, cutoff: float) -> None:
        if (
            connection.execute(
                "SELECT COUNT(*) FROM capacity_leases WHERE state='waiting' AND updated_at>=?",
                (cutoff,),
            ).fetchone()[0]
            >= 10000
        ):
            raise HostCapacityUnavailable(
                "Shared capacity waiting inventory is full; no process started"
            )

    def _renew_wait_position(self, connection, row, now: float):
        if row["updated_at"] < now - WAIT_PRIORITY_SECONDS:
            self._ensure_priority_slot(connection, now - WAIT_PRIORITY_SECONDS)
            # A never-started intent retains identity, not abandoned priority.
            connection.execute(
                "UPDATE capacity_leases SET sequence=(SELECT COALESCE(MAX(sequence),0)+1 FROM capacity_leases),updated_at=? WHERE operation_id=?",
                (now, row["operation_id"]),
            )
        else:
            connection.execute(
                "UPDATE capacity_leases SET updated_at=? WHERE operation_id=?",
                (now, row["operation_id"]),
            )
        return connection.execute(
            "SELECT * FROM capacity_leases WHERE operation_id=?", (row["operation_id"],)
        ).fetchone()

    def _eligible(self, connection, row, priority_cutoff: float) -> bool:
        """One admission calculation for atomic reserve and observational wake."""
        operation_id, office_id = row["operation_id"], row["office_id"]
        costs = json.loads(row["costs"])
        refused = False
        # At most 10,000 fresh priority holders exist. Restrict aggregation
        # to this candidate and relevant older waiters; never rescan every
        # reservation once per waiter (quadratic under busy multi-office use).
        waiters = [
            waiter
            for waiter in connection.execute(
                "SELECT office_id,costs FROM capacity_leases WHERE state='waiting' AND sequence<? AND updated_at>=?",
                (row["sequence"], priority_cutoff),
            )
            if costs.keys() & json.loads(waiter["costs"]).keys()
        ]
        totals = dict.fromkeys(self.policy["budgets"], 0)
        occupied_by_office = {
            office: dict.fromkeys(self.policy["budgets"], 0)
            for office in {office_id, *(waiter["office_id"] for waiter in waiters)}
        }
        for held in connection.execute(
            "SELECT office_id,costs FROM capacity_leases WHERE state='reserved' AND operation_id!=?",
            (operation_id,),
        ):
            held_costs = json.loads(held["costs"])
            for name, amount in held_costs.items():
                totals[name] += amount
                if held["office_id"] in occupied_by_office:
                    occupied_by_office[held["office_id"]][name] += amount
        office_totals = occupied_by_office[office_id]
        for waiter in waiters:
            waiting_costs = json.loads(waiter["costs"])
            # Eligibility includes ALL budgets needed by that waiter. A
            # globally busy provider must not idle otherwise available CPU.
            occupied = occupied_by_office[waiter["office_id"]]
            blocked = any(
                occupied[name] + cost > self.policy["budgets"][name]["per_office"]
                or totals[name] + cost > self.policy["budgets"][name]["limit"]
                for name, cost in waiting_costs.items()
            )
            if not blocked:
                refused = True
        for name, cost in costs.items():
            budget = self.policy["budgets"][name]
            refused |= (
                totals[name] + cost > budget["limit"]
                or office_totals[name] + cost > budget["per_office"]
            )
        return not refused

    @staticmethod
    def _owned_row(connection, operation_id: str, office_id: str):
        if not all(
            isinstance(value, str) and 1 <= len(value) <= 200
            for value in (operation_id, office_id)
        ):
            raise ValueError("Bounded operation and office identities are required")
        row = connection.execute(
            "SELECT * FROM capacity_leases WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is not None:
            if row["office_id"] != office_id:
                raise ValueError("Operation capacity identity cannot be reused")
            if row["state"] not in {"waiting", "reserved", "released"}:
                raise ValueError(
                    "Unknown capacity ownership state; reconcile before admission"
                )
        return row

    def wait_eligibility(self, operation_id: str, office_id: str) -> dict:
        """Read one consistent queue snapshot; never renew, reserve or launch."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            self._assert_policy(connection)
            row = self._owned_row(connection, operation_id, office_id)
            if row is None:
                return {"state": "missing", "eligible": False}
            cutoff = time.time() - WAIT_PRIORITY_SECONDS
            eligible = (
                row["state"] == "waiting"
                and row["updated_at"] >= cutoff
                and self._eligible(connection, row, cutoff)
            )
            return {"state": row["state"], "eligible": bool(eligible)}

    def renew_wait(self, operation_id: str, office_id: str) -> bool:
        """Renew existing never-started intent after caller validates durable lineage.

        A late renewal rejoins at the tail. This never creates an intent, acquires
        capacity or releases a holder; expired operation ownership is not inferred.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_policy(connection)
            row = self._owned_row(connection, operation_id, office_id)
            if row is None or row["state"] != "waiting":
                return False
            self._renew_wait_position(connection, row, time.time())
            return True

    def reserve(
        self, operation_id: str, office_id: str, declared_resources: list[str] | None
    ) -> dict:
        if not all(
            isinstance(value, str) and 1 <= len(value) <= 200
            for value in (operation_id, office_id)
        ):
            raise ValueError("Bounded operation and office identities are required")
        costs = self._costs(declared_resources)
        resources = json.dumps(
            sorted(declared_resources) if declared_resources is not None else None
        )
        refused = False
        now = time.time()
        priority_cutoff = now - WAIT_PRIORITY_SECONDS
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_policy(connection)
            row = connection.execute(
                "SELECT * FROM capacity_leases WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row:
                if row["office_id"] != office_id or row["resources"] != resources:
                    raise ValueError("Operation capacity identity cannot be reused")
                if row["state"] == "reserved":
                    return {
                        "operation_id": operation_id,
                        "state": "reserved",
                        "costs": costs,
                    }
                if row["state"] == "released":
                    raise ValueError("A released operation cannot be relaunched")
                if row["state"] != "waiting":
                    raise ValueError(
                        "Unknown capacity ownership state; reconcile before admission"
                    )
            if row is not None:
                row = self._renew_wait_position(connection, row, now)
            else:
                self._ensure_priority_slot(connection, priority_cutoff)
                connection.execute(
                    "INSERT INTO capacity_leases (operation_id,office_id,resources,costs,state,created_at,updated_at) VALUES (?,?,?,?, 'waiting',?,?)",
                    (
                        operation_id,
                        office_id,
                        resources,
                        json.dumps(costs),
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM capacity_leases WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            refused = not self._eligible(connection, row, priority_cutoff)
            if not refused:
                connection.execute(
                    "UPDATE capacity_leases SET state='reserved',updated_at=? WHERE operation_id=?",
                    (time.time(), operation_id),
                )
        if refused:
            raise HostCapacityUnavailable(
                "Managed operation queued for shared host/service capacity; no process started"
            )
        return {"operation_id": operation_id, "state": "reserved", "costs": costs}

    def release(
        self, operation_id: str, office_id: str, *, cleanup_confirmed: bool
    ) -> None:
        if cleanup_confirmed is not True:
            raise ValueError("Capacity stays reserved until owned cleanup is confirmed")
        with self._connection() as connection:
            connection.execute(
                "UPDATE capacity_leases SET state='released',updated_at=? WHERE operation_id=? AND office_id=?",
                (time.time(), operation_id, office_id),
            )

    def abandon_wait(self, operation_id: str, office_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE capacity_leases SET state='released',updated_at=? WHERE operation_id=? AND office_id=? AND state='waiting'",
                (time.time(), operation_id, office_id),
            )

    def status(self) -> dict:
        with self._connection() as connection:
            counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state,COUNT(*) AS count FROM capacity_leases GROUP BY state"
                )
            }
            rows = connection.execute(
                "SELECT operation_id,office_id,state,created_at,costs FROM capacity_leases WHERE state!='released' ORDER BY sequence LIMIT 100"
            ).fetchall()
        return {
            "budgets": self.policy["budgets"],
            "counts": counts,
            "operations": [
                dict(row) | {"costs": json.loads(row["costs"])} for row in rows
            ],
            "operations_limit": 100,
        }

    def list_reserved_operations(
        self, office_id: str, *, after_id: str | None = None, limit: int = 100
    ) -> list[str]:
        """Bounded keyset scan for recovery; no release or lease expiry by age."""
        if (
            not isinstance(office_id, str)
            or not 1 <= len(office_id) <= 200
            or type(limit) is not int
            or not 1 <= limit <= 100
            or (
                after_id is not None
                and (not isinstance(after_id, str) or not 1 <= len(after_id) <= 200)
            )
        ):
            raise ValueError("Bounded office, cursor and limit are required")
        with self._connection() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT operation_id FROM capacity_leases WHERE office_id=? AND state='reserved' "
                    "AND (? IS NULL OR operation_id>?) ORDER BY operation_id LIMIT ?",
                    (office_id, after_id, after_id, limit),
                )
            ]
