"""Mirrored board-transition tables — pinned to ``backend/app/tasks/board.py``.

WHY A MIRROR
============
The communicator issues board moves (``move_task`` / ``update_status``
actions) from its recovery, dispatch, and review-routing paths, but it
CANNOT import the backend's authoritative tables: the communicator venv
deliberately does not ship fastapi/sqlalchemy, and ``backend/app/tasks/
board.py`` transitively imports both. The chosen convention (rule R2 of
the Phase-1 review scope: "no recovery move without a transition-table
test") is therefore:

1. This module mirrors ``VALID_TRANSITIONS`` and
   ``MANAGER_ONLY_TRANSITIONS`` from ``backend/app/tasks/board.py``
   (lines ~73-108) EXACTLY, value for value.
2. ``TRANSITION_TABLE_SHA256`` pins a checksum of the canonical
   serialization of the mirrored tables.
3. ``communicator/tests/test_recovery_transitions.py`` recomputes the
   checksum from the mirror AND re-parses the backend source file as
   TEXT (no import) to verify the mirror matches the source — so
   editing ``board.py`` without updating this mirror fails CI on this
   repo, and editing the mirror without updating the checksum fails
   everywhere (including a standalone-packaged communicator).

IF THIS FILE DRIFTS from ``backend/app/tasks/board.py``, fix the mirror
(and the checksum) — never "fix" the test. The backend table is the
single source of truth; code is ground truth over specs.

Keep this module dependency-free (stdlib only): it is imported by tests
and may be imported by recovery/dispatch code for pre-flight validation.
"""

from __future__ import annotations

import hashlib
import json

# ---------------------------------------------------------------------------
# Mirror of backend/app/tasks/board.py:VALID_TRANSITIONS (~line 73).
# from_status -> set of allowed to_statuses.
#
# Notable invariants encoded by the backend table (do not "improve"):
#   * ``in_progress`` NEVER goes back to ``ready`` — that yank stranded a
#     live worker (PE-001.T139); orphaned in_progress tasks are
#     re-dispatched IN PLACE by the daemon, no status flip.
#   * ``blocked`` NEVER goes to ``in_progress`` — unblocking routes
#     through ``ready`` (manager-gated, bounce-capped).
#   * ``archived`` is terminal.
# ---------------------------------------------------------------------------
VALID_TRANSITIONS: dict[str, set[str]] = {
    "backlog": {"ready", "archived"},
    "ready": {"in_progress", "archived"},
    "in_progress": {"review", "blocked", "archived"},
    "blocked": {"ready", "archived"},
    "review": {"done", "in_progress", "blocked", "ready", "archived"},
    "done": {"archived"},
    "archived": set(),
}

# ---------------------------------------------------------------------------
# Mirror of backend/app/tasks/board.py:MANAGER_ONLY_TRANSITIONS (~line 94).
# ---------------------------------------------------------------------------
MANAGER_ONLY_TRANSITIONS: set[tuple[str, str]] = {
    ("backlog", "ready"),
    ("blocked", "ready"),
    ("review", "done"),
    ("review", "in_progress"),
    ("review", "blocked"),
    ("review", "ready"),
    ("backlog", "archived"),
    ("ready", "archived"),
    ("in_progress", "archived"),
    ("blocked", "archived"),
    ("review", "archived"),
    ("done", "archived"),
}

# Mirror of the actor rule in backend/app/tasks/board.py:
#   * ``validate_transition`` (~line 126): "Manager Assistant acts as
#     Board Operator on behalf of the Manager" — both count as manager.
#   * (~lines 170-180): for a MANAGER_ONLY transition, the allowed actors
#     are the manager actors, PLUS the task's designated reviewer when
#     the transition leaves the ``review`` column.
MANAGER_ACTORS: frozenset[str] = frozenset({"manager", "manager-assistant"})


def serialize_transition_tables() -> str:
    """Canonical, deterministic serialization of the mirrored tables.

    Used by the drift checksum. Sorted keys + sorted members so the
    output is stable across Python versions and set iteration orders.
    """
    return json.dumps(
        {
            "valid_transitions": {
                k: sorted(v) for k, v in sorted(VALID_TRANSITIONS.items())
            },
            "manager_only_transitions": sorted(
                list(pair) for pair in MANAGER_ONLY_TRANSITIONS
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_table_checksum() -> str:
    """SHA256 hex digest of :func:`serialize_transition_tables`."""
    return hashlib.sha256(
        serialize_transition_tables().encode("utf-8")
    ).hexdigest()


# SHA256 of the canonical serialization above. The test recomputes the
# digest from the live tables and compares to this constant — anyone who
# edits the tables must consciously re-pin the checksum (and, per the
# docstring, verify against backend/app/tasks/board.py first).
TRANSITION_TABLE_SHA256 = (
    "e361ce111159599269487c9e991d1b29a1ec1ab9207bbc19947965b5605a0303"
)


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """True iff the backend's transition table permits ``from -> to``.

    Same-status moves are backend no-ops (idempotent), not transitions;
    they return False here on purpose — callers should not issue them.
    """
    return to_status in VALID_TRANSITIONS.get(from_status, set())


def actor_may_transition(
    from_status: str,
    to_status: str,
    actor: str,
    *,
    reviewer: str | None = None,
) -> bool:
    """Mirror of the actor gate in ``validate_transition`` (board.py ~170-180
    plus the ``done`` gate at ~237-243).

    ``reviewer`` is the task's designated reviewer (``task.reviewer``),
    or None when unset.
    """
    if not is_valid_transition(from_status, to_status):
        return False
    if (from_status, to_status) in MANAGER_ONLY_TRANSITIONS:
        allowed = set(MANAGER_ACTORS)
        if reviewer and from_status == "review":
            allowed.add(reviewer)
        if actor not in allowed:
            return False
    # Independent ``done`` gate: only manager actors or the designated
    # reviewer may approve (executors must never approve their own work).
    if to_status == "done":
        if actor not in MANAGER_ACTORS and actor != reviewer:
            return False
    return True
