"""Per-slug office-lifecycle locks.

Container names (``cbcl-office-{slug}``) and every piece of per-office
host state (workspace dir, ``.claude-auth/`` backing, office-secrets
file) are keyed by the office NAME's slug — not the immutable
office_id. Deleting an office and immediately creating a new one with
the same name therefore races two independent code paths over the SAME
container name and the SAME host directories:

  * the ``office_deleted`` teardown (stop+remove the container, rmtree
    the workspace), and
  * the ``office_created`` connect (adopt-or-create the container,
    write the workspace).

Incident (cbcl-stg, 2026-09-02 12:03): teardown of the deleted office
began at 12:03:20; the same-name replacement's ``office_created`` push
landed at 12:03:47 and ADOPTED the old office's still-running container
by name; teardown's Phase 5 then removed that very container at
12:03:51 and Phase 6 rmtree'd the workspace the new office had just
synced — the new office's Claude sign-in failed with "The office
container is not running" and the health loop 404-looped on the stale
container id until an operator restarted the daemon.

The fix is to SERIALIZE same-slug lifecycle operations: teardown holds
the slug's lock for its whole run, and connect holds it from container
ensure through the ``connected`` stamp. A same-name recreate then waits
the few seconds for the old office's teardown to finish and builds on
clean ground. Distinct slugs use distinct locks, so normal multi-office
operation is unaffected.

The registry is process-local and never pruned — it is bounded by the
number of distinct office slugs this daemon has ever seen, which is
tiny, and pruning a lock that a coroutine still holds would break the
serialization guarantee.
"""

from __future__ import annotations

import asyncio

_locks: dict[str, asyncio.Lock] = {}


def slug_lifecycle_lock(slug: str) -> asyncio.Lock:
    """Return the process-wide lifecycle lock for an office slug.

    The same slug always yields the same ``asyncio.Lock`` instance, so
    any two coroutines doing lifecycle work on that slug serialize.
    Callers must treat an empty slug as "no lock available" and skip
    locking rather than pass it here (all empty-slug callers would
    otherwise contend on one meaningless global lock).
    """
    lock = _locks.get(slug)
    if lock is None:
        lock = _locks[slug] = asyncio.Lock()
    return lock
