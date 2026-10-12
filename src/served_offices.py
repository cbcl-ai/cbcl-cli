"""Which offices this machine's cbcl serves, for the maintenance drain.

``cbcl maintenance enable --wait`` without ``--office-id`` waits for every
office that can still have work here. That inventory used to be "every
office this machine ever reported a snapshot for", and snapshots were never
removed: an office that was deleted, moved to another Company Token or
parked stayed in it without a fresh snapshot, so its state was ``unknown``
forever and the documented drain could never be confirmed again.

``served_offices`` is the durable answer to "does this cbcl serve the
office now". A row is added when the daemon brings an office up and removed
when the platform no longer assigns the office to this daemon's token. It
survives a daemon stop on purpose: a stopped cbcl still owns its offices.

Forgetting an office removes only what this machine observed about it: the
served row, its last snapshot, and the per-office maintenance pause with its
explanation. Admissions, worker journals and the isolated-execution ledger
are records of work, not observations, and are never touched here: an
office that still owns work keeps blocking the drain until that work is
reconciled.
"""

from __future__ import annotations

import time

# The scope of the global maintenance pause. It is not an office and must
# never be marked served or forgotten.
GLOBAL_SCOPE = "*"


class ServedOfficeStateMixin:
    def initialize_served_offices(self) -> None:
        with self._connection() as connection:
            upgrading = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='served_offices'"
            ).fetchone() is None
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS served_offices (
                    office_id TEXT PRIMARY KEY, served_since REAL NOT NULL
                )
                """
            )
            if upgrading:
                # A database written by cbcl 0.5.36 or earlier: until the
                # daemon's first successful discovery says otherwise, every
                # office it reported for stays in the inventory, so nothing
                # disappears from ``cbcl maintenance status`` by upgrading.
                connection.execute(
                    "INSERT OR IGNORE INTO served_offices "
                    "SELECT office_id, ? FROM runtime_snapshots",
                    (time.time(),),
                )

    def mark_served(self) -> None:
        """Record that the daemon serves this instance's office."""
        _require_office(self.office_id)
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO served_offices VALUES (?, ?)",
                (self.office_id, time.time()),
            )

    def served_office_ids(self) -> set[str]:
        with self._connection() as connection:
            return {
                row[0] for row in connection.execute("SELECT office_id FROM served_offices")
            }

    def forget_office(self, office_id: str) -> None:
        """Drop what this machine observed about an office it no longer serves.

        Also ends the office's own maintenance pause: it was this machine's
        pause for an office the machine no longer has, and it would silently
        stall the office if it were ever assigned back. The global pause is
        untouched.
        """
        from src.operations.maintenance import clear_annotation

        office_id = str(office_id)
        _require_office(office_id)
        with self._connection() as connection:
            for statement in (
                "DELETE FROM served_offices WHERE office_id=?",
                "DELETE FROM runtime_snapshots WHERE office_id=?",
                "DELETE FROM snapshot_owners WHERE office_id=?",
                "DELETE FROM maintenance WHERE scope=?",
            ):
                connection.execute(statement, (office_id,))
        clear_annotation(
            self.database_path.with_name("maintenance-annotations.sqlite3"), office_id,
        )


def _require_office(office_id: str) -> None:
    if not office_id or office_id == GLOBAL_SCOPE:
        raise ValueError("An office id is required; the global scope is not an office")
