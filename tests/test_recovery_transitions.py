"""T1.1.7 — cross-component board-transition guard (rule R2).

Pins EVERY communicator-issued board move (``move_task`` /
``update_status`` actions emitted by recovery, dispatch, completion and
review-routing paths) against the backend's transition rules, via the
mirrored tables in ``src/orchestrator/_board_transitions.py``.

Three layers of protection:

1. **Checksum pin** — the mirrored tables must hash to the embedded
   ``TRANSITION_TABLE_SHA256``. Editing the mirror without consciously
   re-pinning fails everywhere, including a standalone-packaged
   communicator (no backend checkout present).
2. **Source-text drift check** — on this repo's CI the backend's
   ``backend/app/tasks/board.py`` is parsed as TEXT (never imported —
   the communicator venv has no fastapi/sqlalchemy) and its
   ``VALID_TRANSITIONS`` / ``MANAGER_ONLY_TRANSITIONS`` literals must
   equal the mirror. Editing ``board.py`` without updating the mirror
   fails this test. Skipped (with a clear reason) when the backend tree
   is absent.
3. **Move-site table** — ``MOVE_SITES`` below enumerates every
   communicator code site that issues a board move with a literal or
   derivable (from, to) pair. Each pair must be a valid transition, and
   actor-gated moves must use a permitted actor. A "completeness
   canary" greps the source tree for ``"new_status"`` / TASK_COMPLETE
   ``"status"`` literals so a NEW move site (or a changed literal) must
   be registered here before CI goes green.

If a test here fails after you touched a recovery/dispatch path: you
are about to issue a move the backend will reject (or one only the
manager/reviewer may perform). Fix the move — do not widen the tables.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.orchestrator._board_transitions import (
    MANAGER_ACTORS,
    MANAGER_ONLY_TRANSITIONS,
    TRANSITION_TABLE_SHA256,
    VALID_TRANSITIONS,
    actor_may_transition,
    compute_table_checksum,
    is_valid_transition,
)

# communicator/tests/ -> communicator/ -> repo root (cubicle_v2)
COMMUNICATOR_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = COMMUNICATOR_ROOT.parent
BACKEND_BOARD_PY = REPO_ROOT / "backend" / "app" / "tasks" / "board.py"
SRC_ROOT = COMMUNICATOR_ROOT / "src"

# Stand-in agent names for sites whose actor is "whichever worker /
# reviewer the dispatcher bound" (non-manager, non-MA names so the
# manager-actor shortcut can't mask a missing reviewer grant).
EXECUTOR = "test-executor-agent"
REVIEWER = "test-reviewer-agent"


# ---------------------------------------------------------------------------
# Layer 1 — checksum pin
# ---------------------------------------------------------------------------


class TestMirrorChecksum:
    def test_checksum_matches_mirrored_tables(self):
        """Recompute the canonical-serialization SHA256 from the live
        mirror and compare to the embedded constant. If this fails, the
        mirror was edited without re-pinning — verify the new values
        against backend/app/tasks/board.py FIRST, then update
        TRANSITION_TABLE_SHA256."""
        assert compute_table_checksum() == TRANSITION_TABLE_SHA256, (
            "src/orchestrator/_board_transitions.py drifted from its "
            "pinned checksum. Verify the tables against "
            "backend/app/tasks/board.py, then re-pin "
            f"TRANSITION_TABLE_SHA256 to {compute_table_checksum()!r}."
        )


# ---------------------------------------------------------------------------
# Layer 2 — backend source-text drift check (no backend import!)
# ---------------------------------------------------------------------------


def _extract_literal(source: str, assignment_regex: str, what: str):
    """Extract + literal_eval a top-level table literal from board.py text.

    ``set()`` (the empty 'archived' value) is not a literal, so it is
    rewritten to an empty tuple pre-parse and normalized back to a set
    by the caller.
    """
    match = re.search(assignment_regex, source, re.MULTILINE | re.DOTALL)
    assert match, (
        f"Could not locate {what} in backend/app/tasks/board.py — the "
        "extraction regex in test_recovery_transitions.py needs updating "
        "to match the new source layout (and the mirror needs re-checking)."
    )
    literal_text = match.group(1).replace("set()", "()")
    return ast.literal_eval(literal_text)


class TestBackendSourceDrift:
    @pytest.fixture(scope="class")
    def board_source(self) -> str:
        if not BACKEND_BOARD_PY.is_file():
            pytest.skip(
                "backend/app/tasks/board.py not present (communicator "
                "packaged standalone?) — relying on the checksum pin only"
            )
        return BACKEND_BOARD_PY.read_text(encoding="utf-8")

    def test_valid_transitions_match_backend_source(self, board_source):
        backend_raw = _extract_literal(
            board_source,
            r"^VALID_TRANSITIONS\s*:\s*dict\[str,\s*set\[str\]\]\s*="
            r"\s*(\{.*?\n\})",
            "VALID_TRANSITIONS",
        )
        backend_valid = {k: set(v) for k, v in backend_raw.items()}
        assert backend_valid == VALID_TRANSITIONS, (
            "backend/app/tasks/board.py:VALID_TRANSITIONS no longer "
            "matches the communicator mirror "
            "(src/orchestrator/_board_transitions.py). Update the mirror "
            "+ its checksum, and re-audit every MOVE_SITES entry below."
        )

    def test_manager_only_transitions_match_backend_source(
        self, board_source,
    ):
        backend_manager_only = _extract_literal(
            board_source,
            r"^MANAGER_ONLY_TRANSITIONS\s*:\s*set\[tuple\[str,\s*str\]\]"
            r"\s*=\s*(\{.*?\n\})",
            "MANAGER_ONLY_TRANSITIONS",
        )
        assert set(backend_manager_only) == MANAGER_ONLY_TRANSITIONS, (
            "backend/app/tasks/board.py:MANAGER_ONLY_TRANSITIONS no "
            "longer matches the communicator mirror. Update the mirror + "
            "checksum, and re-audit the actor assertions in MOVE_SITES."
        )

    def test_manager_actor_set_matches_backend_source(self, board_source):
        """The 'MA acts as manager' rule is encoded as a literal inside
        validate_transition — pin it so a backend change to the manager
        actor set surfaces here."""
        match = re.search(
            r"manager_actors\s*=\s*(\{[^}]*\})", board_source,
        )
        assert match, (
            "Could not locate the manager_actors literal in board.py — "
            "update this test and re-verify MANAGER_ACTORS in the mirror."
        )
        assert set(ast.literal_eval(match.group(1))) == set(MANAGER_ACTORS)


# ---------------------------------------------------------------------------
# Layer 3 — the enumerated move-site table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MoveSite:
    """One communicator code site that issues a board move."""

    name: str
    # file:line reference(s) — documentation for the human fixing a failure.
    where: str
    from_status: str
    to_status: str
    actor: str
    # The task's designated reviewer at that site (task.reviewer), when
    # the site's actor permission depends on it. None = unset/irrelevant.
    reviewer: str | None = None
    notes: str = field(default="", compare=False)


# Every communicator-issued move with a literal or derivable (from, to)
# pair. Line references verified 2026-06-13 — they are documentation,
# not assertions (the completeness canary below catches unregistered
# NEW sites; drift in line numbers alone is harmless).
MOVE_SITES: tuple[MoveSite, ...] = (
    # -- Watchdog crash recovery -------------------------------------------
    MoveSite(
        name="watchdog.crash_circuit_breaker",
        where="src/watchdog.py:309-329 (_handle_in_progress -> _move_task)",
        from_status="in_progress",
        to_status="blocked",
        actor="manager",
        notes=(
            "After 3 re-spawn-in-place attempts the watchdog escalates a "
            "crashed in_progress task to blocked for MA triage. NOTE: "
            "recovery of the crash itself is re-queue + re-dispatch IN "
            "PLACE — deliberately NOT an in_progress->ready move (see "
            "negative tests)."
        ),
    ),
    # -- Dispatcher pickup --------------------------------------------------
    MoveSite(
        name="dispatcher.ready_pickup",
        where=(
            "src/orchestrator/task_dispatcher.py:389-392 -> "
            "_move_and_assign (:680, literal 'in_progress' at call site; "
            "guarded by task_status == 'ready')"
        ),
        from_status="ready",
        to_status="in_progress",
        actor=EXECUTOR,
        notes=(
            "Worker auto-pick. blocked-status dispatches use _assign_only "
            "(:640) — update_task only, NO move (triage keeps the task in "
            "blocked); review-status dispatches don't flip status at all."
        ),
    ),
    # -- Executor completion (handlers.py move <- _agent_worker_task frame) -
    MoveSite(
        name="handlers.executor_complete.success",
        where=(
            "src/handlers.py:782-792 (task_complete move) <- "
            "src/_agent_worker_task.py:181-189 (status='review', "
            "execute mode: task is in_progress)"
        ),
        from_status="in_progress",
        to_status="review",
        actor=EXECUTOR,
    ),
    MoveSite(
        name="handlers.executor_complete.cancelled",
        where=(
            "src/handlers.py:782-792 <- src/_agent_worker_task.py "
            "(CancelledError, status='blocked', execute mode; the "
            "review-mode CancelledError branch stays in review with "
            "is_review_completion=True — no move, MEDIUM-3)"
        ),
        from_status="in_progress",
        to_status="blocked",
        actor=EXECUTOR,
    ),
    MoveSite(
        name="handlers.executor_complete.error_escalation",
        where=(
            "src/handlers.py:782-792 <- src/_agent_worker_task.py:296-309 "
            "(AgentErrorEscalation, status='blocked', execute mode; the "
            "is_review branch at :281-294 stays in review with "
            "is_review_completion=True — no move)"
        ),
        from_status="in_progress",
        to_status="blocked",
        actor=EXECUTOR,
    ),
    # REMOVED (MEDIUM-3, Phase-1 review): ``handlers.reviewer_session_
    # cancelled`` — the derived review→blocked edge from a CancelledError
    # in REVIEW mode no longer exists. Review-mode cancels now emit a
    # review-stamped completion (is_review_completion=True +
    # details.error_class='cancelled') that rides the capped infra
    # re-queue, never move_task. review→blocked itself stays a valid,
    # registered transition via mcp.worker.move_task.blocked.
    # -- Review routing / circuit breakers (handlers.py) --------------------
    MoveSite(
        name="handlers.ma_auto_approve",
        where="src/handlers.py:877-886 (MA clean review completion)",
        from_status="review",
        to_status="done",
        actor="manager-assistant",
    ),
    MoveSite(
        name="handlers.reviewer_circuit_breaker_auto_approve",
        where=(
            "src/handlers.py:1052-1061 (designated reviewer completed at "
            "the rework cap, no pending action request)"
        ),
        from_status="review",
        to_status="done",
        actor=REVIEWER,
        reviewer=REVIEWER,
        notes="Branch is gated on designated == agent_name.",
    ),
    MoveSite(
        name="handlers.reviewer_return_for_rework",
        where=(
            "src/handlers.py:1089-1098 (designated reviewer completed "
            "without an explicit verdict, below the rework cap)"
        ),
        from_status="review",
        to_status="ready",
        actor=REVIEWER,
        reviewer=REVIEWER,
    ),
    # -- In-container MCP tool surfaces (enum-locked LLM moves) -------------
    MoveSite(
        name="mcp.worker.update_status.review",
        where=(
            "src/_agent_image/_mcp/tools_worker.py:34-87 (update_status "
            "enum ['review','blocked']; executors hold in_progress tasks)"
        ),
        from_status="in_progress",
        to_status="review",
        actor=EXECUTOR,
    ),
    MoveSite(
        name="mcp.worker.update_status.blocked",
        where="src/_agent_image/_mcp/tools_worker.py:34-87",
        from_status="in_progress",
        to_status="blocked",
        actor=EXECUTOR,
    ),
    MoveSite(
        name="mcp.worker.move_task.done",
        where=(
            "src/_agent_image/_mcp/tools_worker.py:180-204 (reviewer/"
            "Board-Operator move_task, enum ['done','ready','blocked',"
            "'in_progress']; canonical source column is review)"
        ),
        from_status="review",
        to_status="done",
        actor=REVIEWER,
        reviewer=REVIEWER,
    ),
    MoveSite(
        name="mcp.worker.move_task.ready",
        where="src/_agent_image/_mcp/tools_worker.py:180-204",
        from_status="review",
        to_status="ready",
        actor=REVIEWER,
        reviewer=REVIEWER,
    ),
    MoveSite(
        name="mcp.worker.move_task.blocked",
        where="src/_agent_image/_mcp/tools_worker.py:180-204",
        from_status="review",
        to_status="blocked",
        actor=REVIEWER,
        reviewer=REVIEWER,
    ),
    MoveSite(
        name="mcp.worker.move_task.in_progress",
        where="src/_agent_image/_mcp/tools_worker.py:180-204",
        from_status="review",
        to_status="in_progress",
        actor=REVIEWER,
        reviewer=REVIEWER,
        notes="Rework return — manager-only, reviewer-granted.",
    ),
    MoveSite(
        name="mcp.manager.retry_blocked_task",
        where=(
            "src/_agent_image/_mcp/tools_manager.py:308+ "
            "(retry_blocked_task — backend resets blocked_bounce_count "
            "and performs the blocked->ready promotion as the manager)"
        ),
        from_status="blocked",
        to_status="ready",
        actor="manager",
    ),
    MoveSite(
        name="mcp.transform.archive_task",
        where=(
            "src/_agent_image/_mcp/transforms.py:41-46 (archive_task "
            "transform: new_status='archived', actor='manager'; "
            "reachability from every non-archived column asserted in "
            "test_archived_reachable_from_every_non_archived_status)"
        ),
        from_status="done",  # canonical pin; full fan-in tested separately
        to_status="archived",
        actor="manager",
    ),
)


class TestMoveSitesAreValidTransitions:
    @pytest.mark.parametrize(
        "site", MOVE_SITES, ids=[s.name for s in MOVE_SITES],
    )
    def test_site_pair_is_a_valid_transition(self, site: MoveSite):
        assert is_valid_transition(site.from_status, site.to_status), (
            f"Move site {site.name} ({site.where}) issues "
            f"{site.from_status} -> {site.to_status}, which the backend "
            "transition table REJECTS. Fix the move site — do not widen "
            "the mirror."
        )

    @pytest.mark.parametrize(
        "site", MOVE_SITES, ids=[s.name for s in MOVE_SITES],
    )
    def test_site_actor_is_permitted(self, site: MoveSite):
        assert actor_may_transition(
            site.from_status,
            site.to_status,
            site.actor,
            reviewer=site.reviewer,
        ), (
            f"Move site {site.name} ({site.where}) issues "
            f"{site.from_status} -> {site.to_status} as actor "
            f"{site.actor!r} (task.reviewer={site.reviewer!r}), which the "
            "backend actor gate REJECTS (manager-only transition / done "
            "approval gate)."
        )

    @pytest.mark.parametrize(
        "site", MOVE_SITES, ids=[s.name for s in MOVE_SITES],
    )
    def test_no_same_status_moves_registered(self, site: MoveSite):
        """Same-status moves are backend no-ops; a site issuing one is a
        latent bug (it relies on idempotency instead of not moving)."""
        assert site.from_status != site.to_status

    def test_manager_move_tool_enum_targets_exist_in_table(self):
        """The Manager's move_task tool enum (tools_manager.py:225-248)
        is the full 7-column surface with no fixed source column; pin
        that every enum target except 'backlog' is reachable from at
        least one column (the backend validates the specific pair at
        call time). 'backlog' is creation-only — nothing moves INTO it."""
        manager_enum = {
            "backlog", "ready", "in_progress", "blocked", "review",
            "done", "archived",
        }
        all_targets = set().union(*VALID_TRANSITIONS.values())
        assert "backlog" not in all_targets, (
            "The transition table now allows moving INTO backlog — "
            "re-audit the Manager move_task tool enum and this test."
        )
        assert manager_enum - {"backlog"} <= all_targets

    def test_archived_reachable_from_every_non_archived_status(self):
        """The archive_task transform (transforms.py:41-46) issues
        new_status='archived' from WHATEVER column the task is in, as
        actor='manager'. Valid iff every non-archived column allows
        -> archived for the manager."""
        for from_status, allowed in VALID_TRANSITIONS.items():
            if from_status == "archived":
                assert not allowed, "archived must stay terminal"
                continue
            assert "archived" in allowed, (
                f"{from_status} -> archived removed from the backend "
                "table — the archive_task transform can now issue an "
                "invalid move from that column."
            )
            assert actor_may_transition(
                from_status, "archived", "manager",
            )


# ---------------------------------------------------------------------------
# Negative tests — the transitions recovery code must NEVER (re)introduce
# ---------------------------------------------------------------------------


FORBIDDEN_PAIRS: tuple[tuple[str, str], ...] = (
    # The "yank": pulling an executing task back to ready strands its
    # live worker (PE-001.T139). Removed from the backend table
    # 2026-06-04; crash recovery is re-spawn-in-place instead.
    ("in_progress", "ready"),
    # Unblocking never jumps straight to execution — it routes through
    # ready (manager-gated, bounce-capped) and the dispatcher picks up.
    ("blocked", "in_progress"),
)


class TestForbiddenTransitionsStayForbidden:
    @pytest.mark.parametrize(
        "pair", FORBIDDEN_PAIRS, ids=[f"{a}->{b}" for a, b in FORBIDDEN_PAIRS],
    )
    def test_pair_is_not_a_valid_transition(self, pair):
        from_status, to_status = pair
        assert not is_valid_transition(from_status, to_status), (
            f"{from_status} -> {to_status} reappeared in the transition "
            "table. This pair was deliberately removed (see "
            "src/orchestrator/_board_transitions.py docstring) — if the "
            "backend really re-added it, update FORBIDDEN_PAIRS with a "
            "design rationale, not just the assertion."
        )

    @pytest.mark.parametrize(
        "pair", FORBIDDEN_PAIRS, ids=[f"{a}->{b}" for a, b in FORBIDDEN_PAIRS],
    )
    def test_no_registered_site_issues_the_pair(self, pair):
        offenders = [
            s.name
            for s in MOVE_SITES
            if (s.from_status, s.to_status) == pair
        ]
        assert not offenders, (
            f"Move site(s) {offenders} are registered with the forbidden "
            f"transition {pair[0]} -> {pair[1]}."
        )


# ---------------------------------------------------------------------------
# Completeness canary — a NEW move site must be registered above
# ---------------------------------------------------------------------------

# Literal `"new_status": "<status>"` occurrences across communicator/src.
# Variable-valued sites (watchdog._move_task, dispatcher._move_and_assign,
# handlers task_complete move, transforms move_task pass-through) carry
# no literal and are pinned via MOVE_SITES + the TASK_COMPLETE literal
# scan below. Key: path relative to communicator/, value: the exact
# multiset of literals expected in that file.
EXPECTED_NEW_STATUS_LITERALS: dict[str, tuple[str, ...]] = {
    # MA auto-approve (:881) + circuit-breaker auto-approve (:1056) +
    # return-for-rework (:1093).
    "src/handlers.py": ("done", "done", "ready"),
    # archive_task transform (:45).
    "src/_agent_image/_mcp/transforms.py": ("archived",),
}

# Literal `"status": "<status>"` values in the TASK_COMPLETE frames of
# the agent worker (the emitter feeding handlers.py's move). "planning"
# is the synthetic Planner-consult status — flagged is_review_completion
# so it NEVER reaches move_task (no board task exists).
EXPECTED_TASK_COMPLETE_STATUS_LITERALS: tuple[str, ...] = (
    "review",    # reviewer-mode completion (stay in review, no move)
    "blocked",   # triage-mode completion (stay in blocked, no move)
    "planning",  # planner consult (synthetic, no move)
    "review",    # executor success -> in_progress->review move
    "review",    # POST-TERMINAL CancelledError in REVIEW mode
                 #      (pivot-2 P1: verdict already landed —
                 #      is_review_completion=True, NO error_class,
                 #      details.post_terminal_cancel — the reviewer
                 #      branch fetches done/ready and takes "no action
                 #      needed"; no move). The matching EXECUTOR clean
                 #      frame carries a COMPUTED status (the terminal
                 #      action's own target -> same-status no-op move),
                 #      so it has no literal to pin here.
    "review",    # pre-terminal CancelledError in REVIEW mode (MEDIUM-3:
                 #      stay in review, is_review_completion=True ->
                 #      capped infra re-queue, no move)
    "blocked",   # pre-terminal CancelledError in execute mode ->
                 #      blocked move
    "review",    # reviewer AgentErrorEscalation (stay in review,
                 #      is_review_completion=True -> re-queue, no move)
    "blocked",   # executor AgentErrorEscalation -> blocked move
)

_NEW_STATUS_LITERAL_RE = re.compile(r'"new_status":\s*"([a-z_]+)"')
_STATUS_LITERAL_RE = re.compile(r'"status":\s*"([a-z_]+)"')


def _scan_new_status_literals() -> dict[str, tuple[str, ...]]:
    found: dict[str, tuple[str, ...]] = {}
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        literals = tuple(_NEW_STATUS_LITERAL_RE.findall(text))
        if literals:
            rel = py_file.relative_to(COMMUNICATOR_ROOT).as_posix()
            found[rel] = literals
    return found


class TestCompletenessCanary:
    def test_every_new_status_literal_is_registered(self):
        """Bidirectional: a NEW `"new_status": "<literal>"` anywhere in
        communicator/src (new move site, or a changed literal in an
        existing one) fails until EXPECTED_NEW_STATUS_LITERALS — and the
        MOVE_SITES table — are updated; a REMOVED one fails too so the
        registry can't go stale."""
        found = _scan_new_status_literals()
        assert found == EXPECTED_NEW_STATUS_LITERALS, (
            "The set of literal new_status move sites in communicator/src "
            "changed.\n"
            f"  found:    {found}\n"
            f"  expected: {EXPECTED_NEW_STATUS_LITERALS}\n"
            "Register the new/changed site in MOVE_SITES (with its "
            "(from, to, actor) pinned against the transition table) and "
            "update EXPECTED_NEW_STATUS_LITERALS."
        )

    def test_every_registered_literal_has_a_move_site(self):
        """Every literal target status found by the scan must appear as
        the to_status of at least one registered MoveSite — so the site
        table can't silently lag the registry."""
        registered_targets = {s.to_status for s in MOVE_SITES}
        for rel, literals in _scan_new_status_literals().items():
            for literal in literals:
                assert literal in registered_targets, (
                    f"{rel} issues a move to {literal!r} but no MOVE_SITES "
                    "entry covers that target — register the site."
                )

    def test_task_complete_status_literals_are_registered(self):
        """The agent worker's TASK_COMPLETE `status` literals feed the
        handlers.py executor-complete move. A new literal here is a new
        derivable move (or a new non-move synthetic status) and must be
        accounted for in MOVE_SITES / the expected tuple."""
        worker_file = SRC_ROOT / "_agent_worker_task.py"
        text = worker_file.read_text(encoding="utf-8")
        found = tuple(_STATUS_LITERAL_RE.findall(text))
        assert found == EXPECTED_TASK_COMPLETE_STATUS_LITERALS, (
            "TASK_COMPLETE status literals in src/_agent_worker_task.py "
            "changed.\n"
            f"  found:    {found}\n"
            f"  expected: {EXPECTED_TASK_COMPLETE_STATUS_LITERALS}\n"
            "If a new board status was added, register the derived "
            "(from, to) pair in MOVE_SITES; if it is a synthetic "
            "non-move status, document it next to "
            "EXPECTED_TASK_COMPLETE_STATUS_LITERALS."
        )

    def test_board_statuses_in_task_complete_are_movable_from_worker_modes(
        self,
    ):
        """Each *board* status emitted by an execute-mode TASK_COMPLETE
        frame must be reachable from in_progress (the execute-mode
        column). 'planning' is synthetic and exempt; review/blocked
        stay-in-place frames are flagged is_review_completion and never
        move."""
        for status in set(EXPECTED_TASK_COMPLETE_STATUS_LITERALS):
            if status == "planning":
                continue
            assert is_valid_transition("in_progress", status), (
                f"Execute-mode completion emits status={status!r} but "
                f"in_progress -> {status} is not a valid transition."
            )
