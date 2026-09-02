"""Message handler registration and per-office initialisation.

Wires together all per-office components (supervisor, dispatcher, manager,
session manager, script runner, queue manager, etc.) and registers Redis
message handlers for the process-per-agent model.

Event-driven queue updates: every task event from the backend updates the
per-agent queue immediately via AgentQueueManager. Unassigned review/blocked
tasks are routed to the Manager Assistant's queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.script_sync import ScriptSyncer
from src.config_sync.sync_service import ConfigStore
from src.config_sync.workspace_setup import WorkspaceSetup
from src.dispatch import (
    handle_script_kill,
    handle_script_execute,
    handle_script_secret_update,
    handle_script_variable_binding_set,
    handle_skill_secret_update,
)
from src._handlers._mcp import run_mcp_add, run_mcp_remove
from src.agent_channel import AgentChannelEmitter
from src._handlers._mcp_listing import MCPRefreshState, refresh_mcp_list
from src._handlers._office_lifecycle import (
    handle_office_created,
    handle_office_deleted,
)
from src._handlers._requests import dispatch_backend_request
from src._handlers._setup import (
    run_analyze_office_description,
    run_generate_office_config,
    run_improve_office_config,
)
from src._handlers._tasks import route_task_moved, route_task_updated
from src.health.reporter import HealthReporter
from src.orchestrator.agent_queue import AgentQueueManager
from src.orchestrator.manager_controller import ManagerController
from src.orchestrator.session_manager import SessionManager
from src.scripts.script_execution import (
    reconcile_orphaned_executions as reconcile_orphaned_script_executions,
)
from src.scripts.script_runner import ScriptRunner
from src.scripts.secrets_store import SecretsStore
from src.scripts.variable_manager import VariableManager

if TYPE_CHECKING:
    from src.config import OfficeConfig

logger = logging.getLogger("cbcl.handlers")

# Strong-reference holder for fire-and-forget background tasks that
# would otherwise be GC'd mid-execution (per asyncio docs). Tasks
# self-remove via ``add_done_callback(_BACKGROUND_TASKS.discard)``.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background(coro, *, name: str | None = None) -> asyncio.Task | None:
    """Spawn a fire-and-forget task with strong-reference + GC cleanup.

    Returns the task on success, or ``None`` when no event loop is
    running (matches the test-harness fallback the MCP-init spawn
    needs — bare ``create_task`` raises in that case). When there's
    no loop, the coroutine is ``close()``-d explicitly so callers
    don't trigger a ``coroutine was never awaited`` RuntimeWarning.
    The done callback removes the entry from ``_BACKGROUND_TASKS``
    so we don't leak references for the life of the daemon.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    task = loop.create_task(coro, name=name) if name else loop.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task

# After this many rework cycles, a reviewer session that completes without
# explicitly moving the task auto-approves (circuit breaker). Below this,
# ambiguous completion returns the task for another rework cycle.
# Matches the Manager system prompt ("Maximum 2 rework cycles").
#
# T1.1.4 (05/D-03): the cap is SINGLE-SOURCED from the backend — the
# resolved ``board.MAX_REWORK_CYCLES`` value ships in every sync_config
# payload and lands in ``ConfigStore.max_rework_cycles``. The env read
# below is only the cold-start fallback (before the first sync_config),
# so divergent per-host env tuning can no longer split the policy.
MAX_REWORK_CYCLES = int(os.environ.get("CUBICLE_MAX_REWORK_CYCLES", "2"))


def get_max_rework_cycles(config_store: ConfigStore | None = None) -> int:
    """Resolve the rework-cycle cap, preferring the backend-synced value.

    The backend is the policy owner (``app/tasks/board.py``); it ships
    its resolved cap in sync_config. Falls back to the local env default
    when no config has synced yet (cold start) or the synced value is
    malformed.
    """
    if config_store is not None:
        synced = getattr(config_store, "max_rework_cycles", None)
        if isinstance(synced, int) and synced >= 0:
            return synced
    return MAX_REWORK_CYCLES


# HIGH-2: per-task cap on infra-failure review RE-QUEUES. A
# DETERMINISTIC infra failure (e.g. auth_failed escalates on its very
# first attempt per error_classifier) would otherwise re-spawn the
# reviewer forever — a full CLI session per cycle. After this many
# infra re-queues the task is LEFT in review (no move — review-state
# escalation is the backend's stuck-review sweeper's job) with a loud
# activity. The counter is in-memory per office (daemon restart resets
# it) and resets on a genuine, non-infra review completion.
REVIEW_INFRA_REQUEUE_CAP = 3

# Round-2 LOW (MEDIUM-4 follow-up): in-flight Planner consult markers,
# keyed by the synthetic task id minted at spawn time
# (``planner-<uuid>``). Supervisor-SYNTHESIZED fatal events (heartbeat
# kill / process exit) carry no ``planner_consult`` marker, so the
# planner error branch in ``_on_agent_event`` recovers the consult's
# mode/context_key from here instead of poking with the
# specify/general_chat defaults — and applies the verify-silence rule
# (a killed backend-fired verify must NOT poke the Manager; the
# stuck-verifying sweeper owns recovery). Synthetic ids are
# uuid-unique, so a flat module dict is safe across offices. Entries
# are popped on EVERY planner exit path (clean done, worker-emitted
# error, kill); a daemon restart clears it (the consult dies with the
# daemon anyway).
#
# A consult marker the STALL watchdog killed carries the extra key
# ``_watchdog_killed`` (value: "auto_restart" | "cap"). Both planner
# exit branches in ``_on_agent_event`` check it and SUPPRESS the
# now-redundant failure poke: an auto-restart silently re-fires the
# SAME consult (no Manager-facing message), and a cap kill emits ONE
# authoritative "stalled across N attempts" poke from the watchdog
# itself. Without this, every watchdog kill leaked a SECOND, MISLABELED
# "Task was cancelled." poke (incident 2026-06-23: the Manager read it
# as a user cancel and refused to re-engage; chat showed two near-
# identical bubbles per stall).
_planner_consults: dict[str, dict] = {}

# Flow Studio (FS-P3.T4): in-flight Flow-Architect / Data-Curator
# consult markers, keyed by the synthetic task id minted at spawn time
# (``flow-consult-<uuid>``). Same posture as ``_planner_consults``: a
# supervisor-SYNTHESIZED fatal (heartbeat kill / process exit) carries
# no ``flow_consult`` marker on the event, so the error branch in
# ``_on_agent_event`` recovers the consult's ``request_id`` from here
# and publishes the honest ``flow_consult_failed`` — the REST poll
# must never hang on a dead session. Entries are popped on every exit
# path; a daemon restart clears it (the consult dies with the daemon,
# and the backend status row's TTL expires the poll honestly). Also
# carries the ``_last_progress_pub`` throttle stamp for the
# ``flow_consult_progress`` relay.
_flow_consults: dict[str, dict] = {}

# Minimum seconds between relayed ``flow_consult_progress`` events per
# consult — the worker emits a progress frame per tool call, which
# would rewrite the backend status row dozens of times a minute for no
# reader benefit. One pulse every ~10s keeps the Studio rail live AND
# refreshes the status row's TTL.
_FLOW_CONSULT_PROGRESS_MIN_INTERVAL_SECONDS = 10.0

# AREA-2 (verify turn-end incident 2026-07-17): the LIVE heartbeat task
# handle per consult, keyed by the same synthetic id as
# ``_planner_consults``. The heartbeat used to be spawned fire-and-forget
# (handle discarded into ``_BACKGROUND_TASKS``) with an AGENT-shaped exit
# (``not is_agent_busy("planner")`` sampled every 75s) — but every refire
# path respawns the Planner within ~1s of the idle flip, a gap a 75s poll
# essentially never observes, so the stale heartbeat re-latched onto the
# NEXT consult's busy state and kept pulsing its own elapsed counter
# ("49m" interleaved with the fresh "4m"). Two fixes, belt-and-suspenders:
# the loop's exit condition is now CONSULT-shaped (its own stash entry
# gone → break), AND the handle stored here is cancelled from every
# consult exit site (``_cancel_planner_heartbeat``). Synthetic ids are
# uuid-unique, so a flat module dict is safe across offices; entries
# self-prune in the heartbeat's ``finally``.
_planner_heartbeats: dict[str, asyncio.Task] = {}

# AREA-2 single-flight (same incident): scope ids with a daemon-side
# verify REFIRE currently in flight (honesty-check fetch → idle-wait →
# spawn). During that window the old consult's stash entry is already
# popped and the Planner is briefly idle, so a backend/sweeper-fired
# verify for the same scope would neither hit the busy-refuse nor the
# live-stash dedupe — it would double-run the verify (and double the
# heartbeats). ``_handle_consult_planner`` drops a NON-refire verify for
# a scope listed here; the refire itself carries its marker flag and is
# exempt from its own guard.
_verify_refire_pending: set[str] = set()


def _cancel_planner_heartbeat(synthetic_id: str) -> None:
    """Cancel + discard one consult's heartbeat task (leak-proof
    lifecycle, AREA-2). Called from every consult exit site — the clean
    task_complete pop and the error/kill pop in ``_on_agent_event`` — so
    a refire respawning the Planner inside the old heartbeat's 75s sleep
    window can never leave a second elapsed counter pulsing. Idempotent:
    a missing/done handle is a no-op (the heartbeat's own ``finally``
    self-prunes on the consult-shaped loop exit)."""
    heartbeat = _planner_heartbeats.pop(synthetic_id, None)
    if heartbeat is not None and not heartbeat.done():
        heartbeat.cancel()


# Cooldown after a Planner STALL hit the auto-restart cap, keyed by
# (workstream_id, scope_id, mode). While a key is in cooldown,
# ``_handle_consult_planner`` REFUSES a re-consult and tells the Manager
# the consult is wedged (don't immediately retry) instead of spawning a
# fresh Planner — which would reset the restart counter and start the
# whole stall cycle over (the respawn-after-cap loop seen in the
# incident). Cleared on any clean consult completion for the same key or
# when the cooldown window elapses.
_planner_cap_cooldown: dict[tuple[str, str, str], float] = {}


def _cap_cooldown_key(consult: object) -> tuple[str, str, str]:
    # ``planner_consult`` is normally the full marker dict, but some
    # legacy/error paths set it to a bare truthy marker (e.g. True). Coerce
    # any non-dict to an empty key so this never raises on the hot path.
    c = consult if isinstance(consult, dict) else {}
    return (
        str(c.get("workstream_id") or ""),
        str(c.get("scope_id") or ""),
        str(c.get("mode") or ""),
    )


# LONG-VERIFY chat notices (incident 2026-07-16 follow-up): a healthy
# ultracode verify consult can legitimately run 15-30+ minutes — the office
# container is CPU-capped, so workflow subagents run near-serially — but the
# user staring at the chat had no signal whether it was working or wedged
# (the heartbeat's status-pill pulses + feed keepalive rows exist, yet the
# TRANSCRIPT stays silent). At each threshold below, the planner heartbeat
# posts ONE durable, chat-visible ``role='system'`` progress notice into the
# consult's Manager context (never more than once per threshold per consult
# — the sent-flags live on the consult's ``_planner_consults`` stash entry).
# This is a PROGRESS notice only: the verify-silence posture for FAILURES
# (``_poke_failure``'s mode=="verify" branch stays silent; the sweeper owns
# recovery) is untouched.
VERIFY_NOTICE_THRESHOLDS_SECONDS: tuple[int, ...] = (900, 1800)


# Owner directive 2026-08-04: the still-running notices cover EVERY
# consult mode, with mode-aware copy. Verify keeps its historical phrase
# (and the attempts variant); the rest get a short mode label.
_CONSULT_NOTICE_PHRASES: dict[str, str] = {
    "verify": "Scope verification",
    "scope_plan": "Scope planning",
    "specify": "Spec drafting",
    "materialize": "Task authoring",
    "research": "Planner research",
}


def build_long_verify_notice(
    threshold_seconds: int,
    *,
    elapsed_minutes: int | None = None,
    attempts: int = 1,
    mode: str = "verify",
) -> str:
    """The user-facing copy for one still-running consult progress notice.

    ``elapsed_minutes`` is the CUMULATIVE minutes since the FIRST verify
    attempt for this scope (threaded through daemon refires via the
    ``_verify_first_started`` marker — AREA-2, verify turn-end incident
    2026-07-17), so a refired attempt crossing a threshold reports the
    honest total instead of resetting to "(15m)"; when omitted the copy
    falls back to the threshold itself. ``attempts`` > 1 names the refire
    count ("~45m across 3 attempts") — verify-only today.

    ``mode`` selects the phrase (owner directive 2026-08-04 — the
    notices generalized from verify-only to every consult mode); verify
    keeps its historical copy byte-for-byte.

    Pinned by ``tests/test_long_verify_notice.py`` — a copy change must
    update the pins in the same commit.
    """
    minutes = int(elapsed_minutes or threshold_seconds // 60)
    phrase = _CONSULT_NOTICE_PHRASES.get(
        mode, f"The Planner consult ({mode})" if mode else "The Planner consult"
    )
    if mode == "verify":
        if attempts > 1:
            return (
                f"🗺️ Scope verification is still running (~{minutes}m across "
                f"{attempts} attempts) — large scope or constrained "
                "resources; it will report when done."
            )
        return (
            f"🗺️ Scope verification is still running ({minutes}m) — large "
            "scope or constrained resources; it will report when done."
        )
    return (
        f"🗺️ {phrase} is still running ({minutes}m) — it will report "
        "when done."
    )


def claim_due_verify_notice(
    elapsed_seconds: float, marker: dict,
) -> int | None:
    """Return the threshold (seconds) whose long-verify notice is DUE now,
    claiming it (and any lower threshold) on the consult ``marker`` so it
    can never be sent twice.

    Contract (pinned by ``tests/test_long_verify_notice.py``):

    * strictly once per threshold per consult — the ``_verify_notice_<t>``
      sent-flags are stamped on the marker before the caller sends;
    * a consult that finishes before a threshold never crosses it — the
      heartbeat loop is CONSULT-owned (AREA-2, verify turn-end incident
      2026-07-17): it exits the moment ITS consult leaves
      ``_planner_consults`` and is additionally cancelled on that pop
      (``_cancel_planner_heartbeat``), so this is never called for a
      finished consult even when a refire respawns the Planner inside
      the old heartbeat's sleep window. A refired attempt runs its OWN
      heartbeat with a fresh marker; the caller passes CUMULATIVE
      elapsed (``_verify_first_started``) so its notices stay honest;
    * if a pulse lands past SEVERAL unsent thresholds (event-loop stall,
      laptop suspend), only the HIGHEST is returned and the lower ones are
      claimed silently — a stale "(15m)" notice at minute 31+ would be
      noise, not signal.
    """
    due: int | None = None
    for threshold in VERIFY_NOTICE_THRESHOLDS_SECONDS:
        if elapsed_seconds >= threshold and not marker.get(
            f"_verify_notice_{threshold}"
        ):
            due = threshold
    if due is None:
        return None
    for threshold in VERIFY_NOTICE_THRESHOLDS_SECONDS:
        if threshold <= due:
            marker[f"_verify_notice_{threshold}"] = True
    return due


async def _verify_consult_verdict_recorded(
    consult: dict,
    *,
    platform_url: str,
    office_id: str,
    security_token: str,
) -> bool | None:
    """Post-verify honesty check (incident 2026-07-16): did the Planner's
    verify session actually RECORD a verdict?

    The verify pipeline's "completed" signal is exit-code-shaped, not
    verdict-shaped — a Planner session that exits 0 WITHOUT ever getting a
    ``complete_scope_verification`` call accepted (an ultracode session
    ending on subagent summaries, or every PASS refused by the backend's
    pre-PASS gate) used to produce the hard-coded "The Planner has completed
    scope verification" Manager poke, then ~15 minutes of silence until the
    backend's stuck-verifying sweeper. This fetches the scope and checks
    verdict-shaped reality instead. Returns:

    * ``True``  — a verdict was accepted: the scope left ``verifying``, or
      its ``execution_plan.verification.status`` is no longer ``pending``
      (e.g. a FAIL past the verify cap keeps state ``verifying`` but records
      ``failed``).
    * ``False`` — the scope is still ``verifying`` with a ``pending``
      verification: the session ended verdictless.
    * ``None``  — the check could not run (no scope_id / fetch error).
      Callers FAIL OPEN on ``None`` and keep today's success poke, so a
      backend blip can't convert real successes into failure pokes.
    """
    scope_id = str(consult.get("scope_id") or "")
    if not scope_id:
        return None
    import httpx

    from src.backend_client import auth_headers as _auth_headers
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{platform_url}/api/offices/{office_id}/scopes/{scope_id}",
                headers=_auth_headers(security_token),
            )
        if resp.status_code != 200:
            logger.warning(
                "Post-verify honesty check: scope fetch returned %s for %s "
                "— failing open (keeping the success poke)",
                resp.status_code, scope_id,
            )
            return None
        scope = resp.json() or {}
    except Exception:
        logger.warning(
            "Post-verify honesty check: scope fetch failed for %s — "
            "failing open (keeping the success poke)",
            scope_id, exc_info=True,
        )
        return None
    if (scope.get("state") or "") != "verifying":
        return True
    plan = scope.get("execution_plan")
    verification = (
        plan.get("verification") or {} if isinstance(plan, dict) else {}
    )
    status = str(verification.get("status") or "pending").strip().lower()
    return status != "pending"


# FIX P3: consult modes whose success poke is OUTCOME-gated (extends the
# shipped verify honesty check per the outcome-gated-completion posture of
# docs/specs/planner-verify-fixes/00-research.md). Each mode has ONE
# expected durable write; a clean exit without it (an ultracode session
# ending on subagent summaries) used to emit the success poke anyway — the
# Manager discovered the emptiness a turn later, or not at all. ``research``
# is deliberately absent (its write target is discretionary), ``verify``
# has its own verdict-shaped check above.
_OUTCOME_GATED_MODES: frozenset[str] = frozenset({
    "specify", "scope_plan", "materialize",
})

# FIX P2: per-class backoff before the one-shot infra re-fire of a consult.
# Mirrors the classifier's remedy backoffs (``error_classifier._remedy_for``)
# — only the class NAME rides the escalation event, so the values are
# pinned here rather than re-derived from error text.
_INFRA_REFIRE_BACKOFF_SECONDS: dict[str, float] = {
    "api_overloaded": 180.0,
    "rate_limited": 60.0,
    "timeout": 5.0,
    "connection_lost": 3.0,
}


async def _fetch_consult_outcome_state(
    consult: dict,
    mode: str,
    *,
    platform_url: str,
    office_id: str,
    security_token: str,
) -> dict | None:
    """Fetch the current state of ``mode``'s expected write target.

    Returns a small ``{exists, revision, updated_at}`` dict, or ``None``
    when the check could not run (missing ids / fetch error / non-200 —
    every caller FAILS OPEN on ``None``). Shared by the spawn-time
    snapshot and the post-consult outcome gate so both read the same
    shape from the same endpoints:

    * ``specify``     → the workstream's spec row (list endpoint).
    * ``scope_plan``  → the scope's ``execution_plan`` JSONB.
    * ``materialize`` → ``exists`` = the scope has ≥1 task with a
      complete brief (revision/updated_at stay ``None``).
    """
    workstream_id = str(consult.get("workstream_id") or "")
    scope_id = str(consult.get("scope_id") or "")
    import httpx

    from src.backend_client import auth_headers as _auth_headers

    headers = _auth_headers(security_token)
    base = f"{platform_url}/api/offices/{office_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if mode == "specify":
                if not workstream_id:
                    return None
                resp = await client.get(
                    f"{base}/specs",
                    params={"workstream_id": workstream_id},
                    headers=headers,
                )
                if resp.status_code != 200:
                    return None
                spec = next(
                    (
                        s for s in (resp.json() or [])
                        if isinstance(s, dict)
                        and str(s.get("workstream_id") or "")
                        == workstream_id
                    ),
                    None,
                )
                if spec is None:
                    return {"exists": False,
                            "revision": None, "updated_at": None}
                return {
                    "exists": True,
                    "revision": spec.get("revision"),
                    "updated_at": spec.get("updated_at"),
                }
            if mode == "scope_plan":
                if not scope_id:
                    return None
                resp = await client.get(
                    f"{base}/scopes/{scope_id}", headers=headers,
                )
                if resp.status_code != 200:
                    return None
                plan = (resp.json() or {}).get("execution_plan")
                if not isinstance(plan, dict) or not plan:
                    return {"exists": False,
                            "revision": None, "updated_at": None}
                return {
                    "exists": True,
                    "revision": plan.get("revision"),
                    "updated_at": None,
                }
            if mode == "materialize":
                if not scope_id:
                    return None
                resp = await client.get(
                    f"{base}/scopes/{scope_id}/tasks", headers=headers,
                )
                if resp.status_code != 200:
                    return None
                body = resp.json()
                tasks = (
                    body if isinstance(body, list)
                    else (body or {}).get("items") or []
                )
                has_contracted = any(
                    isinstance(t, dict) and t.get("brief_is_complete")
                    for t in tasks
                )
                return {"exists": has_contracted,
                        "revision": None, "updated_at": None}
    except Exception:
        logger.warning(
            "Consult outcome fetch failed (mode=%s) — failing open",
            mode, exc_info=True,
        )
        return None
    return None


def _consult_outcome_advanced(
    snapshot: dict | None, current: dict | None,
) -> bool | None:
    """Decide whether the mode's expected write actually LANDED.

    * ``current is None``      → ``None`` (check couldn't run; fail open).
    * target absent            → ``False`` (nothing was written).
    * no spawn-time snapshot   → ``True`` (existence is the best signal
      we have — fail-open direction, per the plan).
    * snapshot present         → advanced iff revision grew or
      ``updated_at`` changed (a specify that edits a draft IN PLACE keeps
      its revision, so ``updated_at`` is load-bearing there). Targets
      whose fetch carries neither field (materialize) pass on existence.
    """
    if current is None:
        return None
    if not current.get("exists"):
        return False
    if not isinstance(snapshot, dict):
        return True
    if not snapshot.get("exists"):
        # Absent → exists IS the advance (incident 2026-08-04, Presale
        # Office / FO-002.S03): the spawn-time snapshot said the target
        # did not exist and it exists now, so the consult's write landed.
        # Without this branch a scope_plan writing the FIRST revision on
        # a fresh scope read as not-advanced — scope_plan's fetch never
        # carries ``updated_at`` and the snapshot's ``revision: None``
        # defeats the int comparison below — so EVERY new scope's
        # skeleton consult was silently refired for a full redundant
        # 25-40 min ultracode session (and the refire fed the stall
        # watchdog the >40-min sessions it then killed).
        return True
    snap_rev, cur_rev = snapshot.get("revision"), current.get("revision")
    if (
        isinstance(snap_rev, int)
        and isinstance(cur_rev, int)
        and cur_rev > snap_rev
    ):
        return True
    snap_upd, cur_upd = snapshot.get("updated_at"), current.get("updated_at")
    if cur_upd is not None and cur_upd != snap_upd:
        return True
    if cur_rev is None and cur_upd is None:
        # Existence-shaped target (materialize) — nothing to compare.
        return True
    # Snapshot target still exists but neither revision nor updated_at
    # moved: the pre-existing artifact was NOT touched this consult.
    # One edge deliberately tolerated: a snapshot taken when the target
    # ALREADY existed and a consult that legitimately decided "no change
    # needed" reads as not-advanced — the refire is one-shot and
    # idempotent, so the cost is one redundant consult, never a loop.
    return False


async def _route_completed_task(
    task_id: str,
    new_status: str,
    *,
    platform_url: str,
    office_id: str,
    security_token: str,
    config_store: ConfigStore,
    queue_manager: AgentQueueManager,
    dispatcher: object | None,
) -> None:
    """Route a freshly-moved review/blocked task to the
    reviewer / Manager Assistant queue. Runs via
    ``_spawn_background`` — exceptions are logged here
    because the spawn point can't see them.

    NIT-10: hoisted out of the ``_on_agent_event`` closure to module
    level (deps passed explicitly) so it isn't re-defined per event.
    """
    import httpx

    from src.backend_client import auth_headers as _auth_headers
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Fetch task to get reviewer + readable_id.
            task_resp = await client.get(
                f"{platform_url}/api/offices/{office_id}/tasks/{task_id}",
                headers=_auth_headers(security_token),
            )
            task_info = task_resp.json() if task_resp.status_code == 200 else {}
        reviewer = task_info.get("reviewer") or ""
        readable_id = task_info.get("readable_id") or task_id[:8]

        # ADD-A4: only route to the designated
        # reviewer when it is a known, ACTIVE
        # agent. A deactivated/deleted reviewer
        # would starve the task forever (the
        # dispatch loop only visits active in-
        # config agents). Fall back to the MA.
        reviewer_ok = (
            reviewer
            and config_store.is_agent_dispatchable(
                reviewer
            )
        )
        if new_status == "review":
            if reviewer and not reviewer_ok:
                logger.warning(
                    "Task %s reviewer '%s' is "
                    "inactive/missing — falling "
                    "back to Manager Assistant",
                    readable_id, reviewer,
                )
            if reviewer_ok:
                # DIRECT REVIEWER ROUTING — skip MA.
                await queue_manager.add_task(reviewer, {
                    "task_id": task_id,
                    "readable_id": readable_id,
                    "reviewer": reviewer,
                    "status": "review",
                    "priority": "urgent",
                })
                if dispatcher is not None:
                    await dispatcher.dispatch_agent(reviewer)
                logger.info(
                    "Task %s -> reviewer '%s' queue (direct)",
                    readable_id, reviewer,
                )
            else:
                # No reviewer — fallback: route to MA queue.
                # Do NOT unassign the executor — assigned_agent
                # must remain static for the task lifecycle.
                await queue_manager.add_task("manager-assistant", {
                    "task_id": task_id,
                    "readable_id": readable_id,
                    "status": "review",
                    "priority": "urgent",
                })
                if dispatcher is not None:
                    await dispatcher.dispatch_agent("manager-assistant")
                logger.info("Task %s -> MA queue (no reviewer)", readable_id)

        elif new_status == "blocked":
            # Blocked goes to MA for triage — UNLESS the task
            # already has a pending action request awaiting the
            # user's decision. Without this guard the MA picks
            # up the same blocked task on every dispatch loop,
            # proposes another action_request, and floods the
            # inbox (the user reported 100+ duplicates for the
            # same task TO-007.T40). The pending-request check is
            # the canonical "task is parked waiting on a human"
            # signal — when one exists, leaving the task alone
            # is the right move. Helper lives in
            # ``backend_client`` so the parallel routing path
            # (``_handlers._tasks.route_task_moved``) shares
            # the same check.
            from src.backend_client import (
                task_should_skip_ma_routing,
            )
            has_pending = await task_should_skip_ma_routing(
                platform_url=platform_url,
                office_id=office_id,
                task_id=task_id,
                security_token=security_token,
            )
            if has_pending:
                logger.info(
                    "Task %s blocked — pending action request exists, "
                    "skipping MA queue routing",
                    readable_id,
                )
            else:
                # Do NOT unassign — executor stays assigned.
                await queue_manager.add_task("manager-assistant", {
                    "task_id": task_id,
                    "readable_id": readable_id,
                    "status": "blocked",
                    "priority": "high",
                })
                if dispatcher is not None:
                    await dispatcher.dispatch_agent("manager-assistant")
                logger.info("Task %s -> MA queue (blocked)", readable_id)
    except Exception:
        logger.exception(
            "Background routing failed for task %s",
            task_id[:8],
        )


async def _run_history_backfill(
    workspace_path: Path, router: object, office_id: str,
) -> None:
    """Republish every terminal-state ``status.json`` to the backend.

    Scans ``{workspace}/.scripts/*/executions/*/status.json`` and
    sends each completed / failed execution as a ``script_status``
    WS event. The backend's existing ``handle_script_status``
    handler upserts on ``(script_id, execution_id)`` so the call
    is idempotent — re-running this backfill on the same disk set
    produces zero duplicate DB rows.

    Solves the gap where historical script executions (anything
    run before the in-container reporter shipped in cbcl 0.2.38)
    never made it to the DB and so don't appear in the Execution
    History panel. Without this, the user sees an empty history
    for any script with on-disk executions from before the upgrade.

    Best-effort: WS not connected yet → ``_publish`` logs and
    drops; per-file errors get logged + skipped (one corrupt
    status.json doesn't stop the rest).
    """
    # Poll the WS for up to 60s instead of sleeping a fixed 15s.
    # A slow-reconnecting daemon (cold network, backend warming up)
    # would otherwise miss the connect window and fire the backfill
    # against a disconnected router — every publish drops silently
    # and the user sees no rows. Loop with 0.5s ticks so we react
    # the moment the WS lands, but cap at 60s so a permanently-broken
    # backend doesn't keep this coroutine alive forever (the next
    # daemon restart retries the backfill anyway).
    ws_client = getattr(router, "ws_client", None) or getattr(router, "_ws_client", None)
    if ws_client is None:
        logger.debug(
            "history backfill: router has no ws_client attribute — "
            "skipping (transport type may have changed)",
        )
        return
    deadline = time.monotonic() + 60.0
    connected = False
    while time.monotonic() < deadline:
        if getattr(ws_client, "connected", False):
            connected = True
            break
        await asyncio.sleep(0.5)
    if not connected:
        logger.warning(
            "history backfill: WS still not connected after 60s — "
            "skipping. Next daemon restart will retry.",
        )
        return

    scripts_root = workspace_path / ".scripts"
    if not scripts_root.is_dir():
        return

    try:
        from src.scripts.script_notifier import _publish as _publish_event
    except Exception:
        logger.debug(
            "history backfill: cannot import script_notifier (non-fatal)",
            exc_info=True,
        )
        return

    total_attempted = 0
    total_published = 0
    for script_dir in scripts_root.iterdir():
        if not script_dir.is_dir():
            continue
        exec_root = script_dir / "executions"
        if not exec_root.is_dir():
            continue
        for run_dir in exec_root.iterdir():
            if not run_dir.is_dir():
                continue
            status_file = run_dir / "status.json"
            if not status_file.is_file():
                continue
            try:
                data = json.loads(status_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            status = data.get("status")
            # Only publish terminal states. ``running`` rows would
            # mark old-but-still-marked-running entries as live in
            # the DB and confuse the UI; the host-side
            # ``reconcile_orphaned_script_executions`` earlier in
            # office init has already reconciled those against the
            # real in-container process state (killing orphans and
            # flipping to ``failed`` on disk), so by the time we get
            # here every row that WAS hung is now terminal.
            if status not in ("completed", "failed", "timed_out"):
                continue
            total_attempted += 1
            # T8.3.2 (03/#19): ``timed_out`` is a host-only on-disk status;
            # the ws-protocol script_status enum is {running, completed,
            # failed}. Map it to ``failed`` ON THE WIRE (matching the live
            # completion path in script_execution.py) — the timeout detail
            # stays in error_message.
            wire_status = "failed" if status == "timed_out" else status
            payload = {
                "script_name": script_dir.name,
                "execution_id": run_dir.name,
                "status": wire_status,
                "task_id": data.get("task_id"),
                "triggered_by": data.get("triggered_by") or "unknown",
                "started_at": data.get("started_at") or "",
                "completed_at": data.get("completed_at"),
                "duration_seconds": data.get("duration_seconds"),
                "error_message": data.get("error_message"),
                "progress": None,
                "cron_id": None,
            }
            try:
                await _publish_event(
                    router, None, "script_status", payload,
                    context=f"backfill {script_dir.name}/{run_dir.name}",
                )
                total_published += 1
            except Exception:
                logger.debug(
                    "history backfill: publish failed for %s/%s",
                    script_dir.name, run_dir.name, exc_info=True,
                )
    if total_attempted:
        logger.info(
            "history backfill: published %d / %d script executions "
            "(office=%s)",
            total_published, total_attempted, office_id[:8],
        )


# ---------------------------------------------------------------------------
# Process-per-agent model (the only supported mode)
# ---------------------------------------------------------------------------


class ProcessModelOfficeComponents(NamedTuple):
    """Components for process-per-agent mode."""

    supervisor: object  # AgentSupervisor
    dispatcher: object  # TaskDispatcher
    router: object  # TransportClient (WsTransport)
    reporter: HealthReporter
    script_runner: ScriptRunner
    manager: ManagerController
    watchdog: object  # TaskWatchdog
    queue_manager: AgentQueueManager
    tool_proxy: object | None  # ToolProxyServer (WS mode only)


async def init_office_process_model(
    office: OfficeConfig,
    platform_url: str,
    container_name: str,
    redis_client: object,
    security_token: str = "",
    delete_queue: "asyncio.Queue[str] | None" = None,
    create_queue: "asyncio.Queue[dict] | None" = None,
    containers: object | None = None,
) -> ProcessModelOfficeComponents:
    """Create per-office components using the process-per-agent model.

    Parameters
    ----------
    office:
        Office configuration.
    platform_url:
        Backend platform URL.
    container_name:
        Docker container name for this office.
    redis_client:
        ``redis.asyncio.Redis`` connection (for task queues, sessions, health).
    security_token:
        ``cbcl_`` token for WebSocket authentication.
    delete_queue:
        Daemon-level queue for ``office_deleted`` notifications.
        When the backend pushes ``office_deleted`` over WS, the
        per-office router enqueues this office's id; a daemon-level
        consumer picks it up and runs the full teardown
        (``_disconnect_office_process_model``). The handler can't
        run the teardown directly because that would shut down its
        own router mid-callback. ``None`` disables the proactive
        path; reconciliation via the office-poll loop still works.
    create_queue:
        Daemon-level queue for ``office_created`` broadcasts. When
        the backend creates a new office it broadcasts on every
        connected WS; the FIRST router to receive a given
        office_id enqueues it (others see "already in connected
        dict" and silently drop) so a daemon-level consumer can
        connect the new office immediately instead of waiting up
        to 15s for the next office-poll tick. ``None`` disables
        the proactive path; the poll loop still picks up the new
        office on its next iteration as a safety net.
    containers:
        The daemon's ``ContainerManager``. Enables the per-office
        resource-limit reconciler (sync-driven recreate-when-idle —
        ``src.docker.limits_reconciler``). ``None`` (test surface)
        disables the reconciler; everything else works as before.
    """
    # 1. Workspace setup
    workspace_setup = WorkspaceSetup(office.workspace_path)
    workspace_setup.ensure_structure()

    # 2. Config sync — fetch from backend at startup
    config_store = ConfigStore()
    # Stamp the live mount set so the sync_config drift detector
    # can compare future configs against what's actually applied
    # in Docker. ``office.extra_mounts`` reflects what was just
    # passed to ``start_office`` in the previous step of office
    # bring-up.
    config_store.mark_extra_mounts_applied(office.extra_mounts)
    # Same stamp for the container resource limits: capture what the
    # container-create step just resolved (per-office override → host
    # config chain) so the sync-driven limits reconciler can compare
    # future sync_configs against what Docker actually applied.
    from src.config import resolve_office_resource_limits
    config_store.mark_resource_limits_applied(
        resolve_office_resource_limits(
            office.container_cpus, office.container_memory,
        )
    )
    script_syncer = ScriptSyncer(
        office.workspace_path, office_id=str(office.id),
    )
    claude_md_writer = ClaudeMdWriter(office.workspace_path)
    session_manager = SessionManager(workspace_path=office.workspace_path)
    await session_manager.init_from_disk()

    # 2b. Fetch initial config from backend REST API
    try:
        import httpx
        from src.backend_client import auth_headers
        headers = auth_headers(security_token)
        async with httpx.AsyncClient(timeout=10.0) as client:
            agents_resp = await client.get(
                f"{platform_url}/api/offices/{office.id}/agents",
                headers=headers,
            )
            agents = agents_resp.json() if agents_resp.status_code == 200 else []

            ws_resp = await client.get(
                f"{platform_url}/api/offices/{office.id}/workstreams",
                headers=headers,
            )
            workstreams = ws_resp.json() if ws_resp.status_code == 200 else []

            office_resp = await client.get(
                f"{platform_url}/api/offices/{office.id}",
                headers=headers,
            )
            office_data = office_resp.json() if office_resp.status_code == 200 else {}

            # CTX-03: if ANY bootstrap fetch degraded (non-200 → empty list),
            # do NOT run the workspace sync from a partial config. The empty
            # lists would drive the orphan-cleanup paths (now guarded, but this
            # is the upstream fix): a transient 401/500/503 at daemon start must
            # not touch the agent/workstream dirs at all. The connector WS
            # sync_config that follows carries the authoritative config.
            _bootstrap_ok = (
                agents_resp.status_code == 200
                and ws_resp.status_code == 200
                and office_resp.status_code == 200
            )
            if not _bootstrap_ok:
                logger.warning(
                    "Office %s startup bootstrap fetch degraded "
                    "(agents=%s workstreams=%s office=%s) — skipping workspace "
                    "sync_all; the connector WS sync_config will populate it.",
                    office.id,
                    agents_resp.status_code,
                    ws_resp.status_code,
                    office_resp.status_code,
                )

            # If the backend GET succeeded but ``manager_model`` is
            # missing from the response (degraded payload, schema
            # drift), fall back to the local default rather than
            # silently downgrading the Manager to Sonnet. The
            # backend pins ``manager_model`` to the latest "thinking"
            # Opus and strips client overrides — using the same
            # default here keeps both sides aligned even when the
            # fetch is partial.
            from src.orchestrator._model_defaults import (
                FALLBACK_MANAGER_MODEL,
            )
            sync_msg = {
                "config": {
                    "office_id": office.id,
                    "office_name": office.name,
                    "manager_model": (
                        office_data.get("manager_model")
                        or FALLBACK_MANAGER_MODEL
                    ),
                    # CTX-04: carry the office instructions + output style from
                    # the office GET so the startup bootstrap writes a COMPLETE
                    # office/Manager CLAUDE.md (with the office's orchestration
                    # guidance + house output style) instead of the degraded
                    # "No office content / default output style" fallback that
                    # would stand until the connector WS sync_config lands.
                    "claude_md_content": office_data.get("claude_md_content"),
                    "output_style": office_data.get("output_style"),
                    "agents": agents,
                    "workstreams": workstreams,
                    "scripts": [],
                }
            }
            # CTX-03: only materialise the workspace from a HEALTHY bootstrap.
            # On a degraded fetch, skip sync_all (which prunes agent/workstream
            # dirs) and let the connector WS sync_config populate authoritatively.
            if _bootstrap_ok:
                await config_store.update_from_sync(sync_msg)
                claude_md_writer.sync_all(sync_msg.get("config", {}))
                workspace_setup.sync_agent_workspaces(sync_msg.get("config", {}).get("agents", []))
                workspace_setup.sync_workstream_outputs(
                    sync_msg.get("config", {}).get("workstreams", [])
                )
                logger.info(
                    "Initial config loaded: %d agents, %d workstreams",
                    len(agents), len(workstreams),
                )
    except Exception as exc:
        logger.warning("Failed to fetch initial config from backend: %s", exc)

    # 3. Script management — scripts run inside the office container
    # by default (docker exec). Passing container_name here is what
    # switches ScriptRunner out of its host-Python fallback path.
    # config_store + manager are needed by the outbox watcher so it
    # can resolve workstreams and route script → Manager callbacks;
    # manager is created further down so we plumb it post-hoc below.
    variable_manager = VariableManager(office.workspace_path)
    secrets_store = SecretsStore(office.workspace_path)
    script_runner = ScriptRunner(
        workspace_path=office.workspace_path,
        secrets_store=secrets_store,
        variable_manager=variable_manager,
        ws_client=None,
        container_name=container_name,
        office_id=office.id,
        office_name=office.name,
        config_store=config_store,
    )
    # T8.3.3: let the script syncer defer stale-dir cleanup for scripts the
    # runner reports as mid-execution (created earlier than the runner, wired
    # now that both exist).
    script_syncer._has_active = script_runner.has_active_script

    # 4. Startup cleanup
    orphaned = script_runner.cleanup_orphaned_run_files()
    if orphaned:
        logger.info("Cleaned up %d orphaned _run.py file(s)", orphaned)

    # Reconcile executions a previous daemon left "running" against the
    # REAL in-container process state (ADD-C1). The container is reused
    # across restarts, so an orphaned-but-alive run must be killed +
    # honestly marked failed rather than blindly reported failed while
    # it keeps writing outputs (which made the Manager rework a run that
    # actually succeeded).
    stale = await reconcile_orphaned_script_executions(
        office.workspace_path, container_name,
    )
    if stale:
        logger.info("Reconciled %d stale script execution(s)", stale)

    # T4.3.3 (07/G12): reap orphan agent CLI sessions a crashed previous daemon
    # left running in this REUSED container, BEFORE the dispatcher's full_sync
    # re-queues + re-spawns the same tasks (which would double-execute). Script
    # subprocesses are unaffected — the reap pattern only matches `claude
    # --print`. Best-effort; never blocks bring-up.
    from src.recovery import reap_orphan_agent_sessions

    await reap_orphan_agent_sessions(container_name)

    # 4a. Schedule a backfill of on-disk script executions to the
    # backend DB. In split-host production the backend has no
    # filesystem access to the daemon's workspace, so historical
    # runs (especially anything older than cbcl 0.2.38 — i.e.
    # before the in-container reporter was added) never appear in
    # the Execution History panel. This one-shot scan publishes
    # every terminal-state ``status.json`` as a ``script_status``
    # event; the backend's existing ``handle_script_status``
    # handler upserts via (script_id, execution_id) so re-running
    # the backfill is idempotent. Deferred to after the WS connects
    # — see ``_run_history_backfill`` below.
    _history_backfill_workspace = Path(office.workspace_path)

    # 4b. Reap stale outbox .processing claims left by the previous
    # run. Done once at startup (never mid-loop — see
    # outbox_watcher.reap_stale_claims_on_startup docstring for why
    # per-tick reaping is the wrong fix for this race). Also rescans
    # any PRISTINE notify-*.json files left orphaned by an MCP-side
    # crash between script exit and _trigger_outbox_scan firing.
    # Without the pristine rescan, those drops would sit in .outbox/
    # forever (cbcl 0.2.49 bug).
    _pending_outbox_rescans: list[str] = []
    try:
        from pathlib import Path as _Path
        from src.scripts.outbox_watcher import reap_stale_claims_on_startup
        scripts_root = _Path(office.workspace_path) / ".scripts"
        if scripts_root.is_dir():
            total_reaped = 0
            for script_dir in scripts_root.iterdir():
                if not script_dir.is_dir():
                    continue
                try:
                    total_reaped += reap_stale_claims_on_startup(script_dir)
                except Exception:
                    logger.debug(
                        "outbox reap failed for %s (non-fatal)",
                        script_dir.name, exc_info=True,
                    )
                # Orphan-notify-rescan: collect script names that
                # have pristine ``.outbox/*.json`` files (no
                # ``.processing`` suffix). These are drops from a
                # previous-process MCP crash; trigger a scan once
                # the script_runner is wired.
                outbox = script_dir / ".outbox"
                if outbox.is_dir():
                    has_pristine = any(
                        f.is_file() and f.suffix == ".json"
                        for f in outbox.iterdir()
                    )
                    if has_pristine:
                        _pending_outbox_rescans.append(script_dir.name)
            if total_reaped:
                logger.info(
                    "Reaped %d stale outbox claim(s) at startup",
                    total_reaped,
                )
            if _pending_outbox_rescans:
                logger.info(
                    "Found pristine notify files in %d script outbox(es) "
                    "at startup — will rescan once script_runner is wired: %s",
                    len(_pending_outbox_rescans),
                    _pending_outbox_rescans[:10],
                )
    except Exception:
        logger.debug(
            "Startup outbox reap skipped (non-fatal)", exc_info=True,
        )

    # 5. Backend URL computation
    container_backend_url = platform_url.replace("localhost", "host.docker.internal")
    container_backend_url = container_backend_url.replace("127.0.0.1", "host.docker.internal")
    host_backend_url = platform_url
    logger.info("Container backend URL: %s", container_backend_url)
    logger.info("Host backend URL: %s", host_backend_url)

    # 6. Create AgentSupervisor
    from src.orchestrator.agent_supervisor import AgentSupervisor

    # 6b. Create ManagerController first (needed for on_event callback)
    mgr = ManagerController(
        supervisor=None,
        router=None,
        session_manager=session_manager,
        config_store=config_store,
        office_id=office.id,
        workspace_path=office.workspace_path,
        # T4.3.2: enable the give-up escalation POST (Bearer surface).
        backend_url=host_backend_url,
        security_token=security_token,
    )

    # Plumb the Manager reference into the ScriptRunner now that
    # both exist — the outbox watcher's per-tick scan needs to call
    # `mgr.ingest_script_message(...)` for script → Manager
    # callbacks. Using the setter (rather than reaching into the
    # private attr) documents the contract and logs the wiring.
    script_runner.set_manager(mgr)

    # Flush any pristine notify-*.json files we found at startup (per
    # the orphan-rescan collection in step 4b). These are
    # ``cubicle.notify_manager()`` drops from a previous process that
    # never made it through ``_trigger_outbox_scan`` (MCP crash, etc.).
    # Now that script_runner has both config_store + manager wired,
    # we can call scan_outbox_for() directly. Fire-and-forget — a
    # transient watcher failure on any one script doesn't block the
    # office init.
    if _pending_outbox_rescans:
        async def _flush_orphan_outboxes() -> None:
            for name in _pending_outbox_rescans:
                try:
                    delivered = await script_runner.scan_outbox_for(name)
                    if delivered:
                        logger.info(
                            "Startup orphan-notify reaper: delivered "
                            "%d drop(s) for script %s",
                            delivered, name,
                        )
                except Exception:
                    logger.warning(
                        "Startup orphan-notify reaper: scan failed for %s "
                        "(non-fatal — drops will be retried on next run)",
                        name, exc_info=True,
                    )
        _spawn_background(_flush_orphan_outboxes())

    # 7. Create AgentQueueManager (per-agent queues)
    queue_manager = AgentQueueManager(redis_client, office.id)

    # 8. Forward-declare dispatcher + router so the _on_agent_event closure
    # (registered on the supervisor below, before either exists) can
    # reference them. Late-binding makes this safe at run time today; the
    # explicit None guards against a NameError if an agent event ever fires
    # during init (e.g. an eager Manager spawn or a startup self-test).
    dispatcher = None  # Set after creation
    router = None  # Set after creation (WsTransport, step 10)

    # Flow Studio V2-P2: the per-office ``agent_channel`` emitter — the
    # live streaming overlay for Architect/Curator consults (chunk /
    # tool_start / tool_end / final / state frames on
    # ``flow-design:{flow_id}`` / ``collections-curate``). Best-effort by
    # contract: a relay failure never touches the consult itself or the
    # durable design_log / flow_consult_* poll path. Late-binds ``router``
    # (assigned in step 10) — a frame fired before the transport exists
    # is silently dropped here.
    async def _agent_channel_publish(frame: dict) -> None:
        if router is None:
            return
        await router.publish_event(frame)

    _agent_channel = AgentChannelEmitter(_agent_channel_publish)

    # Late-bound ref to the ``consult_planner`` command handler — it is
    # defined in ``_register_process_model_handlers`` (a different scope),
    # but the verdictless-verify refire below needs to re-dispatch the SAME
    # consult without a wire round-trip. Populated at handler-registration
    # time (step 12); same late-binding posture as ``dispatcher``/``router``.
    _consult_planner_ref: list = []

    async def _refire_verdictless_verify(consult: dict) -> None:
        """One-shot re-fire of a verify consult that ended VERDICTLESS
        (incident 2026-07-16 fix, part a2).

        Mirrors ``refire_verifying_scopes_for_office``'s posture — a
        recovery trigger, not a failure signal: the backend's
        ``verify_redispatch_count`` cap is untouched and the 900s
        stuck-verifying sweeper remains the durable backstop. The re-fired
        consult's marker carries ``_verdictless_refire`` so a SECOND
        verdictless exit is never re-fired from here (loop guard) — at that
        point recovery is deliberately left to the sweeper.
        """
        if consult.get("_verdictless_refire"):
            logger.warning(
                "Verify consult for scope %s ended verdictless AGAIN after "
                "the one-shot refire — leaving recovery to the backend "
                "stuck-verifying sweeper",
                consult.get("scope_id") or "?",
            )
            return
        handler = _consult_planner_ref[0] if _consult_planner_ref else None
        if handler is None:  # registration incomplete (test harness)
            return
        # AREA-2 single-flight: register the scope as refire-in-flight
        # for the whole idle-wait → spawn window, so a backend/sweeper-
        # fired verify for the same scope arriving inside it is dropped
        # by ``_handle_consult_planner`` instead of double-running.
        scope_key = str(consult.get("scope_id") or "")
        if scope_key and scope_key in _verify_refire_pending:
            logger.info(
                "verdictless verify re-fire for scope %s already in "
                "flight — dropping the duplicate (single-flight)",
                scope_key,
            )
            return
        if scope_key:
            _verify_refire_pending.add(scope_key)
        try:
            # LOW-8 shape: the supervisor flips the planner to IDLE only
            # after the completion callback returns; this coroutine runs in
            # the background ingest task, which can win that race. Wait
            # briefly so the spawn isn't refused as "planner already busy"
            # (verify-mode busy-drops are silent by design — the refire
            # would just vanish).
            for _ in range(20):
                if not supervisor.is_agent_busy("planner"):
                    break
                await asyncio.sleep(0.05)
            try:
                _attempt = int(consult.get("_verify_attempt") or 1)
            except (TypeError, ValueError):
                _attempt = 1
            refire = {
                "mode": "verify",
                "objective": consult.get("objective") or "",
                "workstream_id": consult.get("workstream_id") or "",
                "scope_id": consult.get("scope_id") or "",
                "approved_spec_reqs": consult.get("approved_spec_reqs") or [],
                "scope_covers": consult.get("scope_covers") or [],
                "_verdictless_refire": True,
                # AREA-2 honest cumulative elapsed: the refired attempt
                # inherits the FIRST attempt's start time + its ordinal so
                # the heartbeat/notices report total wall-clock instead of
                # resetting to zero per attempt.
                "_verify_first_started": consult.get("_verify_first_started"),
                "_verify_attempt": _attempt + 1,
            }
            logger.info(
                "Verify consult for scope %s ended verdictless — re-firing "
                "the same consult once (backend sweeper remains the "
                "backstop)",
                consult.get("scope_id") or "?",
            )
            try:
                await handler(refire)
            except Exception:
                logger.exception("verdictless verify re-fire failed")
        finally:
            _verify_refire_pending.discard(scope_key)

    async def _refire_consult_infra(consult: dict, reason: str) -> bool:
        """FIX P2/P3: one-shot re-fire of a NON-verify consult that died on
        a transient infra class or ended without its expected write —
        generalizes the verdictless-verify posture above.

        Safe to re-run: scope_plan/specify authoring is
        overwrite-convergent and materialize is idempotent on
        (scope, title) (``task_service.py``). The re-fired consult's
        marker carries ``_infra_refire`` so a SECOND death/missing
        outcome falls through to the honest Manager failure poke (loop
        guard). Returns ``True`` when the re-fire was dispatched — the
        caller then SUPPRESSES the failure poke (the mode-specific
        failure bodies say "re-consult", which would race the re-fired
        consult already running).
        """
        if consult.get("_infra_refire"):
            return False
        handler = _consult_planner_ref[0] if _consult_planner_ref else None
        if handler is None:  # registration incomplete (test harness)
            return False
        backoff = _INFRA_REFIRE_BACKOFF_SECONDS.get(reason, 0.0)
        mode = str(consult.get("mode") or "")
        logger.warning(
            "Planner %s consult died on %s — re-firing the same consult "
            "once after %.0fs (one-shot; the Manager failure poke is the "
            "fallback if the re-fire dies too)",
            mode or "?", reason, backoff,
        )
        # User-visible trace in the Planner's rail feed — a silent
        # re-fire must still leave a record of WHY the consult restarted.
        try:
            await _push_agent_feed("planner", {
                "type": "progress",
                "event_type": "checkpoint",
                "content": (
                    f"Consult ({mode}) interrupted ({reason}) — "
                    f"re-firing automatically"
                    + (f" in ~{int(backoff)}s" if backoff else "")
                    + "."
                ),
            })
        except Exception:
            logger.debug("infra-refire feed row failed", exc_info=True)
        if backoff > 0:
            await asyncio.sleep(backoff)
        # LOW-8 shape (same as the verdictless refire): wait briefly for
        # the supervisor's IDLE flip so the spawn isn't refused as busy.
        for _ in range(20):
            if not supervisor.is_agent_busy("planner"):
                break
            await asyncio.sleep(0.05)
        refire = {
            "mode": mode,
            "objective": consult.get("objective") or "",
            "workstream_id": consult.get("workstream_id") or "",
            "scope_id": consult.get("scope_id") or "",
            "approved_spec_reqs": consult.get("approved_spec_reqs") or [],
            "scope_covers": consult.get("scope_covers") or [],
            "_infra_refire": True,
        }
        try:
            await handler(refire)
        except Exception:
            logger.exception("infra consult re-fire failed (mode=%s)", mode)
            return False
        return True

    # -- Agent feed: lightweight Redis list for sidebar "Recent Activity" --
    # Helper extracted to ``_handlers._agent_feed`` (wave 13). The closure
    # captures the captured deps (office_id, redis_client, supervisor) so
    # the call site in ``_on_agent_event`` below stays a single-arg call.
    from src._handlers._agent_feed import push_agent_feed as _push_agent_feed_impl

    async def _push_agent_feed(agent_name: str, event: dict) -> None:
        await _push_agent_feed_impl(
            agent_name, event,
            office_id=office.id,
            redis_client=redis_client,
            supervisor=supervisor,
        )

    # Late-bound ref handed to ``_register_process_model_handlers`` so the
    # planner heartbeat's feed keepalive (a closure in that OTHER function
    # scope) can reach this office-bound push helper — same mutable-ref
    # posture as ``_consult_planner_ref``/``_watchdog_ref``. Without it the
    # heartbeat's ``_push_agent_feed`` reference was an unbound name: the
    # NameError landed in the heartbeat's swallow-all except and silently
    # killed BOTH the keepalive row and the stall watchdog on the first
    # pulse (incident 2026-07-16 follow-up, 2026-07-17).
    _push_agent_feed_ref: list = [_push_agent_feed]

    # HIGH-2: per-task infra-failure review re-queue counter shared by
    # the three re-queue sites (MA infra completion, designated-reviewer
    # infra completion, crashed-reviewer fatal). In-memory (mirrors
    # watchdog._task_crash_count); pruned on a genuine review
    # completion; daemon restart resets it.
    _review_infra_requeues: dict[str, int] = {}

    async def _dispatch_when_idle(agent: str) -> None:
        """LOW-8: the supervisor flips an agent to IDLE only AFTER the
        ``_on_agent_event`` callback returns, so an inline
        ``dispatch_agent`` for the SAME agent inside the callback is a
        guaranteed busy no-op (the re-dispatch then waits for the
        dispatcher's next poll tick). Spawned via ``_spawn_background``
        (T1.1.6 shape); waits briefly for the IDLE flip so the
        re-dispatch lands right after the callback. The dispatcher's
        2s poll remains the backstop if the agent stays busy."""
        for _ in range(20):
            if not supervisor.is_agent_busy(agent):
                break
            await asyncio.sleep(0.05)
        if dispatcher is not None:
            await dispatcher.dispatch_agent(agent)

    async def _requeue_review_capped(
        reviewer_agent: str,
        task_id: str,
        readable_id: str,
        error_summary: str,
    ) -> bool:
        """Re-queue an infra-failed review, bounded per task (HIGH-2).

        Returns ``True`` when the review was actually re-queued and
        ``False`` when the per-task cap refused it (round-2 LOW: call
        sites gate their "re-queued" logs on this so logs never lie).

        After ``REVIEW_INFRA_REQUEUE_CAP`` infra re-queues the task is
        LEFT in review with a loud activity — no move (review-state
        escalation is the backend sweeper's job at 30min).
        """
        count = _review_infra_requeues.get(task_id, 0)
        if count >= REVIEW_INFRA_REQUEUE_CAP:
            logger.warning(
                "Review re-queue cap (%d) reached for %s — NOT "
                "re-queuing to '%s' (last infra error: %s); leaving in "
                "review for the board sweeper / Manager",
                REVIEW_INFRA_REQUEUE_CAP, readable_id, reviewer_agent,
                error_summary,
            )
            await router.publish_event({
                "type": "task_activity",
                "task_id": task_id,
                "event_type": "error",
                "actor": "system",
                "content": (
                    f"Review re-queue cap reached ({count} infrastructure "
                    f"failures): {error_summary} — leaving in review for "
                    "the board sweeper / Manager."
                ),
            })
            return False
        _review_infra_requeues[task_id] = count + 1
        await queue_manager.add_task(reviewer_agent, {
            "task_id": task_id,
            "readable_id": readable_id,
            "reviewer": reviewer_agent,
            "status": "review",
            "priority": "urgent",
        })
        if dispatcher is not None:
            _spawn_background(
                _dispatch_when_idle(reviewer_agent),
                name=f"requeue-dispatch-{task_id[:8]}",
            )
        return True

    # Unified event handler: routes Manager events to ManagerController,
    # Worker events (progress, task_complete) to backend + queue updates.
    async def _on_agent_event(agent_name: str, event: dict) -> None:
        if agent_name == "manager":
            await mgr.handle_manager_event(agent_name, event)
        else:
            event_type = event.get("type", "")

            # Push to agent feed for sidebar visibility
            if event_type in ("progress", "task_complete", "error"):
                await _push_agent_feed(agent_name, event)

            # Stall-watchdog activity clock (incident 2026-08-04): stamp
            # the owning consult's stash entry on every Planner progress
            # frame so the per-consult heartbeat measures SILENCE, not
            # wall-clock. A healthy 40+ minute consult that is visibly
            # streaming tool activity must never be killed as "stalled";
            # a consult that stops producing output still dies at the
            # existing ceiling. The stash entry is the marker the
            # heartbeat already reads (notice sent-flags live there too).
            if agent_name == "planner" and event_type == "progress":
                _act_marker = _planner_consults.get(
                    str(event.get("task_id") or "")
                )
                if _act_marker is not None:
                    _act_marker["_last_activity_monotonic"] = (
                        time.monotonic()
                    )

            if event_type == "task_complete":
                task_id = event.get("task_id", "")
                new_status = event.get("status", "review")
                is_review_completion = event.get("is_review_completion", False)

                # Clear active task in queue manager.
                if dispatcher is not None:
                    await dispatcher.on_agent_complete(agent_name)

                # Flow Studio consult completion (FS-P3.T4): synthetic,
                # non-board assignment reporting to the REST poll path —
                # publish flow_consult_complete/_failed keyed by
                # request_id. NEVER a Manager chat poke (the design/curate
                # exchange lives in the Studio rail, not chat). Checked
                # BEFORE the planner branch so a flow marker can never
                # route into ingest_planner_result.
                if event.get("flow_consult") or task_id.startswith(
                    "flow-consult-"
                ):
                    stashed_fc = _flow_consults.pop(task_id, None)
                    fc_marker = (
                        event.get("flow_consult")
                        if isinstance(event.get("flow_consult"), dict)
                        else None
                    ) or stashed_fc or {}
                    fc_rid = str(fc_marker.get("request_id") or "")
                    fc_status = str(event.get("status") or "").strip().lower()
                    fc_details = event.get("details")
                    fc_err_class = (
                        str(fc_details.get("error_class") or "")
                        if isinstance(fc_details, dict)
                        else ""
                    )
                    fc_failed = fc_status in (
                        "blocked", "error", "failed", "cancelled",
                    ) or bool(fc_err_class)
                    if not fc_rid:
                        logger.warning(
                            "flow consult %s completed with no request_id "
                            "— nothing to report (poll will expire)",
                            task_id[:12],
                        )
                    else:
                        # The marker's flow_id rides the terminal event:
                        # the backend honours a daemon-supplied flow_id
                        # only when its seed has none (a curate consult
                        # that turned out flow-scoped) — without it that
                        # design-log path is unreachable.
                        fc_extra = (
                            {"flow_id": str(fc_marker.get("flow_id"))}
                            if fc_marker.get("flow_id")
                            else {}
                        )
                        fc_error_text = (
                            str(event.get("comment") or "").strip()
                            or (
                                f"the consult session failed "
                                f"({fc_err_class})"
                                if fc_err_class
                                else "the consult session ended "
                                "without completing"
                            )
                        )
                        fc_summary_text = (
                            str(event.get("summary") or "").strip()
                            or "Consult complete (the session "
                            "produced no report text)."
                        )
                        try:
                            if fc_failed:
                                await router.publish_event({
                                    "type": "flow_consult_failed",
                                    "request_id": fc_rid,
                                    "error": fc_error_text,
                                    **fc_extra,
                                })
                            else:
                                await router.publish_event({
                                    "type": "flow_consult_complete",
                                    "request_id": fc_rid,
                                    "summary": fc_summary_text,
                                    **fc_extra,
                                })
                        except Exception:
                            logger.exception(
                                "flow_consult terminal event publish failed "
                                "for %s", task_id[:12],
                            )
                        # V2-P2: mirror the terminal outcome on the live
                        # agent_channel overlay (best-effort — the emitter
                        # swallows every failure). ``final`` + ``done`` on
                        # success; ``failed`` on every failure shape, so
                        # the FE typing indicator can never hang.
                        if fc_failed:
                            await _agent_channel.relay_failed(
                                fc_marker, fc_error_text,
                            )
                        else:
                            await _agent_channel.relay_final(
                                fc_marker, fc_summary_text,
                            )
                    await router.publish_event({
                        "type": "agent_status_changed",
                        "agent_name": agent_name,
                        "display_name": agent_name,
                        "status": "idle",
                        "current_task": None,
                        "current_task_title": None,
                    })
                    return

                # Planner consult completion (execution_improvements_v1):
                # synthetic, non-board assignment. There is no task to move
                # — poke the Manager so it acts on the new plan, then mark
                # the planner idle. Skip the entire move/route flow.
                if event.get("planner_consult"):
                    # Round-2 LOW: prune the spawn-time consult stash on
                    # the clean-completion exit path. AREA-2: cancel the
                    # consult's heartbeat in the same breath — a refire
                    # respawns the Planner within ~1s of the idle flip,
                    # so an uncancelled heartbeat would re-latch onto the
                    # NEXT consult and keep pulsing its stale counter.
                    stashed_done = _planner_consults.pop(task_id, None)
                    _cancel_planner_heartbeat(task_id)
                    # Incident 2026-06-23: if the STALL watchdog killed this
                    # consult, its worker subprocess still emits a
                    # CancelledError ``task_complete`` with the generic
                    # comment "Task was cancelled." — SUPPRESS that poke.
                    # The watchdog owns the messaging: an auto-restart
                    # silently re-fires the SAME consult, and a cap kill
                    # emits ONE authoritative "stalled across N attempts"
                    # poke itself. Suppressing here is what collapses the
                    # mislabeled + duplicate Manager poke per stall (the
                    # Manager was treating the bare "cancelled" as an
                    # intentional user cancel and refusing to re-engage).
                    if stashed_done and stashed_done.get("_watchdog_killed"):
                        await router.publish_event({
                            "type": "agent_status_changed",
                            "agent_name": agent_name,
                            "display_name": agent_name,
                            "status": "idle",
                            "current_task": None,
                            "current_task_title": None,
                        })
                        return
                    # Clean completion — release any post-cap cooldown for
                    # this consult's (workstream, scope, mode) key so a
                    # later legitimate consult of the same body isn't
                    # blocked once the work actually progressed.
                    _planner_cap_cooldown.pop(
                        _cap_cooldown_key(event.get("planner_consult") or {}),
                        None,
                    )
                    # T1.1.6 (07/G9): ingest_planner_result runs a FULL
                    # Manager turn (the done-poke). The supervisor bounds
                    # this callback at 30s (agent_supervisor reader loop),
                    # so awaiting the turn inline got the poke cancelled
                    # almost every time the Manager was busy. Spawn it in
                    # the background so the callback returns immediately;
                    # nothing downstream here depends on the ingest
                    # finishing (the planner session itself is over, so
                    # the idle publication below is already truthful).
                    payload = dict(event)
                    consult_done = (
                        event.get("planner_consult")
                        if isinstance(event.get("planner_consult"), dict)
                        else {}
                    )

                    async def _ingest_planner_done() -> None:
                        try:
                            mode = (consult_done.get("mode") or "").strip()
                            status = str(
                                payload.get("status") or ""
                            ).strip().lower()
                            details = payload.get("details")
                            err_class = (
                                str(details.get("error_class") or "")
                                if isinstance(details, dict)
                                else ""
                            )
                            failed = bool(
                                payload.get("planner_error")
                            ) or status in (
                                "blocked", "error", "failed", "cancelled",
                            )
                            # FIX P2: a NON-verify consult whose worker
                            # session died on a transient INFRA class
                            # (retry-exhausted 529/429/timeout/drop) gets
                            # ONE silent daemon-side re-fire instead of the
                            # failure poke — during the very outage that
                            # killed the consult, the failure poke (a full
                            # Manager turn) usually fails too, and its
                            # "re-consult when ready" body would race a
                            # re-fired consult anyway. Loop-guarded by the
                            # ``_infra_refire`` marker flag (a second death
                            # falls through to the honest poke); verify
                            # keeps its own verdict-shaped path below.
                            if (
                                mode
                                and mode != "verify"
                                and failed
                                and err_class
                                in _INFRA_REFIRE_BACKOFF_SECONDS
                            ):
                                if await _refire_consult_infra(
                                    consult_done, err_class,
                                ):
                                    return
                            # Post-verify honesty check (incident
                            # 2026-07-16): a verify consult's clean exit is
                            # exit-code-shaped, not verdict-shaped — the
                            # session can end (ultracode subagent summaries,
                            # or every PASS refused by the backend gate)
                            # without ``complete_scope_verification`` ever
                            # being accepted. Before poking, fetch the scope:
                            # still ``verifying`` with a ``pending``
                            # verification means the session was verdictless
                            # — stamp ``planner_error`` so the ingest takes
                            # its existing verify FAILURE branch (which
                            # honestly says the backend re-fires/escalates)
                            # instead of the "has completed scope
                            # verification" success body, and re-fire the
                            # SAME consult once (one-shot, loop-guarded).
                            # Fails OPEN (fetch error keeps today's poke).
                            if mode == "verify":
                                recorded = (
                                    await _verify_consult_verdict_recorded(
                                        consult_done,
                                        platform_url=platform_url,
                                        office_id=str(office.id),
                                        security_token=security_token,
                                    )
                                )
                                if recorded is False:
                                    payload["planner_error"] = (
                                        "the Planner session ended WITHOUT "
                                        "recording a verdict "
                                        "(complete_scope_verification was "
                                        "never accepted)"
                                    )
                                    # AREA-1 fix 3: the worker counted
                                    # spawn tool_use ids whose result
                                    # never arrived at clean stream end
                                    # (``pending_spawns`` on the
                                    # completion payload) — NAME the
                                    # turn-end trap so the poke teaches
                                    # instead of mystifying.
                                    try:
                                        _pending = int(
                                            payload.get("pending_spawns")
                                            or 0
                                        )
                                    except (TypeError, ValueError):
                                        _pending = 0
                                    if _pending:
                                        payload["planner_error"] += (
                                            " — the session ended its "
                                            "turn with a workflow still "
                                            f"running ({_pending} "
                                            "unresolved subagent "
                                            "spawn(s); background work "
                                            "dies at turn end in "
                                            "headless mode)"
                                        )
                                    # Isolated try: a refire failure must
                                    # never cost the honest poke below.
                                    try:
                                        await _refire_verdictless_verify(
                                            consult_done
                                        )
                                    except Exception:
                                        logger.exception(
                                            "verdictless verify re-fire "
                                            "failed (sweeper remains the "
                                            "backstop)"
                                        )
                            # FIX P3: outcome gate for the non-verify
                            # authoring modes — before the success poke,
                            # verify the mode's ONE expected write actually
                            # landed (spec row touched / execution_plan
                            # present / ≥1
                            # complete-brief task). A clean exit without it
                            # is treated like an infra death: one silent
                            # re-fire (shared ``_infra_refire`` loop guard),
                            # then the honest failure poke. Fails OPEN on
                            # fetch errors — a backend blip must not convert
                            # real successes into failure pokes.
                            elif mode in _OUTCOME_GATED_MODES and not failed:
                                current = await _fetch_consult_outcome_state(
                                    consult_done, mode,
                                    platform_url=platform_url,
                                    office_id=str(office.id),
                                    security_token=security_token,
                                )
                                advanced = _consult_outcome_advanced(
                                    consult_done.get("_pre_outcome"),
                                    current,
                                )
                                if advanced is False:
                                    if await _refire_consult_infra(
                                        consult_done, "missing_outcome",
                                    ):
                                        return
                                    payload["planner_error"] = (
                                        "the Planner session ended WITHOUT "
                                        f"persisting the {mode} output "
                                        "(no plan/spec write was accepted)"
                                    )
                            await mgr.ingest_planner_result(payload)
                        except Exception:
                            logger.exception(
                                "ingest_planner_result failed for planner consult"
                            )

                    _spawn_background(
                        _ingest_planner_done(),
                        name=f"planner-ingest-{task_id[:8]}",
                    )
                    await router.publish_event({
                        "type": "agent_status_changed",
                        "agent_name": agent_name,
                        "display_name": agent_name,
                        "status": "idle",
                        "current_task": None,
                        "current_task_title": None,
                    })
                    return

                # Publish agent idle AFTER task move completes (in finally block).
                # This avoids the race condition where UI shows "idle" but task
                # is still in_progress because the move hasn't happened yet.
                async def _publish_agent_idle():
                    await router.publish_event({
                        "type": "agent_status_changed",
                        "agent_name": agent_name,
                        "display_name": agent_name,
                        "status": "idle",
                        "current_task": None,
                        "current_task_title": None,
                    })

                if not is_review_completion:
                    # EXECUTOR completed: move to target status, then route.
                    #
                    # T1.1.6 (07/G18): the supervisor bounds this callback at
                    # 30s. The MOVE stays inline (one fast HTTP POST — board
                    # state must be consistent before the idle publication
                    # and the supervisor's IDLE flip), but the ROUTING leg
                    # (task fetch + queue add + dispatch_agent, which can
                    # spawn a reviewer subprocess) runs in a background task
                    # (module-level ``_route_completed_task``) so a slow
                    # backend / spawn can't blow the 30s budget and get the
                    # reviewer dispatch cancelled.
                    import httpx

                    from src.backend_client import auth_headers as _ah
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            move_resp = await client.post(
                                f"{platform_url}/api/offices/{office.id}/tool-call",
                                json={"action": "move_task", "params": {
                                    "task_id": task_id,
                                    "new_status": new_status,
                                    "actor": agent_name,
                                    "comment": event.get("comment", ""),
                                }},
                                # SEC3-01: Company-Token bearer (daemon-side).
                                headers=_ah(security_token),
                            )
                            if move_resp.status_code == 200:
                                move_result = move_resp.json() if move_resp.status_code == 200 else {}
                                old_status = move_result.get("old_status", "")
                                actual_new = move_result.get("new_status", new_status)

                                # If old_status == new_status, the move was a no-op
                                # (task already in target status — the MCP tool call
                                # already moved it). Skip routing entirely to avoid
                                # double-dispatch.
                                if old_status == actual_new:
                                    logger.info(
                                        "Task %s already in %s — skip routing (handled by task_moved event)",
                                        task_id[:8], actual_new,
                                    )
                                elif new_status in ("review", "blocked"):
                                    logger.info(
                                        "Moved task %s to %s — routing in background",
                                        task_id[:8], new_status,
                                    )
                                    _spawn_background(
                                        _route_completed_task(
                                            task_id, new_status,
                                            platform_url=platform_url,
                                            office_id=str(office.id),
                                            security_token=security_token,
                                            config_store=config_store,
                                            queue_manager=queue_manager,
                                            dispatcher=dispatcher,
                                        ),
                                        name=f"route-complete-{task_id[:8]}",
                                    )
                            else:
                                logger.warning("Failed to move task %s: %s", task_id[:8], move_resp.text[:200])
                    except Exception as exc:
                        logger.warning("Task completion handling failed: %s", exc)
                    finally:
                        # Always mark agent idle — even if task move failed
                        await _publish_agent_idle()
                else:
                    # REVIEW-MODE COMPLETION. Three cases:
                    # A) MA (Board Operator) completed — no action needed.
                    # B) Designated reviewer completed — they should have
                    #    already moved the task to done/ready. Verify.
                    # C) Non-designated reviewer (old flow) — unassign, MA.
                    if agent_name == "manager-assistant":
                        # MA completed Board Operator work. For MA specifically
                        # (the default reviewer for tasks without a designated
                        # specialist reviewer), a clean session end after a
                        # positive review is treated as APPROVE. MA is the
                        # "benefit-of-the-doubt" reviewer — the circuit-breaker
                        # rework logic only applies to custom designated
                        # reviewers (editors, auditors, etc.) who are expected
                        # to make an explicit decision.
                        import httpx
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                from src.backend_client import auth_headers as _auth_headers
                                task_resp = await client.get(
                                    f"{platform_url}/api/offices/{office.id}/tasks/{task_id}",
                                    headers=_auth_headers(security_token),
                                )
                                task_info = task_resp.json() if task_resp.status_code == 200 else {}
                                task_status = task_info.get("status", "")
                                readable_id = task_info.get("readable_id") or task_id[:8]

                                # ADD-A5 (+C1 fix): never auto-approve a SKIPPED
                                # MA session (no deliverables read, no verdict) —
                                # that would ship unreviewed work to done. And
                                # never re-dispatch in a tight loop: the worker
                                # SKIPS when the MA is neither assigned_agent nor
                                # reviewer (a task with no designated reviewer),
                                # so a blind re-queue would re-skip forever. The
                                # helper decides approve / authorize_requeue /
                                # noop using whether the MA is already the
                                # reviewer.
                                from src._handlers._tasks import (
                                    decide_ma_review_completion,
                                )
                                ma_is_reviewer = (
                                    (task_info.get("reviewer") or "")
                                    == "manager-assistant"
                                )
                                decision = decide_ma_review_completion(
                                    task_status,
                                    bool(event.get("review_skipped")),
                                    ma_is_reviewer=ma_is_reviewer,
                                )
                                # Parity with the designated-reviewer branch
                                # (T1.1.3): an infra-failure completion
                                # (error_class on the event — e.g. a retry-
                                # exhausted reviewer session) did NO real
                                # review. Never auto-approve it; re-queue the
                                # review urgently instead.
                                # NOTE: despite the name, this captures ANY
                                # error_class stamped on a review-mode
                                # completion (not only the infra subset
                                # rate_limited/timeout/...). The rationale is
                                # the same for all of them — a class-stamped
                                # "completion" did NO real review, so it must
                                # not consume a rework cycle or auto-approve;
                                # re-queue instead (bounded by
                                # REVIEW_INFRA_REQUEUE_CAP).
                                infra_error_class = (
                                    event.get("error_class")
                                    or (event.get("details") or {}).get("error_class")
                                )
                                if not infra_error_class:
                                    # HIGH-2: a genuine (non-infra) review
                                    # completion resets the infra re-queue
                                    # budget for this task.
                                    _review_infra_requeues.pop(task_id, None)
                                if task_status == "review" and infra_error_class:
                                    # Round-2 LOW: log AFTER the capped
                                    # helper, gated on its result, so a
                                    # cap-refused re-queue never logs as
                                    # "re-queued" (the helper logs the
                                    # cap warning itself).
                                    if await _requeue_review_capped(
                                        "manager-assistant", task_id, readable_id,
                                        f"MA review session ended with infra "
                                        f"error (class={infra_error_class})",
                                    ):
                                        logger.warning(
                                            "MA review session on %s ended with infra error "
                                            "(class=%s) — re-queued review without "
                                            "auto-approving",
                                            readable_id, infra_error_class,
                                        )
                                elif decision == "approve":
                                    # GUARD (parity with the designated-
                                    # reviewer circuit breaker): never
                                    # auto-approve over a live escalation. A
                                    # pending action request sourced from
                                    # this task means "parked on a human" —
                                    # a force-done here would bury the
                                    # pending decision.
                                    from src.backend_client import (
                                        task_has_pending_action_request,
                                    )
                                    has_pending_ar = await task_has_pending_action_request(
                                        platform_url=platform_url,
                                        office_id=str(office.id),
                                        task_id=task_id,
                                        security_token=security_token,
                                    )
                                    if has_pending_ar is None:
                                        # HIGH-1: the pending-AR lookup FAILED
                                        # — fail CLOSED. Approving over a
                                        # possibly-live escalation would bury
                                        # the pending human decision; leave
                                        # the task in review instead.
                                        logger.warning(
                                            "MA completed review of %s but the "
                                            "pending-action-request lookup failed — "
                                            "leaving in review (fail-closed, NOT "
                                            "auto-approving)",
                                            readable_id,
                                        )
                                    elif has_pending_ar:
                                        logger.warning(
                                            "MA completed review of %s but a pending "
                                            "action request exists — leaving in review "
                                            "(escalation is live, NOT auto-approving)",
                                            readable_id,
                                        )
                                    else:
                                        logger.info(
                                            "MA completed review of %s without moving — auto-approving",
                                            readable_id,
                                        )
                                        await client.post(
                                            f"{platform_url}/api/offices/{office.id}/tool-call",
                                            json={"action": "move_task", "params": {
                                                "task_id": task_id,
                                                "new_status": "done",
                                                "actor": "manager-assistant",
                                                "comment": "Auto-approved after review completion.",
                                            }},
                                            headers=auth_headers(security_token),
                                        )
                                        # LOUD, user-visible marker (parity
                                        # with the designated-reviewer
                                        # branch): the approval was
                                        # mechanical, not an explicit
                                        # reviewer verdict.
                                        await router.publish_event({
                                            "type": "task_activity",
                                            "task_id": task_id,
                                            "event_type": "review_approved",
                                            "actor": "manager-assistant",
                                            "content": (
                                                "AUTO-APPROVED (circuit breaker): the "
                                                "Manager Assistant completed the review "
                                                "without an explicit verdict. Please "
                                                "double-check this deliverable."
                                            ),
                                        })
                                elif decision == "authorize_requeue":
                                    # Skipped because the MA wasn't authorized
                                    # (no designated reviewer). Designate the MA
                                    # as reviewer so the retry is authorized and
                                    # does a REAL review — bounded to ONE retry
                                    # (next time ma_is_reviewer is True → noop).
                                    logger.warning(
                                        "MA review of %s skipped (unauthorized) — "
                                        "designating MA as reviewer and retrying once",
                                        readable_id,
                                    )
                                    # C2: only re-dispatch if the reviewer write
                                    # actually PERSISTED. httpx doesn't raise on
                                    # a non-200, so a failed write + blind
                                    # re-dispatch would re-skip → unbounded loop.
                                    # On failure, leave the task for the
                                    # reconciler / stuck-review sweeper.
                                    from src.backend_client import (
                                        designate_ma_reviewer,
                                    )
                                    persisted = await designate_ma_reviewer(
                                        platform_url, str(office.id), task_id,
                                        security_token,
                                    )
                                    if persisted:
                                        await queue_manager.add_task("manager-assistant", {
                                            "task_id": task_id,
                                            "readable_id": readable_id,
                                            "reviewer": "manager-assistant",
                                            "status": "review",
                                            "priority": "urgent",
                                        })
                                        if dispatcher is not None:
                                            await dispatcher.dispatch_agent("manager-assistant")
                                    else:
                                        logger.warning(
                                            "Could not designate MA as reviewer "
                                            "for %s — leaving for the sweeper "
                                            "instead of re-dispatching blind",
                                            readable_id,
                                        )
                                else:
                                    logger.info("MA completed review of %s (already %s, skipped=%s)", readable_id, task_status, bool(event.get("review_skipped")))
                        except Exception as exc:
                            logger.warning("MA review completion check failed: %s", exc)
                        await _publish_agent_idle()
                    else:
                        # Check if this agent is the designated reviewer
                        # and whether they already moved the task.
                        import httpx
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                from src.backend_client import auth_headers as _auth_headers
                                task_resp = await client.get(
                                    f"{platform_url}/api/offices/{office.id}/tasks/{task_id}",
                                    headers=_auth_headers(security_token),
                                )
                                task_info = task_resp.json() if task_resp.status_code == 200 else {}
                                task_status = task_info.get("status", "review")
                                designated = task_info.get("reviewer") or ""
                                readable_id = task_info.get("readable_id") or task_id[:8]

                                if designated == agent_name and task_status in ("done", "ready", "archived"):
                                    # Task already moved or archived — clean completion.
                                    logger.info(
                                        "Reviewer %s completed task %s (now %s) — no action needed",
                                        agent_name, readable_id, task_status,
                                    )
                                elif (
                                    designated == agent_name
                                    and task_status == "review"
                                    and bool(event.get("review_skipped"))
                                ):
                                    # ADD-A5 (L1): a SKIPPED designated-reviewer
                                    # session did no real review — never bump
                                    # rework_count or auto-approve on it. Leave
                                    # the task in review; the reconciler/sweeper
                                    # recovers. (Latent today — a designated
                                    # reviewer only skips when unauthorized,
                                    # which diverts to the else branch — but
                                    # keeps the skip semantics consistent with
                                    # the MA branch and future-proofs it.)
                                    logger.info(
                                        "Reviewer %s review of %s was skipped "
                                        "(no work) — leaving in review",
                                        agent_name, readable_id,
                                    )
                                elif designated == agent_name and task_status == "review":
                                    # Reviewer completed WITHOUT moving task.
                                    # T1.1.3 (07/G3+G3b) decision tree:
                                    # - infra-failure completion (error_class on the
                                    #   event) → re-queue the review urgently; the
                                    #   review→ready move is what increments
                                    #   rework_count backend-side, so skipping the
                                    #   move = NOT consuming a rework cycle on an
                                    #   infrastructure fault.
                                    # - rework_count >= cap + pending action request
                                    #   → the reviewer's mandated escalate-at-cap is
                                    #   LIVE; leave in review, never force-done over
                                    #   a pending human decision.
                                    # - rework_count >= cap, no pending AR → auto-
                                    #   approve (circuit breaker) with a LOUD
                                    #   user-visible activity.
                                    # - below cap, genuine ambiguity → return for
                                    #   rework (unchanged).
                                    rework_count = int(task_info.get("rework_count") or 0)
                                    max_rework = get_max_rework_cycles(config_store)
                                    infra_error_class = (
                                        event.get("error_class")
                                        or (event.get("details") or {}).get("error_class")
                                    )
                                    if not infra_error_class:
                                        # HIGH-2: genuine completion resets
                                        # the infra re-queue budget.
                                        _review_infra_requeues.pop(task_id, None)
                                    if infra_error_class:
                                        # Round-2 LOW: gate on the capped
                                        # helper's result so a cap-refused
                                        # re-queue never logs as "re-queued".
                                        if await _requeue_review_capped(
                                            agent_name, task_id, readable_id,
                                            f"reviewer session ended with infra "
                                            f"error (class={infra_error_class})",
                                        ):
                                            logger.warning(
                                                "Reviewer %s session on %s ended with infra error "
                                                "(class=%s) — re-queued review without consuming "
                                                "a rework cycle",
                                                agent_name, readable_id, infra_error_class,
                                            )
                                    elif rework_count >= max_rework:
                                        # GUARD: never auto-approve over a live
                                        # escalation. The reviewer prompt mandates
                                        # escalate-at-cap (an action request), which
                                        # is the exact trigger of this branch — a
                                        # force-done here would bury the pending
                                        # human decision. We check for ANY pending
                                        # AR sourced from this task (not just
                                        # escalate_blocker): every pending AR means
                                        # "parked on a human" regardless of type
                                        # (request_clarification, escalate_blocker,
                                        # …), same semantics the blocked-routing
                                        # skip uses via task_should_skip_ma_routing.
                                        from src.backend_client import (
                                            task_has_pending_action_request,
                                        )
                                        has_pending_ar = await task_has_pending_action_request(
                                            platform_url=platform_url,
                                            office_id=str(office.id),
                                            task_id=task_id,
                                            security_token=security_token,
                                        )
                                        if has_pending_ar is None:
                                            # HIGH-1: lookup FAILED — fail
                                            # CLOSED. A force-done over a
                                            # possibly-live escalation would
                                            # bury the pending human decision.
                                            logger.warning(
                                                "Reviewer %s completed %s at the rework cap "
                                                "(%d) but the pending-action-request lookup "
                                                "failed — leaving in review (fail-closed, "
                                                "NOT auto-approving)",
                                                agent_name, readable_id, rework_count,
                                            )
                                        elif has_pending_ar:
                                            logger.warning(
                                                "Reviewer %s completed %s at the rework cap "
                                                "(%d) but a pending action request exists — "
                                                "leaving in review (escalation is live, NOT "
                                                "auto-approving)",
                                                agent_name, readable_id, rework_count,
                                            )
                                        else:
                                            logger.warning(
                                                "Reviewer %s completed task %s, rework_count=%d "
                                                "(>=%d) and no pending escalation — auto-approving "
                                                "(circuit breaker)",
                                                agent_name, readable_id, rework_count, max_rework,
                                            )
                                            try:
                                                await client.post(
                                                    f"{platform_url}/api/offices/{office.id}/tool-call",
                                                    json={"action": "move_task", "params": {
                                                        "task_id": task_id,
                                                        "new_status": "done",
                                                        "actor": agent_name,
                                                        "comment": f"Auto-approved — reviewer completed after {rework_count} rework cycles (circuit breaker).",
                                                    }},
                                                    headers=auth_headers(security_token),
                                                )
                                                # LOUD, user-visible marker: the move's
                                                # status_changed activity alone is easy
                                                # to miss; this review_approved entry
                                                # names the circuit breaker explicitly
                                                # so the user knows the approval was
                                                # mechanical, not a reviewer verdict.
                                                await router.publish_event({
                                                    "type": "task_activity",
                                                    "task_id": task_id,
                                                    "event_type": "review_approved",
                                                    "actor": agent_name,
                                                    "content": (
                                                        "AUTO-APPROVED (circuit breaker): the "
                                                        "reviewer completed without an explicit "
                                                        f"verdict after {rework_count} rework "
                                                        "cycles. Please double-check this "
                                                        "deliverable."
                                                    ),
                                                })
                                            except Exception:
                                                # logger.exception (not warning) — a failed
                                                # circuit-breaker auto-approve leaves the task
                                                # stuck in `review`; capture the cause (HTTP /
                                                # body error), don't swallow it.
                                                logger.exception(
                                                    "Auto-approve failed for %s", readable_id
                                                )
                                    else:
                                        logger.info(
                                            "Reviewer %s completed task %s without moving (rework_count=%d) — returning for rework",
                                            agent_name, readable_id, rework_count,
                                        )
                                        try:
                                            await client.post(
                                                f"{platform_url}/api/offices/{office.id}/tool-call",
                                                json={"action": "move_task", "params": {
                                                    "task_id": task_id,
                                                    "new_status": "ready",
                                                    "actor": agent_name,
                                                    "comment": "Reviewer completed without explicit approval — returned for rework. Please address reviewer feedback in activity.",
                                                }},
                                                headers=auth_headers(security_token),
                                            )
                                        except Exception:
                                            logger.warning("Return-for-rework failed for %s", readable_id)
                                elif designated == agent_name:
                                    # Task in unexpected status — do nothing, don't re-queue.
                                    logger.warning(
                                        "Reviewer %s completed, task %s in unexpected status '%s' — skipping",
                                        agent_name, readable_id, task_status,
                                    )
                                else:
                                    # A reviewing agent that is NOT the task's
                                    # designated reviewer completed (legacy /
                                    # anomalous — the dispatcher routes review to
                                    # the reviewer, so this is rare). Log the
                                    # verdict and hand the review to the Board
                                    # Operator (Manager Assistant) to resolve.
                                    # Do NOT unassign — the executor stays
                                    # assigned (no-unassign-after-Ready invariant;
                                    # the backend drops the clear anyway).
                                    await router.publish_event({
                                        "type": "task_activity",
                                        "task_id": task_id,
                                        "event_type": "checkpoint",
                                        "actor": agent_name,
                                        "content": event.get("comment", "Review complete."),
                                        "token_cost": event.get("token_cost", 0),
                                    })
                                    await queue_manager.add_task("manager-assistant", {
                                        "task_id": task_id,
                                        "readable_id": readable_id,
                                        "status": "review",
                                        "priority": "urgent",
                                    })
                                    if dispatcher is not None:
                                        await dispatcher.dispatch_agent("manager-assistant")
                        except Exception as exc:
                            logger.warning("Reviewer completion handling failed: %s", exc)
                        finally:
                            await _publish_agent_idle()

            elif event_type == "progress":
                details = event.get("details")
                if details and not isinstance(details, (dict, list)):
                    details = None
                activity_payload = {
                    "type": "task_activity",
                    "task_id": event.get("task_id", ""),
                    "event_type": event.get("event_type", "checkpoint"),
                    "actor": agent_name,
                    "content": event.get("content", ""),
                    "details": details,
                    "token_cost": event.get("token_cost"),
                }
                # FIX P1: a Planner consult's activities ride a SYNTHETIC
                # task id (``planner-<uuid>``) that has no backend task
                # row — the backend used to drop them on ``uuid.UUID()``
                # (no durable record; retries like "Recovering from
                # api_overloaded" only lived in the 1h Redis feed). Thread
                # the consult's context_key/mode from the spawn-time stash
                # so the backend can persist them as a consult-log
                # ``manager_events`` row in the right workstream context.
                _pid = str(event.get("task_id") or "")
                if _pid.startswith("planner-"):
                    _stash = _planner_consults.get(_pid) or {}
                    _ws = str(_stash.get("workstream_id") or "")
                    activity_payload["context_key"] = (
                        f"workstream:{_ws}" if _ws else "general_chat"
                    )
                    activity_payload["consult_mode"] = (
                        _stash.get("mode") or ""
                    )
                # Flow Studio consult (FS-P3.T4): the synthetic id has no
                # backend task row, so the task_activity publish would
                # just be dropped on uuid parse — relay a THROTTLED
                # ``flow_consult_progress`` instead so the Studio rail's
                # poll shows live progress AND the status row's TTL keeps
                # refreshing across a long extraction. The agent feed
                # push above already covers sidebar visibility.
                if _pid.startswith("flow-consult-"):
                    _fc_stash = _flow_consults.get(_pid)
                    # V2-P2: the live agent_channel overlay — checkpoint
                    # text rides as coalesced ``chunk`` frames and tool
                    # telemetry as ``tool_start``/``tool_end`` pairs,
                    # UN-throttled by the 10s poll pulse below (the
                    # emitter's own ≤10/s coalescer bounds it).
                    # Best-effort: the emitter swallows every failure.
                    if _fc_stash is not None:
                        await _agent_channel.relay_progress(
                            _fc_stash, event,
                        )
                    _fc_msg = str(event.get("content") or "").strip()
                    if _fc_stash is not None and _fc_msg:
                        _fc_now = time.monotonic()
                        _fc_last = float(
                            _fc_stash.get("_last_progress_pub") or 0.0
                        )
                        if (
                            _fc_now - _fc_last
                            >= _FLOW_CONSULT_PROGRESS_MIN_INTERVAL_SECONDS
                        ):
                            _fc_stash["_last_progress_pub"] = _fc_now
                            try:
                                await router.publish_event({
                                    "type": "flow_consult_progress",
                                    "request_id": str(
                                        _fc_stash.get("request_id") or ""
                                    ),
                                    "message": _fc_msg[:300],
                                })
                            except Exception:
                                logger.exception(
                                    "flow_consult_progress publish failed"
                                )
                    return
                await router.publish_event(activity_payload)
            elif event_type == "error":
                is_fatal = event.get("fatal", False)
                task_id = event.get("task_id") or ""
                logger.warning("Worker %s error (fatal=%s): %s", agent_name, is_fatal, event.get("message", ""))
                # Flow Studio consult error (FS-P3.T4): synthetic task, no
                # board recovery possible — publish the honest
                # ``flow_consult_failed`` so the REST poll never hangs.
                # Supervisor-synthesized fatals (heartbeat kill, process
                # exit) carry no marker, so ALSO match by agent name /
                # synthetic id and recover the request_id from the
                # spawn-time stash (the MEDIUM-4 planner lesson). Checked
                # BEFORE the planner branch.
                if (
                    event.get("flow_consult")
                    or agent_name in ("flow-architect", "data-curator")
                    or task_id.startswith("flow-consult-")
                ):
                    stashed_fc = _flow_consults.pop(task_id, None)
                    fc_marker = (
                        event.get("flow_consult")
                        if isinstance(event.get("flow_consult"), dict)
                        else None
                    ) or stashed_fc or {}
                    fc_rid = str(fc_marker.get("request_id") or "")
                    fc_death_text = (
                        str(event.get("message") or "").strip()
                        or "the consult session was killed "
                        f"({event.get('reason') or 'crash'})"
                    )
                    if fc_rid:
                        try:
                            await router.publish_event({
                                "type": "flow_consult_failed",
                                "request_id": fc_rid,
                                "error": fc_death_text,
                            })
                        except Exception:
                            logger.exception(
                                "flow_consult_failed publish failed for %s",
                                task_id[:12],
                            )
                        # V2-P2: flip the live channel to failed so the FE
                        # typing indicator can never hang on a dead
                        # session. A supervisor-synthesized fatal may
                        # carry no ``kind`` — fall back to the agent's
                        # surface (the curator channel is office-wide, so
                        # a markerless curator death is still
                        # addressable; a flow_id-less architect death is
                        # not, and the emitter skips it).
                        await _agent_channel.relay_failed(
                            {
                                "request_id": fc_rid,
                                "kind": (
                                    str(fc_marker.get("kind") or "")
                                    or (
                                        "collections_curate"
                                        if agent_name == "data-curator"
                                        else "flow_design"
                                    )
                                ),
                                "flow_id": str(
                                    fc_marker.get("flow_id") or ""
                                ),
                            },
                            fc_death_text,
                        )
                    else:
                        logger.warning(
                            "flow consult error for %s with no recoverable "
                            "request_id — poll will expire (%s)",
                            agent_name, task_id[:12],
                        )
                    if dispatcher is not None:
                        await queue_manager.clear_active(agent_name)
                    await router.publish_event({
                        "type": "agent_status_changed",
                        "agent_name": agent_name,
                        "display_name": agent_name,
                        "status": "idle",
                        "current_task": None,
                        "current_task_title": None,
                    })
                    return
                # Planner consult error: synthetic task, no board recovery
                # possible. Poke the Manager with a failure note (else it was
                # told "engaged" and waits forever) and mark planner idle.
                # Phase 3 robustness — applies regardless of is_fatal.
                #
                # MEDIUM-4: supervisor-SYNTHESIZED fatal events (heartbeat
                # kill, process exit) carry no planner_consult marker, so a
                # wedged-then-killed Planner used to fall through to the
                # board-recovery branch below (404 task fetch on the
                # synthetic id, no poke — the Manager waited on "engaged"
                # forever). Detect the Planner by agent name / synthetic
                # task id and route it here too; without the consult marker
                # the poke lands with the generic failure body.
                if event.get("planner_consult") or (
                    agent_name == "planner" or task_id.startswith("planner-")
                ):
                    # Round-2 LOW: pop the spawn-time consult stash on
                    # this exit path too (worker-emitted error OR kill).
                    # When the event carries no marker (supervisor-
                    # synthesized kill), the stashed marker recovers the
                    # consult's real mode/context_key instead of the
                    # specify/general_chat defaults. AREA-2: cancel the
                    # consult's heartbeat here too (same leak window as
                    # the clean-completion pop above).
                    stashed_consult = _planner_consults.pop(task_id, None)
                    _cancel_planner_heartbeat(task_id)
                    # Incident 2026-06-23: if the STALL watchdog killed this
                    # consult (auto-restart or cap), a SIGKILL'd worker
                    # surfaces here as a supervisor-synthesized fatal event.
                    # SUPPRESS the poke — the watchdog owns the messaging
                    # (cap → one authoritative "stalled across N attempts"
                    # poke; auto-restart → silent re-fire). This mirrors the
                    # graceful-SIGTERM suppression on the task_complete path
                    # so a watchdog kill produces EXACTLY ONE Manager poke
                    # regardless of whether the subprocess exited cleanly.
                    if stashed_consult and stashed_consult.get(
                        "_watchdog_killed"
                    ):
                        if dispatcher is not None:
                            await queue_manager.clear_active(agent_name)
                        await router.publish_event({
                            "type": "agent_status_changed",
                            "agent_name": agent_name,
                            "display_name": agent_name,
                            "status": "idle",
                            "current_task": None,
                            "current_task_title": None,
                        })
                        return
                    recovered_consult = (
                        stashed_consult
                        if not event.get("planner_consult") else None
                    )
                    if (
                        recovered_consult
                        and (recovered_consult.get("mode") or "").strip()
                        == "verify"
                    ):
                        # Same verify-silence rule as the consult-drop
                        # path (_poke_failure): a verify consult is
                        # BACKEND-fired (scope auto-enters `verifying`),
                        # so a killed verify must NOT poke the Manager
                        # about a consult it never issued — the
                        # stuck-verifying sweeper owns re-fire/escalate.
                        # Just clean up (clear_active + idle) and stop.
                        logger.info(
                            "Planner verify consult %s killed (%s) — "
                            "backend-fired; the stuck-verifying sweeper "
                            "will re-fire/escalate, not poking the "
                            "Manager",
                            task_id[:8],
                            event.get("reason")
                            or event.get("message")
                            or "fatal error",
                        )
                        if dispatcher is not None:
                            await queue_manager.clear_active(agent_name)
                        await router.publish_event({
                            "type": "agent_status_changed",
                            "agent_name": agent_name,
                            "display_name": agent_name,
                            "status": "idle",
                            "current_task": None,
                            "current_task_title": None,
                        })
                        return
                    # T1.1.6 (07/G9): same 30s-callback bound as the done
                    # path — the failure poke is a full Manager turn, so
                    # spawn it in the background and finish the cleanup
                    # (clear_active + idle publication) inline.
                    default_planner_error = (
                        "the Planner session ended with an error"
                        if event.get("planner_consult")
                        else (
                            "the Planner session was killed "
                            f"({event.get('reason') or 'heartbeat timeout/crash'})"
                        )
                    )
                    error_payload = {
                        **event,
                        "planner_error": (
                            event.get("message") or default_planner_error
                        ),
                    }
                    if recovered_consult:
                        # Non-verify kill: poke with the consult's real
                        # mode/context_key (else ingest_planner_result
                        # defaults to specify/general_chat).
                        error_payload["planner_consult"] = recovered_consult

                    async def _ingest_planner_error() -> None:
                        try:
                            await mgr.ingest_planner_result(error_payload)
                        except Exception:
                            logger.exception(
                                "ingest_planner_result failed for planner error"
                            )

                    _spawn_background(
                        _ingest_planner_error(),
                        name=f"planner-error-ingest-{task_id[:8]}",
                    )
                    if dispatcher is not None:
                        await queue_manager.clear_active(agent_name)
                    await router.publish_event({
                        "type": "agent_status_changed",
                        "agent_name": agent_name,
                        "display_name": agent_name,
                        "status": "idle",
                        "current_task": None,
                        "current_task_title": None,
                    })
                    return
                if is_fatal and dispatcher is not None:
                    # LOW-5: only clear the active marker when it still
                    # points at THIS event's task — a late fatal event for
                    # an older task must not wipe the marker of a newer
                    # assignment the dispatcher already made.
                    active = await queue_manager.get_active(agent_name)
                    active_task_id = (active or {}).get("task_id") or ""
                    if not task_id or not active_task_id or active_task_id == task_id:
                        await queue_manager.clear_active(agent_name)
                    else:
                        logger.info(
                            "Fatal event for %s task %s but active marker is "
                            "%s — leaving the marker in place",
                            agent_name, task_id[:8], active_task_id[:8],
                        )
                    if task_id:
                        import httpx
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                # Fetch task to check status and reviewer.
                                from src.backend_client import auth_headers as _auth_headers
                                task_resp = await client.get(
                                    f"{platform_url}/api/offices/{office.id}/tasks/{task_id}",
                                    headers=_auth_headers(security_token),
                                )
                                task_info = task_resp.json() if task_resp.status_code == 200 else {}
                                task_status = task_info.get("status", "")
                                task_reviewer = task_info.get("reviewer") or ""

                                if task_status in ("done", "archived"):
                                    # Task already completed — no recovery needed.
                                    logger.info("Crashed agent %s task %s already %s — no recovery", agent_name, task_id[:8], task_status)
                                elif (
                                    task_status == "review"
                                    and task_reviewer
                                    and config_store.is_agent_dispatchable(task_reviewer)
                                ):
                                    # Reviewer crashed during review — re-queue to
                                    # reviewer for another attempt. Do NOT move to
                                    # Ready (that would lose the review verdict if
                                    # it was already posted). Bounded by the shared
                                    # infra re-queue cap (HIGH-2) so a reviewer
                                    # that crashes deterministically doesn't
                                    # re-spawn forever.
                                    # Round-2 LOW: gate the "re-queued" log
                                    # on the capped helper's result so it
                                    # never lies when the cap refused (the
                                    # helper logs the cap warning itself).
                                    if await _requeue_review_capped(
                                        task_reviewer, task_id,
                                        task_info.get("readable_id", ""),
                                        "reviewer session crashed "
                                        f"({event.get('reason') or event.get('message') or 'fatal error'})",
                                    ):
                                        logger.info("Reviewer %s crashed on %s — re-queued to reviewer", agent_name, task_id[:8])
                                elif task_status == "review" and task_reviewer:
                                    # ADD-A4 (H1 fix): the crashed reviewer is no
                                    # longer dispatchable (deactivated/deleted/
                                    # stale). Re-queueing to it would starve the
                                    # review (the dispatch loop never visits a
                                    # dead agent). Fall back to the Manager
                                    # Assistant, designating it as reviewer so it
                                    # is authorized to act.
                                    logger.warning(
                                        "Crashed reviewer '%s' on %s is "
                                        "inactive/missing — falling back to MA",
                                        task_reviewer, task_id[:8],
                                    )
                                    # C2: gate the MA re-dispatch on a verified
                                    # reviewer write (a non-200 would otherwise
                                    # re-skip → loop). On failure, leave for the
                                    # sweeper.
                                    from src.backend_client import (
                                        designate_ma_reviewer,
                                    )
                                    if await designate_ma_reviewer(
                                        platform_url, str(office.id), task_id,
                                        security_token,
                                    ):
                                        await queue_manager.add_task("manager-assistant", {
                                            "task_id": task_id,
                                            "readable_id": task_info.get("readable_id", ""),
                                            "reviewer": "manager-assistant",
                                            "status": "review",
                                            "priority": "urgent",
                                        })
                                        if dispatcher is not None:
                                            await dispatcher.dispatch_agent("manager-assistant")
                                    else:
                                        logger.warning(
                                            "Could not designate MA as reviewer "
                                            "for crashed-reviewer task %s — "
                                            "leaving for the sweeper",
                                            task_id[:8],
                                        )
                                else:
                                    # Executor crashed mid-task. Do NOT move the
                                    # task: ``in_progress → ready`` is not a valid
                                    # board transition (the backend rejects it with
                                    # an HTTP-200 ``{"error": ...}`` body, so the
                                    # old "Recovered ... back to ready" log here
                                    # was a lie). Recovery is re-spawn-in-place:
                                    # the dispatcher's reconciler re-adds the
                                    # in_progress orphan to the executor's queue
                                    # (and the watchdog re-queues it explicitly,
                                    # metering crashes — 3 strikes → blocked).
                                    logger.info(
                                        "Executor %s crashed on task %s (status=%s)"
                                        " — leaving in place for re-spawn via the "
                                        "dispatcher reconciler / watchdog",
                                        agent_name, task_id[:8],
                                        task_status or "unknown",
                                    )
                        except Exception as exc:
                            logger.warning("Failed to recover crashed task %s: %s", task_id[:8], exc)

    supervisor = AgentSupervisor(
        workspace_path=office.workspace_path,
        office_id=office.id,
        backend_url=host_backend_url,
        container_name=container_name,
        on_event=_on_agent_event,
    )

    # Wire supervisor back into the manager controller (P2-H setter).
    mgr.set_supervisor(supervisor)

    # 9. Create TaskDispatcher (uses per-agent queues)
    from src.orchestrator.task_dispatcher import TaskDispatcher

    dispatcher = TaskDispatcher(
        redis=redis_client,
        office_id=office.id,
        supervisor=supervisor,
        config_store=config_store,
        queue_manager=queue_manager,
        backend_url=host_backend_url,
        security_token=security_token,
    )

    # 10. Create WebSocket transport
    from src.transport.ws_transport import WsTransport

    router = WsTransport(
        platform_url=platform_url,
        office_id=office.id,
        security_token=security_token,
    )
    logger.info("WebSocket transport created for office %s", office.id)

    # Fire-and-forget the script-execution backfill. Waits briefly
    # for the WS to connect, then publishes every terminal-state
    # status.json found on disk so historical runs surface in the
    # Execution History panel. Idempotent via the backend's
    # (script_id, execution_id) upsert; a re-fire on the same set
    # of files is safe. See ``4a`` block above for context.
    _spawn_background(
        _run_history_backfill(
            workspace_path=_history_backfill_workspace,
            router=router,
            office_id=str(office.id),
        ),
    )

    # Office-memory v1 (T3.6): one-time learnings.md → memory import, per
    # workstream dir — the lazy per-office on-connect migration. The
    # rename to ``learnings.migrated.md`` is the idempotency marker, so a
    # re-fire on every connect is a cheap no-op once migrated, and a
    # failed POST (backend offline, or a pre-memory backend answering
    # 404) leaves the file for the next connect. Fire-and-forget; never
    # blocks bring-up.
    from src.memory_import import run_learnings_import

    _spawn_background(
        run_learnings_import(
            workspace_path=Path(office.workspace_path),
            platform_url=platform_url,
            office_id=str(office.id),
            security_token=security_token,
            config_store=config_store,
        ),
    )

    # 10b. Start tool proxy server (routes Docker container tool calls via WS)
    # Use port 0 to let the OS assign a free port — avoids conflicts when
    # multiple offices each start their own proxy server.
    from src.tool_proxy_server import ToolProxyServer

    tool_proxy = ToolProxyServer(
        ws_client=router.ws_client,
        port=0,
        # Hand the host-side ScriptRunner to the proxy so the
        # in-container MCP can delegate ``execute_script`` for
        # manifests that reference office secrets via
        # ``from_office_secret``. See tool_proxy_server.py module
        # docstring for the security boundary rationale.
        script_runner=script_runner,
    )
    await tool_proxy.start()
    actual_port = tool_proxy.port
    proxy_url = f"http://host.docker.internal:{actual_port}"
    # Per-office tool-proxy URL + bearer token plumbed through the
    # supervisor to spawned worker processes. Do NOT use os.environ
    # here — subsequent offices would overwrite it, cross-wiring all
    # tool calls to a single office's WS. The token is required on
    # every /tool-call and /script-execute-host POST so any other
    # local process on the cbcl host can't trigger office-secret
    # injection via the proxy.
    supervisor.set_tool_proxy(
        proxy_url,
        tool_proxy.token,
        collections_token=tool_proxy.collections_token,
    )
    # Scripts get ONLY the narrow collections token (spec ui-ux-aug19
    # D4.2/D4.3): the host runner injects CUBICLE_TOOL_PROXY_URL +
    # CUBICLE_COLLECTIONS_TOKEN into script subprocess env so the SDK's
    # ``cubicle.collections`` can reach POST /collections/rpc — and
    # nothing else on the proxy.
    script_runner.set_collections_endpoint(
        proxy_url, tool_proxy.collections_token,
    )
    logger.info("Tool proxy server started for office %s on port %d", office.id, actual_port)

    # 10c. Register filesystem handler for backend file operation requests
    from src.fs_handler import FsHandler

    fs_handler = FsHandler(office.workspace_path)

    # 10c-bis. Office-local collections datastore (Flow Studio FS-P1):
    # rows live in ~/.cubicle/data/<office-slug>.sqlite — NEVER in the
    # workspace bind mount and never backend-side. Schemas come from
    # ``sync_config.collections`` via the config_store (read live on
    # every operation, so a schema push applies without re-wiring).
    # The slug is the workspace dir's basename — identical to
    # ``slugify(office.name)`` by construction
    # (``OfficeConfig.workspace_path``), without re-deriving it.
    from src.datastore import OfficeDatastore
    from src.paths import get_datastore_path

    datastore = OfficeDatastore(
        get_datastore_path(Path(office.workspace_path).name), config_store,
    )
    # Wire the datastore into the tool proxy so scripts can reach the
    # collections rows via POST /collections/rpc (spec ui-ux-aug19
    # D4.1; the set_tool_proxy post-construction pattern — the proxy
    # is built before the datastore exists). Until this line runs the
    # route answers 503 "restart cbcl".
    tool_proxy.set_datastore(datastore)

    async def _handle_backend_request(message: dict) -> None:
        """Route requests from the backend (file ops, MCP queries, etc.).

        P3-G: dispatch table lives in ``src._handlers._requests`` —
        this closure just forwards with the captured deps.
        """
        await dispatch_backend_request(
            message,
            router=router,
            fs_handler=fs_handler,
            office=office,
            redis_client=redis_client,
            container_name=container_name,
            supervisor=supervisor,
            datastore=datastore,
        )

    router.on("request", _handle_backend_request)

    # 11. Wire router into the ManagerController (P2-H setter).
    mgr.set_router(router)
    # Wire the same router into the ScriptRunner so every
    # script_status event (spawn-time "running", terminal
    # "completed"/"failed", monitor progress) actually publishes.
    # Pre-fix posture: the manual UI Run path published NOTHING
    # because self._router was None — see ScriptRunner.set_router
    # docstring for the user-visible symptom that triggered this fix.
    script_runner.set_router(router)

    # 11a. Flow Studio daemon block executor (FS-P2.T5): the daemon
    # side of ``flow_block_execute`` / ``flow_block_result`` — ai
    # blocks (one-shot generation CLI, schema-validated + 1 retry),
    # generate blocks (doc.yaml assembly into
    # outputs/<ws_short>/<run_readable>/), action blocks (script /
    # snapshot / notice / webhook / artifacts). See
    # ``src/flow_blocks.py`` for the idempotency + delivery posture.
    from src.flow_blocks import FlowBlockExecutor

    flow_block_executor = FlowBlockExecutor(
        router=router,
        office_id=str(office.id),
        workspace_path=office.workspace_path,
        container_name=container_name,
        script_runner=script_runner,
        datastore=datastore,
        platform_url=platform_url,
        security_token=security_token,
    )
    router.on(
        "flow_block_execute",
        flow_block_executor.handle_flow_block_execute,
    )
    # Reconnect re-fire (spec §6.3 — the verify-reconnect posture,
    # daemon half): results produced while the WS was down ride the
    # ws_client replay queue, but that queue is bounded
    # (CBCL_MAX_QUEUE_SIZE, oldest-drops) — so on (re)connect the
    # executor re-publishes any cached result whose original publish
    # happened while disconnected. The backend also re-sends
    # un-acked ``flow_block_execute`` commands (its sweeper +
    # reconnect seam); the executor dedupes those on its
    # (run_id, block_id) in-flight marker + completed-result cache.
    try:
        router.ws_client.on_reconnect(flow_block_executor.on_reconnect)
    except AttributeError:
        # Test-harness routers without a real PlatformWSClient — the
        # sweeper-driven backend re-fire still covers lost results.
        pass

    # Mutable ref for watchdog access in handlers
    _watchdog_ref: list = []

    # 11b. Per-office container resource-limit reconciler. sync_config
    # carries the desired ``container_cpus``/``container_memory``; the
    # reconciler recreates the office container when they drift from
    # what Docker applied at create — immediately when the office is
    # idle, deferred (re-checked on the health tick) while busy. Only
    # wired when the daemon passed its ContainerManager.
    limits_reconciler = None
    if containers is not None:
        from src.docker.limits_reconciler import ResourceLimitReconciler
        limits_reconciler = ResourceLimitReconciler(
            containers=containers,
            office=office,
            config_store=config_store,
            supervisor=supervisor,
            script_runner=script_runner,
            manager=mgr,
        )

    # 12. Register message handlers on the router
    _register_process_model_handlers(
        router, config_store, script_syncer,
        claude_md_writer, mgr, supervisor, dispatcher,
        script_runner, secrets_store, queue_manager, _watchdog_ref,
        container_name=container_name,
        office=office,
        redis_client=redis_client,
        workspace_setup=workspace_setup,
        platform_url=platform_url,
        security_token=security_token,
        variable_manager=variable_manager,
        create_queue=create_queue,
        delete_queue=delete_queue,
        consult_planner_ref=_consult_planner_ref,
        push_agent_feed_ref=_push_agent_feed_ref,
        limits_reconciler=limits_reconciler,
    )

    # 13. Create HealthReporter
    reporter = HealthReporter(
        redis=redis_client,
        office_id=office.id,
        supervisor=supervisor,
        dispatcher=dispatcher,
        session_manager=session_manager,
        script_runner=script_runner,
        config_store=config_store,
        transport=router,
        limits_reconciler=limits_reconciler,
        datastore=datastore,
    )

    # 14. Create TaskWatchdog (simplified — no review/blocked handling)
    from src.watchdog import TaskWatchdog, HttpBoardClient

    board_client = HttpBoardClient(platform_url, office.id, security_token)

    watchdog = TaskWatchdog(
        ws=board_client,
        executor=None,
        manager=mgr,
        config_store=config_store,
        task_queue=None,
        office_id=office.id,
        supervisor=supervisor,
        dispatcher=dispatcher,
    )
    # T8/1.1+2.1: give the dispatcher a read-only handle to the watchdog's
    # crash state so it honors the respawn cap and doesn't false-arm the
    # deadlock detector against a holder under crash recovery.
    dispatcher.set_watchdog(watchdog)
    _watchdog_ref.append(watchdog)

    return ProcessModelOfficeComponents(
        supervisor=supervisor,
        dispatcher=dispatcher,
        router=router,
        reporter=reporter,
        script_runner=script_runner,
        manager=mgr,
        watchdog=watchdog,
        queue_manager=queue_manager,
        tool_proxy=tool_proxy,
    )


def _register_process_model_handlers(
    router: object,
    config_store: ConfigStore,
    script_syncer: ScriptSyncer,
    claude_md_writer: ClaudeMdWriter,
    mgr: ManagerController,
    supervisor: object,
    dispatcher: object,
    script_runner: ScriptRunner,
    secrets_store: SecretsStore,
    queue_manager: AgentQueueManager,
    watchdog_ref: list | None = None,
    *,
    container_name: str = "",
    office: object = None,
    redis_client: object = None,
    workspace_setup: object = None,
    platform_url: str = "",
    security_token: str = "",
    variable_manager: VariableManager | None = None,
    # Lifecycle queues consumed by the inner ``_handle_office_*``
    # closures. Default ``None`` keeps the test surface (handlers
    # built without queues) green.
    create_queue: "asyncio.Queue[dict] | None" = None,
    delete_queue: "asyncio.Queue[str] | None" = None,
    # Mutable ref populated with ``_handle_consult_planner`` so the
    # verdictless-verify refire in ``init_office_process_model``'s
    # ``_on_agent_event`` scope can re-dispatch a consult locally (the two
    # closures live in different functions). ``None`` keeps the test
    # surface (handlers built without the ref) green.
    consult_planner_ref: list | None = None,
    # Mutable ref carrying ``init_office_process_model``'s office-bound
    # ``_push_agent_feed`` closure (the reverse direction of
    # ``consult_planner_ref``) so the planner heartbeat's feed keepalive
    # can push the "dynamic workflow running" placeholder row. ``None``
    # keeps the test surface green — the keepalive is then a no-op.
    push_agent_feed_ref: list | None = None,
    # Per-office resource-limit reconciler
    # (``src.docker.limits_reconciler.ResourceLimitReconciler``).
    # ``None`` (test surface / no ContainerManager) disables the
    # sync-driven recreate-when-idle path.
    limits_reconciler: object | None = None,
) -> None:
    """Register command handlers on the transport for process model.

    ``platform_url`` and ``security_token`` are captured by the
    ``_handle_task_updated`` / ``_handle_task_moved`` closures so the
    blocked-task triage cooldown check (``task_should_skip_ma_routing``)
    can reach the backend. Without them the closures referenced unbound
    names and raised ``NameError`` on every event, silently disabling
    routing-skip paths #2 (Manager-driven blocked) and #3 (orphan-blocked
    sweep). See ``docs/specs/task-spec.md`` Hard Rule #10.
    """

    async def _handle_sync_config(msg: dict) -> None:
        await config_store.update_from_sync(msg)
        # SEC3-01: capture the per-office /tool-call capability secret so
        # newly-spawned agents can authenticate their direct tool-call POSTs.
        tool_secret = msg.get("config", {}).get("office_tool_secret")
        if tool_secret:
            supervisor.set_office_tool_secret(tool_secret)
        await script_syncer.sync_from_config(msg)
        # T8.3.3 (03/#20): these are synchronous filesystem-bound writes
        # (CLAUDE.md files, per-agent + per-workstream dirs) — run them off the
        # event loop so a slow/contended workspace FS can't stall the daemon
        # loop (every office's WS/heartbeat/dispatch). They touch no loop-affine
        # state.
        cfg = msg.get("config", {})
        await asyncio.to_thread(claude_md_writer.sync_all, cfg)
        if workspace_setup:
            await asyncio.to_thread(
                workspace_setup.sync_agent_workspaces, cfg.get("agents", []),
            )
            await asyncio.to_thread(
                workspace_setup.sync_workstream_outputs,
                cfg.get("workstreams", []),
            )
        # Reconcile the per-office container resource limits against
        # what the running container was created with. Recreates the
        # container when idle; defers (health-tick re-check) while
        # busy. Best-effort — a reconcile failure must never break
        # config sync itself.
        if limits_reconciler is not None:
            try:
                await limits_reconciler.on_sync_config(cfg)
            except Exception:
                logger.exception(
                    "Resource-limit reconcile failed (non-fatal; will "
                    "retry on the next sync_config/health tick)",
                )
        dispatcher.wake()

    async def _handle_task_ready(msg: dict) -> None:
        task_data = msg.get("task_data", msg)
        # Race-proof the per-workstream output dir: a brand-new
        # workstream may not have been pre-created by the most recent
        # sync_config (the backend pushes sync after this task_ready),
        # so create the dir just-in-time. Idempotent.
        if workspace_setup:
            try:
                workspace_setup.ensure_task_output_dir(
                    task_data.get("workstream_short_code", ""),
                    task_data.get("scope_readable_id"),
                )
            except Exception:
                logger.debug(
                    "ensure_task_output_dir failed (non-fatal)",
                    exc_info=True,
                )
        await dispatcher.add_task(task_data)

    async def _handle_task_rework(msg: dict) -> None:
        task_data = {
            "task_id": msg.get("task_id", ""),
            "readable_id": msg.get("readable_id", ""),
            "title": msg.get("title", ""),
            "assigned_agent": msg.get("assigned_agent", ""),
            "reviewer": msg.get("reviewer", ""),
            "priority": msg.get("priority", "medium"),
            "brief": msg.get("brief", {}),
            "rework_feedback": msg.get("feedback", ""),
            "rework_count": msg.get("rework_count", 0),
            # So the worker prompt's "previously BLOCKED" note + the Recent
            # Activity context fire on the rework path too (parity with the
            # initial dispatch).
            "blocked_bounce_count": msg.get("blocked_bounce_count", 0),
            "recent_activities": msg.get("recent_activities", []),
            "workstream_name": msg.get("workstream_name", ""),
            "workstream_short_code": msg.get("workstream_short_code", ""),
            # CTX-01 (rework half): the backend's send_task_rework ships the
            # SAME pre-built workstream context as send_task_ready
            # (workstream_id + workstream_context + workstream_has_spec) so the
            # worker prompt renders the workstream header, the CLAUDE.md
            # conventions pointer, and the spec-read step on REWORK dispatches
            # too. This allowlist used to drop all three — a reworked task ran
            # blind to its workstream even after the ready-path fix (the exact
            # CTX-01 failure mode, on the second attempt where the worker needs
            # the conventions MOST because it just failed review).
            "workstream_id": msg.get("workstream_id", ""),
            "workstream_context": msg.get("workstream_context"),
            "workstream_has_spec": msg.get("workstream_has_spec", False),
            # Carry scope context through the rework path so the
            # worker's per-task CUBICLE_OUTPUT_DIR stays consistent
            # across review cycles. Without this, a scoped task
            # collapses from /workspace/outputs/{ws}/{scope}/ to
            # /workspace/outputs/{ws}/ on its second attempt and
            # files split across two directories within one cycle.
            "scope_id": msg.get("scope_id"),
            "scope_readable_id": msg.get("scope_readable_id"),
            "status": "ready",
            # Preserve session across rework cycles to keep context continuity
            "prior_session_id": msg.get("prior_session_id", ""),
        }
        if workspace_setup:
            try:
                workspace_setup.ensure_task_output_dir(
                    task_data.get("workstream_short_code", ""),
                    task_data.get("scope_readable_id"),
                )
            except Exception:
                logger.debug(
                    "ensure_task_output_dir failed (non-fatal, rework)",
                    exc_info=True,
                )
        await dispatcher.add_task(task_data)

    async def _handle_task_updated(msg: dict) -> None:
        """React to task updates (P3-G: routing in ``_handlers._tasks``)."""
        await route_task_updated(
            msg,
            queue_manager=queue_manager,
            dispatcher=dispatcher,
            supervisor=supervisor,
            router=router,
            platform_url=platform_url,
            office_id=str(office.id),
            security_token=security_token,
            config_store=config_store,
        )

    async def _handle_task_moved(msg: dict) -> None:
        """React to task status changes (P3-G: routing in ``_handlers._tasks``)."""
        await route_task_moved(
            msg,
            queue_manager=queue_manager,
            dispatcher=dispatcher,
            supervisor=supervisor,
            router=router,
            platform_url=platform_url,
            office_id=str(office.id),
            security_token=security_token,
            config_store=config_store,
        )

    async def _handle_task_kill(msg: dict) -> None:
        task_id = msg.get("task_id", "")
        agent_name = msg.get("agent_name", "")
        if agent_name:
            try:
                await supervisor._kill_process(agent_name)
            except Exception as exc:
                logger.warning("Failed to kill agent '%s': %s", agent_name, exc)
            # Clear active hash and dispatch next task for this agent
            await queue_manager.clear_active(agent_name)
            # ADD-A3: scope the queue removal to the KILLED agent only.
            # The previous ``remove_task_from_all(task_id)`` wiped the task
            # from EVERY queue — including a reviewer's queue that
            # ``route_task_moved`` may have JUST populated for this same task
            # on a review submission (the backend sends ``task_moved`` then
            # ``task_kill``). That race yanked the review out of the
            # reviewer's queue, stalling it until the ~60s reconciler re-added
            # it. Removing only from the killed agent's queue stops the
            # executor without clobbering the freshly-routed reviewer entry.
            await queue_manager.remove_task(agent_name, task_id)
        else:
            # No agent specified (rare / legacy) — fall back to the broad
            # sweep so a stray task still gets cleaned up.
            await queue_manager.remove_task_from_all(task_id)
        # Wake dispatcher so freed agent picks up next task
        dispatcher.wake()

    # -- MCP control handlers (P3-G: bodies in ``_handlers._mcp``) --
    async def _handle_mcp_add(msg: dict) -> None:
        await run_mcp_add(
            msg,
            container_name=container_name,
            refresh_mcp_list=_refresh_mcp_list,
            router=router,
        )

    async def _handle_mcp_remove(msg: dict) -> None:
        await run_mcp_remove(
            msg,
            container_name=container_name,
            refresh_mcp_list=_refresh_mcp_list,
        )

    # P3-G: refresh + parse helpers live in ``src._handlers._mcp_listing``.
    # ``_mcp_refresh_state`` is a small dataclass tracking the last-refresh
    # timestamp for the 5-s debounce; callers pass ``force=True`` to bypass it.
    _mcp_refresh_state = MCPRefreshState()

    async def _refresh_mcp_list(*, force: bool = False) -> None:
        await refresh_mcp_list(
            state=_mcp_refresh_state,
            container_name=container_name,
            redis_client=redis_client,
            router=router,
            office_id=str(office.id),
            force=force,
        )

    # Initial MCP list cache on startup. ``_spawn_background`` is
    # loop-aware: if no event loop is running (test harnesses that
    # build a router without one), the call is a no-op and the
    # first user-triggered refresh still warms the cache.
    _spawn_background(_refresh_mcp_list())

    async def _handle_consult_planner(msg: dict) -> None:
        """Spawn a one-shot Planner session for a Manager consult
        (execution_improvements_v1 Phase 3). The Planner runs as a worker
        process named 'planner' with a synthetic task carrying the consult
        marker; on completion it pokes the Manager (see the task_complete
        routing in ``_on_agent_event``). Fire-and-forget."""
        import uuid as _uuid

        # Default mirrors planner_prompt's default consult mode (pivot-1 T6:
        # ``roadmap`` retired — the backend refuses new roadmap consults).
        mode = (msg.get("mode") or "specify").strip()
        objective = (msg.get("objective") or "").strip()
        workstream_id = msg.get("workstream_id") or ""
        scope_id = msg.get("scope_id") or ""
        # verify-consult enrichment (backend-fired): the approved spec's REQ
        # list + the REQ ids THIS scope is responsible for, so the Planner has
        # the coverage contract at session start instead of behind tool calls.
        approved_spec_reqs = msg.get("approved_spec_reqs") or []
        scope_covers = msg.get("scope_covers") or []

        # Consult marker reused for both the spawn and any failure poke.
        consult_marker = {
            "mode": mode,
            "objective": objective,
            "workstream_id": workstream_id,
            "scope_id": scope_id,
            "approved_spec_reqs": approved_spec_reqs,
            "scope_covers": scope_covers,
        }
        if msg.get("_verdictless_refire"):
            # This consult IS the one-shot verdictless-verify re-fire. The
            # flag rides the marker into the worker's task_complete event so
            # the post-verify honesty check never re-fires a second time
            # (loop guard — see ``_refire_verdictless_verify``).
            consult_marker["_verdictless_refire"] = True
        if msg.get("_infra_refire"):
            # FIX P2/P3: this consult IS the one-shot infra / missing-
            # outcome re-fire. Same loop-guard posture as above — a second
            # death or missing outcome falls through to the honest Manager
            # failure poke instead of another re-fire.
            consult_marker["_infra_refire"] = True
        if mode == "verify":
            # AREA-2 (verify turn-end incident 2026-07-17): cumulative
            # elapsed bookkeeping. The FIRST attempt stamps its start
            # time; a daemon refire threads it through (plus the attempt
            # ordinal) so the heartbeat's elapsed copy and the 15m/30m
            # long-verify notices report honest scope-level wall-clock
            # ("~45m across 3 attempts") instead of resetting to zero
            # per refired attempt. Backend/sweeper-fired verifies carry
            # neither key and start a fresh clock — the daemon can only
            # thread what it re-fires itself.
            try:
                _first_started = float(
                    msg.get("_verify_first_started") or 0.0
                )
            except (TypeError, ValueError):
                _first_started = 0.0
            consult_marker["_verify_first_started"] = (
                _first_started or time.monotonic()
            )
            try:
                consult_marker["_verify_attempt"] = int(
                    msg.get("_verify_attempt") or 1
                )
            except (TypeError, ValueError):
                consult_marker["_verify_attempt"] = 1

        # AREA-2 single-flight verify per scope (verify turn-end incident
        # 2026-07-17): at most ONE verify may run/re-fire per scope at
        # the consult layer. The supervisor's busy-refuse already blocks
        # two concurrent Planner PROCESSES, but a backend/sweeper-fired
        # verify landing inside a daemon refire's idle-wait window (old
        # stash popped, Planner briefly idle) would spawn a back-to-back
        # double run — and a second heartbeat. Drop it silently (verify
        # posture — the sweeper re-fires on its own cadence); the refire
        # itself carries its marker flag and is exempt from its own
        # pending guard.
        if (
            mode == "verify"
            and scope_id
            and not (
                msg.get("_verdictless_refire") or msg.get("_infra_refire")
            )
        ):
            _live_verify = any(
                (c.get("mode") or "") == "verify"
                and str(c.get("scope_id") or "") == str(scope_id)
                for c in _planner_consults.values()
            )
            if _live_verify or str(scope_id) in _verify_refire_pending:
                logger.info(
                    "consult_planner(verify): a verify for scope %s is "
                    "already running / re-firing — dropping the "
                    "duplicate (single-flight; the stuck-verifying "
                    "sweeper remains the backstop)",
                    scope_id,
                )
                return

        async def _poke_failure(reason: str) -> None:
            """Tell the Manager the consult could NOT run (it was told
            'engaged' synchronously — without this it waits forever).

            EXCEPT for ``mode=verify``: that consult is fired by the BACKEND
            (scope auto-enters `verifying` → `_trigger_planner_verify`), NOT by
            a Manager turn. Poking the Manager about a verify it never issued is
            misleading ("re-consult your verify"), and the stuck-`verifying`
            sweeper re-fires every cycle — so each drop would spam a fresh
            Manager turn. The sweeper owns verify re-dispatch + the eventual
            user escalation; stay silent here (just log)."""
            if mode == "verify":
                logger.info(
                    "consult_planner(verify) dropped (%s) — backend-fired; the "
                    "stuck-verifying sweeper will re-fire/escalate, not poking "
                    "the Manager", reason,
                )
                return
            try:
                await mgr.ingest_planner_result(
                    {"planner_consult": consult_marker, "planner_error": reason}
                )
            except Exception:
                logger.exception("consult_planner failure poke failed")

        if supervisor is None:
            logger.warning("consult_planner: supervisor not ready — dropping")
            await _poke_failure("the office orchestrator was not ready")
            return
        if supervisor.is_agent_busy("planner"):
            logger.info(
                "consult_planner: planner already busy — not started "
                "(Manager will be told to re-consult once it's free)"
            )
            await _poke_failure(
                "the Planner is already running another consult — only one "
                "runs at a time; re-consult after the current one reports back"
            )
            return
        # Respawn-after-cap guard (incident 2026-06-23): if this exact consult
        # (workstream, scope, mode) recently hit the stall auto-restart cap,
        # REFUSE a fresh spawn during the cooldown. The cap poke previously
        # told the Manager to "re-consult", which spawned a new Planner with
        # the restart counter reset → the whole stall cycle restarted (the
        # observed 07:08/08:07/08:40 respawns). Tell the Manager it's wedged
        # and to investigate/split/escalate instead of looping.
        import os as _os_cd
        import time as _time_cd

        try:
            _cd_secs = float(
                _os_cd.environ.get(
                    "CUBICLE_PLANNER_CAP_COOLDOWN_SECONDS", "1800"
                )
            )
        except (TypeError, ValueError):
            _cd_secs = 1800.0
        # D1-F2 (incident 2026-06-23 audit): opportunistically evict elapsed
        # cooldown entries so a capped (ws,scope,mode) that is never
        # re-consulted (the Manager splits into a different scope) doesn't leak
        # a stale float until daemon restart. Bounded, cheap, runs per consult.
        _cd_now = _time_cd.monotonic()
        for _stale in [
            _k
            for _k, _t in _planner_cap_cooldown.items()
            if _cd_now - _t >= _cd_secs
        ]:
            _planner_cap_cooldown.pop(_stale, None)

        _cd_key = _cap_cooldown_key(consult_marker)
        _cd_at = _planner_cap_cooldown.get(_cd_key)
        if _cd_at is not None:
            if _time_cd.monotonic() - _cd_at < _cd_secs:
                logger.warning(
                    "consult_planner: %s is in post-cap cooldown — refusing "
                    "re-consult to break the respawn loop",
                    _cd_key,
                )
                await _poke_failure(
                    "the Planner already STALLED repeatedly on this exact "
                    "consult and hit the retry cap moments ago — a cooldown is "
                    "active, so re-consulting now would just restart the same "
                    "stall loop. Do NOT immediately re-consult: the objective "
                    "is likely too large for one session (consider splitting "
                    "the scope into smaller pieces), or something is wedged. "
                    "Investigate, or ask the user how to proceed."
                )
                return
            # Cooldown elapsed — drop the stale marker and allow the spawn.
            _planner_cap_cooldown.pop(_cd_key, None)
        agent_config = config_store.get_agent("planner")
        if not agent_config:
            logger.warning(
                "consult_planner: 'planner' agent not in config — cannot "
                "spawn. Save any agent in the UI or restart cbcl to resync."
            )
            await _poke_failure(
                "the Planner agent is not configured for this office "
                "(restart cbcl to resync)"
            )
            return

        # Workstream context so the planner prompt's header renders the
        # workstream name/goals/description (else it only sees the bare UUID).
        ws = config_store.get_workstream(workstream_id) or {}
        ws_ctx = {
            "name": ws.get("name", ""),
            "goals": ws.get("goals", ""),
            "description": ws.get("description", ""),
        }

        # FIX P3: snapshot the outcome target's pre-consult revision on the
        # marker so the post-consult gate can test "advanced" cheaply
        # (revision grew / updated_at changed) instead of existence alone.
        # Best-effort — a failed snapshot leaves the gate existence-shaped
        # (fail-open direction). Materialize is existence-shaped by design
        # (≥1 complete-brief task), so no snapshot is needed there.
        if mode in _OUTCOME_GATED_MODES and mode != "materialize":
            consult_marker["_pre_outcome"] = (
                await _fetch_consult_outcome_state(
                    consult_marker, mode,
                    platform_url=platform_url,
                    office_id=str(getattr(office, "id", "") or ""),
                    security_token=security_token,
                )
            )

        synthetic_id = f"planner-{_uuid.uuid4().hex[:12]}"
        task_data = {
            "task_id": synthetic_id,
            "readable_id": "PLAN",
            "title": f"Planning consult ({mode})",
            "status": "planning",
            "priority": "high",
            "brief": {},
            "workstream_context": ws_ctx,
            # The marker dict verbatim (incl. any ``_verdictless_refire``
            # flag) — the worker echoes it on its task_complete event, which
            # is what the honesty check's loop guard reads.
            "planner_consult": dict(consult_marker),
        }
        spawned = await supervisor.spawn_worker(
            "planner", agent_config, task_data
        )
        if spawned:
            # Round-2 LOW: stash the consult marker so a supervisor-
            # synthesized fatal (heartbeat kill — no marker on the
            # event) can still recover mode/context_key in the planner
            # error branch of ``_on_agent_event``. Popped there on
            # every planner exit path.
            _planner_consults[synthetic_id] = dict(consult_marker)
        if not spawned:
            logger.warning(
                "consult_planner: failed to spawn Planner session "
                "(mode=%s ws=%s)", mode, workstream_id,
            )
            await _poke_failure(
                "the Planner session failed to start (the office may be at its "
                "agent limit) — re-consult shortly"
            )
            return

        # VISIBILITY (user report 2026-06-04): a Planner consult is async and
        # can run for MINUTES (a `materialize` of a 10-task scope took ~6 min),
        # during which the Manager is idle and the chat is silent — only the
        # "engaged" bubble, then nothing until the result poke. Users read that
        # as "the Planner stopped working" and nudge the Manager. Pulse a
        # "Planner working" status to the workstream while it runs so the UI
        # shows it's alive. Stops as soon as the Planner is no longer busy
        # (consult finished → the result/failure poke runs a Manager turn that
        # sets its own state, overwriting this).
        _verb = {
            "specify": "drafting the workstream spec",
            "scope_plan": "planning the scope",
            "materialize": "authoring the scope's tasks",
            "research": "researching",
            "verify": "verifying the completed scope",
        }.get(mode, mode)

        async def _planner_heartbeat() -> None:
            """Per-consult heartbeat + STALL watchdog.

            While the Planner runs, pulse a status so the UI shows it's alive.
            The Claude CLI has NO built-in hang timeout, so a consult that
            produces nothing can wedge indefinitely (the reported 30-min
            stall). If a consult has not completed after
            ``CUBICLE_PLANNER_STALL_SECONDS`` (default 600s = 10 min) it is
            treated as STALLED and AUTO-RESTARTED: the hung session is killed
            and the SAME consult is re-fired (specify / materialize /
            scope_plan / verify authoring is overwrite-safe — it converges, it
            doesn't duplicate). Capped at ``CUBICLE_PLANNER_MAX_RESTARTS``
            (default 2); after the cap the Manager is poked to re-consult or
            escalate so the work never stalls silently forever.

            LIFECYCLE (AREA-2, verify turn-end incident 2026-07-17): the
            loop is CONSULT-owned, not agent-owned. Exit/intervention
            checks require BOTH ``is_agent_busy("planner")`` AND this
            consult's own ``_planner_consults`` entry to still be live
            (``_consult_live``) — the agent-shaped check alone re-latched
            a stale heartbeat onto the NEXT consult whenever a refire
            respawned the Planner inside the 75s sleep window (the
            interleaved "49m"/"4m" double counter). Belt-and-suspenders,
            the task handle is stored in ``_planner_heartbeats`` and
            cancelled from every consult exit pop
            (``_cancel_planner_heartbeat``); the ``finally`` below
            self-prunes the handle on any exit.
            """
            import os as _os
            import time as _time

            ctx = (
                f"workstream:{workstream_id}"
                if workstream_id
                else "general_chat"
            )
            # Ultracode-aware stall ceiling (incident 2026-06-23). The Planner
            # ships effort="ultracode", so it orchestrates SILENT background
            # dynamic-workflow sub-agents that legitimately run for many
            # minutes producing NO top-level output. The supervisor heartbeat
            # (PING/PONG, 90s) already proves the session is alive, so this
            # wall-clock watchdog must NOT kill a healthy long ultracode
            # consult at the plain 600s mark — that false-positive kill drove
            # the whole stall → respawn-loop → mislabeled-double-poke cascade.
            # Use a much larger ceiling for ultracode; the cap + cooldown still
            # bound a genuinely-wedged session.
            #
            # Note: VERIFY consults run at PLAIN xhigh by default
            # (2026-07-21 inversion — specify/roadmap/verify are plain
            # unless CBCL_CONSULT_ULTRACODE=1 opts them back into the
            # configured ultracode; the CBCL_VERIFY_FORCE_PLAIN_EFFORT
            # escape hatch in
            # ``_session_policy.agent_config_for_assignment`` still
            # forces plain xhigh either way). Regardless, the ceiling
            # selected here is moot for them: mode=="verify" is already
            # exempt from stall kills below (recovery is owned by the
            # backend stuck-verifying sweeper + the verdictless-exit
            # honesty check).
            _is_ultracode = (
                str((agent_config or {}).get("effort") or "").strip().lower()
                == "ultracode"
            )
            _stall_env = (
                "CUBICLE_PLANNER_STALL_SECONDS_ULTRACODE"
                if _is_ultracode
                else "CUBICLE_PLANNER_STALL_SECONDS"
            )
            _stall_default = "2400" if _is_ultracode else "600"
            try:
                stall_after = float(
                    _os.environ.get(_stall_env, _stall_default)
                )
            except (TypeError, ValueError):
                stall_after = 2400.0 if _is_ultracode else 600.0
            try:
                max_restarts = int(
                    _os.environ.get("CUBICLE_PLANNER_MAX_RESTARTS", "2")
                )
            except (TypeError, ValueError):
                max_restarts = 2
            restart_count = int(msg.get("_restart_count") or 0)
            started = _time.monotonic()
            # AREA-2 cumulative elapsed: for verify, elapsed COPY runs
            # from the FIRST attempt's start (threaded through refires on
            # the marker); ``started`` (this attempt) keeps driving the
            # stall logic. Non-verify markers carry neither key, so
            # ``first_started == started`` there.
            try:
                first_started = float(
                    consult_marker.get("_verify_first_started") or started
                )
            except (TypeError, ValueError):
                first_started = started
            try:
                verify_attempt = int(
                    consult_marker.get("_verify_attempt") or 1
                )
            except (TypeError, ValueError):
                verify_attempt = 1

            def _consult_live() -> bool:
                """Consult-shaped liveness (AREA-2): THIS consult still
                runs. The stash entry is popped on every completion path
                before the supervisor's idle flip, so a stale heartbeat
                can never mistake a refired consult's busy flag for its
                own — and the stall branches can never kill a SUCCESSOR
                consult on a stale timer."""
                return (
                    supervisor.is_agent_busy("planner")
                    and synthetic_id in _planner_consults
                )

            try:
                while True:
                    await asyncio.sleep(75)
                    if not _consult_live():
                        break  # consult finished (or failed) — normal exit
                    elapsed = _time.monotonic() - started
                    # Idle-based stall detection (incident 2026-08-04):
                    # ``elapsed`` alone killed HEALTHY consults at exactly
                    # ``stall_after`` wall-clock (the observed 40:00 kill
                    # of a streaming S03 scope_plan). Measure SILENCE
                    # instead — ``_on_agent_event`` stamps
                    # ``_last_activity_monotonic`` on the consult's stash
                    # entry for every Planner progress frame, so a consult
                    # only reads as stalled after ``stall_after`` seconds
                    # with NO output at all. Ultracode's silent workflow
                    # phases keep the same generous ceiling they had;
                    # a genuinely wedged session still dies on schedule.
                    _act_stash = _planner_consults.get(synthetic_id)
                    try:
                        _last_act = float(
                            (_act_stash or {}).get(
                                "_last_activity_monotonic"
                            ) or started
                        )
                    except (TypeError, ValueError):
                        _last_act = started
                    idle = _time.monotonic() - max(started, _last_act)
                    # Under the stall threshold — OR a VERIFY consult, whose
                    # recovery is owned by the backend stuck-verifying sweeper
                    # (+ reconnect re-fire); the watchdog only auto-restarts the
                    # Manager-initiated modes that have no backend backstop. In
                    # both cases just pulse "still working".
                    if idle < stall_after or mode == "verify":
                        # AREA-2: the DISPLAYED elapsed is cumulative
                        # from the first verify attempt (== this
                        # attempt's elapsed for non-verify and for a
                        # first attempt) so refires don't reset the
                        # user-visible counter.
                        cumulative = _time.monotonic() - first_started
                        mins = max(1, round(cumulative / 60))
                        # STILL-RUNNING CHAT NOTICE (incident 2026-07-16
                        # follow-up; generalized to EVERY consult mode by
                        # owner directive 2026-08-04): a healthy consult
                        # can legitimately run 15-30+ minutes while the
                        # TRANSCRIPT stays silent. At 15m and again at
                        # 30m post ONE durable ``role='system'`` chat
                        # bubble into the consult's Manager context —
                        # strictly once per threshold per consult
                        # (sent-flags on the ``_planner_consults``
                        # stash; a consult that finishes sooner never
                        # crosses a threshold because this loop exits —
                        # and is cancelled — with its consult). The claim
                        # runs on CUMULATIVE elapsed, so a refired
                        # attempt's fresh marker re-fires only the
                        # highest due threshold with honest total copy.
                        # Progress notice ONLY — the verify-silence
                        # posture for failures is untouched. Isolated
                        # try: a notice failure must never kill the
                        # heartbeat (the NameError lesson).
                        _notice_text: str | None = None
                        _notice_marker = _planner_consults.get(
                            synthetic_id
                        )
                        if _notice_marker is not None:
                            _due = claim_due_verify_notice(
                                cumulative, _notice_marker
                            )
                            if _due is not None:
                                _notice_text = build_long_verify_notice(
                                    _due,
                                    elapsed_minutes=mins,
                                    attempts=(
                                        verify_attempt
                                        if mode == "verify"
                                        else 1
                                    ),
                                    mode=mode,
                                )
                        await mgr._publish_manager_state(
                            ctx, "working",
                            _notice_text
                            or f"🗺️ Planner {_verb} — {mins}m elapsed…",
                        )
                        if _notice_text is not None:
                            try:
                                from src.backend_client import (
                                    post_system_chat_notice,
                                )
                                await post_system_chat_notice(
                                    platform_url,
                                    str(getattr(office, "id", "") or ""),
                                    ctx,
                                    _notice_text,
                                    security_token,
                                    action_payload={
                                        # Reuse the whitelisted inline
                                        # system-row kind (see
                                        # ``isInlineSystemRow``) so the
                                        # transcript renders the row; the
                                        # ``notice`` field marks it as a
                                        # progress notice, not a consult
                                        # start.
                                        "kind": "planner_consulted",
                                        # Verify keeps its historical
                                        # value; other modes label the
                                        # generalized progress notice.
                                        "notice": (
                                            "verify_progress"
                                            if mode == "verify"
                                            else "consult_progress"
                                        ),
                                        "mode": mode,
                                        "scope_id": scope_id or None,
                                    },
                                )
                            except Exception:
                                logger.debug(
                                    "long-verify notice failed "
                                    "(non-fatal)", exc_info=True,
                                )
                        # Feed keepalive (incident 2026-07-16): an
                        # ultracode dynamic-workflow phase legitimately
                        # emits NO parent-stream frames for many minutes,
                        # so with zero pushes the Planner's Redis feed
                        # LIST would expire and the sidebar would blank
                        # mid-consult. Push a placeholder row on every
                        # pulse — it refreshes the sliding TTL AND shows
                        # the user the workflow is alive instead of
                        # emptiness. push_agent_feed swallows Redis
                        # failures, so this can't break the heartbeat.
                        # The push helper is a closure in
                        # ``init_office_process_model``'s scope, reached
                        # via the mutable ref (an unbound name here
                        # NameError'd inside this loop's swallow-all
                        # except and silently killed the heartbeat —
                        # 2026-07-17 fix).
                        _push_agent_feed = (
                            push_agent_feed_ref[0]
                            if push_agent_feed_ref
                            else None
                        )
                        if _push_agent_feed is not None:
                            await _push_agent_feed("planner", {
                                "type": "progress",
                                "event_type": "checkpoint",
                                "content": (
                                    f"Planner {_verb} — {mins}m elapsed "
                                    "(dynamic workflow running)"
                                ),
                                # AREA-2 leak diagnosability: stamp the
                                # owning consult's id on every keepalive
                                # row — TWO ids interleaving in the feed
                                # = a leaked heartbeat; one id = healthy.
                                "details": {"consult_id": synthetic_id},
                            })
                        continue

                    # ── STALL detected (non-verify consult) ─────────────
                    # Re-confirm THIS consult is STILL live right before we
                    # intervene. A consult that finished at the boundary (its
                    # task_complete event still propagating to IDLE) must not be
                    # falsely killed/restarted — and a stale timer must NEVER
                    # kill a SUCCESSOR consult (consult-shaped check). Closes
                    # the boundary race for BOTH the cap and the auto-restart
                    # paths below.
                    if not _consult_live():
                        break
                    # SELF-DEREGISTER before killing (incident 2026-08-04):
                    # the kill below makes the worker emit its cancelled
                    # ``task_complete``, whose ``_on_agent_event`` pop calls
                    # ``_cancel_planner_heartbeat(task_id)`` — which would
                    # CANCEL THIS VERY TASK mid-intervention (the AREA-2
                    # pop-cancel is deterministic on the graceful-SIGTERM
                    # path: the worker always flushes task_complete before
                    # exiting). That cancellation landed inside
                    # ``_kill_process``'s ``process.wait()`` await and
                    # silently lost the auto-restart refire / the cap's
                    # give-up poke — the consult died with NOTHING telling
                    # the Manager (the observed S03 wedge: kill at +40:00,
                    # no respawn, no poke, workstream dead). Popping our own
                    # handle first makes that cancel a no-op; the
                    # ``finally`` below still self-prunes on every exit.
                    _planner_heartbeats.pop(synthetic_id, None)
                    if restart_count >= max_restarts:
                        logger.warning(
                            "planner consult STALLED (mode=%s, %.0fs idle "
                            "%.0fs, restart cap %d reached) — killing + "
                            "escalating to Manager",
                            mode, elapsed, idle, max_restarts,
                        )
                        # Flag the consult so the worker's CancelledError
                        # task_complete (and any SIGKILL-synthesized fatal) is
                        # SUPPRESSED in _on_agent_event — the give-up poke
                        # below is the SINGLE authoritative message.
                        _wk_cap = _planner_consults.get(synthetic_id)
                        if _wk_cap is not None:
                            _wk_cap["_watchdog_killed"] = "cap"
                        # Open a cooldown so the Manager can't immediately
                        # re-consult this exact (ws, scope, mode) and restart
                        # the whole stall loop (the respawn-after-cap bug).
                        _planner_cap_cooldown[
                            _cap_cooldown_key(consult_marker)
                        ] = _time.monotonic()
                        try:
                            await supervisor._kill_process("planner")
                        except Exception:
                            logger.debug("planner kill failed", exc_info=True)
                        # Visible give-up poke (runs a Manager turn) so the
                        # work doesn't stall silently after the cap. Tagged
                        # planner_stall_cap so the Manager-facing body tells it
                        # NOT to immediately re-consult (a cooldown is active).
                        try:
                            await mgr.ingest_planner_result({
                                "planner_consult": consult_marker,
                                "planner_stall_cap": True,
                                "planner_error": (
                                    f"stalled with no result after "
                                    f"~{int(elapsed / 60)} min across "
                                    f"{restart_count + 1} attempts "
                                    "(auto-restart cap reached)"
                                ),
                            })
                        except Exception:
                            logger.debug(
                                "planner give-up poke failed", exc_info=True
                            )
                        break

                    # AUTO-RESTART (capped): kill the hung session + re-fire.
                    restart_count += 1
                    logger.warning(
                        "planner consult STALLED (mode=%s, %.0fs, idle "
                        "%.0fs) — auto-restart %d/%d",
                        mode, elapsed, idle, restart_count, max_restarts,
                    )
                    await mgr._publish_manager_state(
                        ctx, "working",
                        f"🗺️ Planner {_verb} — stalled, auto-restarting "
                        f"(attempt {restart_count + 1})…",
                    )
                    # Race guard (incident 2026-06-23 audit, D1 miss): the
                    # publish above AWAITS (yields the loop). The Planner may
                    # finish LEGITIMATELY in that window — its task_complete
                    # branch fires the success poke and marks the agent idle.
                    # If we then kill + re-fire we'd spawn a DUPLICATE consult
                    # (a redundant run + a second "Planner finished" bubble).
                    # Re-check before intervening (consult-shaped — AREA-2).
                    if not _consult_live():
                        break
                    # Flag the consult so the worker's CancelledError
                    # task_complete is SUPPRESSED in _on_agent_event: an
                    # auto-restart re-fires the SAME consult silently, so it
                    # must NOT surface a "Task was cancelled." poke.
                    # (Correction, incident 2026-06-23: the previous comment
                    # here claimed _kill_process's kill_initiated made
                    # _monitor_exit suppress the crash event so "no failure
                    # poke fires" — FALSE. kill_initiated only gates the
                    # supervisor-SYNTHESIZED error event; it does NOT touch the
                    # worker subprocess's own task_complete, which leaked a
                    # spurious mislabeled poke on EVERY restart. This flag is
                    # what actually suppresses it.)
                    _wk_ar = _planner_consults.get(synthetic_id)
                    if _wk_ar is not None:
                        _wk_ar["_watchdog_killed"] = "auto_restart"
                    try:
                        await supervisor._kill_process("planner")
                    except Exception:
                        logger.debug("planner kill failed", exc_info=True)
                    refire = dict(msg)
                    refire["_restart_count"] = restart_count
                    try:
                        # Re-fire the SAME consult — spawns a fresh Planner +
                        # a fresh watchdog; this one's job is done.
                        await _handle_consult_planner(refire)
                    except Exception:
                        logger.exception(
                            "planner auto-restart re-fire failed (mode=%s)", mode
                        )
                        # Honest escalation (incident 2026-08-04): the
                        # killed consult's own task_complete was suppressed
                        # (``_watchdog_killed``), so if the refire dies the
                        # Manager would wait on "engaged" forever. Tell it.
                        try:
                            await mgr.ingest_planner_result({
                                "planner_consult": consult_marker,
                                "planner_error": (
                                    "the consult stalled and its automatic "
                                    "restart failed to start — re-consult "
                                    "when ready"
                                ),
                            })
                        except Exception:
                            logger.exception(
                                "planner restart-failure poke failed"
                            )
                    break
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("planner heartbeat ended", exc_info=True)
            finally:
                # AREA-2 self-prune: drop our own handle on ANY exit
                # (consult-shaped break, cancel, crash). Keys are
                # uuid-unique per consult, so this can never evict a
                # successor's handle.
                _planner_heartbeats.pop(synthetic_id, None)

        # T8.1.6: strong-reference via _spawn_background (not bare
        # create_task) so the GC can't collect this fire-and-forget task
        # mid-flight — every other spawn in this module already uses it.
        # AREA-2: the handle is ALSO kept in ``_planner_heartbeats`` (keyed
        # by this consult's synthetic id) so the consult exit pops in
        # ``_on_agent_event`` can cancel it the moment the consult ends —
        # a refire respawning the Planner inside the 75s sleep window must
        # never inherit a stale elapsed counter.
        _heartbeat_task = _spawn_background(
            _planner_heartbeat(), name="planner-heartbeat"
        )
        if _heartbeat_task is not None:
            _planner_heartbeats[synthetic_id] = _heartbeat_task

    # ── Flow Studio consults (FS-P3.T4/T5) ────────────────────────────
    # The daemon half of the async design/curate request-poll pattern
    # (spec §8.4): the backend POSTs seed a Redis status row and send
    # ``consult_flow_architect`` / ``consult_data_curator``; the daemon
    # spawns a one-shot consult session (the consult_planner spawn
    # machinery) and reports EXCLUSIVELY via ``flow_consult_progress`` /
    # ``flow_consult_complete`` / ``flow_consult_failed`` events keyed by
    # ``request_id`` — never a Manager chat poke (the exchange renders in
    # the Studio's design-log rail). Every refusal path publishes the
    # honest failure event so the REST poll can never hang on a consult
    # that never started.

    _FLOW_CONSULT_AGENTS = {
        "consult_flow_architect": ("flow-architect", "flow_design",
                                   "Flow Architect"),
        "consult_data_curator": ("data-curator", "collections_curate",
                                 "Data Curator"),
    }

    # V2-P2: the spawn-path half of the live agent_channel overlay —
    # ``state: working`` when a consult session starts, ``state: failed``
    # on every pre-spawn refusal (so the FE typing indicator drops the
    # instant a consult is refused, without waiting for the poll).
    # Separate emitter instance from the ``_on_agent_event`` one (they
    # live in different closures); harmless — state frames carry no
    # coalescing state.
    _flow_channel = AgentChannelEmitter(router.publish_event)

    async def _run_flow_consult(command_type: str, msg: dict) -> None:
        import uuid as _uuid

        agent_name, kind, display = _FLOW_CONSULT_AGENTS[command_type]
        request_id = str(msg.get("request_id") or "").strip()

        async def _fail(reason: str) -> None:
            """Publish the honest failure so the poll flips to
            ``failed`` instead of hanging until the TTL expires."""
            if not request_id:
                return
            fail_event = {
                "type": "flow_consult_failed",
                "request_id": request_id,
                "error": reason,
            }
            if str(msg.get("flow_id") or ""):
                fail_event["flow_id"] = str(msg.get("flow_id"))
            try:
                await router.publish_event(fail_event)
            except Exception:
                logger.exception(
                    "%s: flow_consult_failed publish failed", command_type,
                )
            # V2-P2: the live channel's failed pulse (best-effort).
            await _flow_channel.relay_failed(
                {
                    "request_id": request_id,
                    "kind": kind,
                    "flow_id": str(msg.get("flow_id") or ""),
                },
                reason,
            )

        if not request_id:
            logger.warning(
                "%s: missing request_id — dropping (nothing to report to)",
                command_type,
            )
            return
        if supervisor is None:
            await _fail(
                "the office orchestrator was not ready — try again shortly"
            )
            return
        if supervisor.is_agent_busy(agent_name):
            await _fail(
                f"the {display} is already running another consult — only "
                "one runs at a time; try again once it reports back"
            )
            return
        agent_config = config_store.get_agent(agent_name)
        if not agent_config:
            await _fail(
                f"the {agent_name} agent is not configured for this office "
                "(restart cbcl to resync)"
            )
            return

        from src.orchestrator.flow_consult_prompt import (
            build_flow_consult_prompts,
        )

        try:
            system_prompt, user_prompt = build_flow_consult_prompts(
                agent_name, msg
            )
        except Exception:
            logger.exception("%s: prompt assembly failed", command_type)
            await _fail("the consult prompt could not be assembled")
            return

        # Lean marker — echoed verbatim on the worker's completion frame
        # (the planner_consult posture), so the big prompts ride SEPARATE
        # task_data keys instead of the marker.
        marker = {
            "request_id": request_id,
            "kind": kind,
            "role": (
                "architect" if agent_name == "flow-architect" else "curator"
            ),
            "flow_id": str(msg.get("flow_id") or ""),
            "mode": str(msg.get("mode") or ""),
        }
        synthetic_id = f"flow-consult-{_uuid.uuid4().hex[:12]}"
        task_data = {
            "task_id": synthetic_id,
            "readable_id": "FLOW",
            "title": f"{display} consult",
            "status": "consulting",
            "priority": "high",
            "brief": {},
            "flow_consult": dict(marker),
            "flow_consult_system_prompt": system_prompt,
            "flow_consult_user_prompt": user_prompt,
        }
        spawned = await supervisor.spawn_worker(
            agent_name, agent_config, task_data
        )
        if not spawned:
            logger.warning(
                "%s: failed to spawn %s session (request %s)",
                command_type, agent_name, request_id[:8],
            )
            await _fail(
                "the consult session failed to start (the office may be at "
                "its agent limit) — try again shortly"
            )
            return
        # Stash for crash recovery (supervisor-synthesized fatals carry no
        # marker) + the progress-relay throttle stamp.
        _flow_consults[synthetic_id] = dict(marker)
        started_message = (
            f"{display} session started"
            + (f" ({marker['mode']} mode)" if marker["mode"] else "")
        )
        try:
            await router.publish_event({
                "type": "flow_consult_progress",
                "request_id": request_id,
                "message": started_message,
            })
        except Exception:
            logger.exception(
                "%s: initial flow_consult_progress publish failed",
                command_type,
            )
        # V2-P2: the live channel's working pulse — the FE typing
        # indicator turns on the moment the session spawns
        # (best-effort; the poll stays the durable signal).
        await _flow_channel.relay_started(marker, started_message)

    async def _handle_consult_flow_architect(msg: dict) -> None:
        await _run_flow_consult("consult_flow_architect", msg)

    async def _handle_consult_data_curator(msg: dict) -> None:
        await _run_flow_consult("consult_data_curator", msg)

    router.on("chat_message", mgr.handle_chat_message)
    router.on("switch_context", mgr.handle_switch_context)
    router.on("cancel_turn", mgr.cancel_current_turn)
    router.on("scope_completed", mgr.ingest_scope_completed)
    router.on("task_completed", mgr.ingest_task_completed)
    router.on("consult_planner", _handle_consult_planner)
    # Flow Studio (FS-P3.T4): the two async consult commands.
    router.on("consult_flow_architect", _handle_consult_flow_architect)
    router.on("consult_data_curator", _handle_consult_data_curator)
    if consult_planner_ref is not None:
        # Hand the handler back to ``init_office_process_model`` so the
        # verdictless-verify refire can dispatch a consult locally.
        consult_planner_ref.append(_handle_consult_planner)
    router.on(
        "action_request_decided",
        mgr.ingest_action_request_decided,
    )
    router.on(
        "action_request_auto_decide",
        mgr.ingest_action_request_auto_decide,
    )
    router.on(
        "action_request_reconcile",
        mgr.ingest_action_request_reconcile,
    )
    router.on("task_ready", _handle_task_ready)
    router.on("task_rework", _handle_task_rework)
    router.on("task_updated", _handle_task_updated)
    router.on("task_moved", _handle_task_moved)
    router.on("sync_config", _handle_sync_config)
    router.on(
        "script_execute",
        lambda msg: handle_script_execute(msg, script_runner),
    )
    # D-08: the Stop button's other half. Registered beside script_execute
    # because it is the same surface — the runner owns the process, and
    # ScriptRunner.kill already does the container-side work.
    router.on(
        "script_kill",
        lambda msg: handle_script_kill(msg, script_runner),
    )
    router.on(
        "script_secret_update",
        lambda msg: handle_script_secret_update(msg, secrets_store),
    )

    # cbcl 0.2.49+: backend forwards the in-container MCP's
    # ``request_outbox_scan`` tool call here. Triggers the same
    # ``scan_outbox_for(name)`` flow the old tool-proxy
    # ``/outbox-scan`` endpoint used to call directly. Replaced the
    # tool-proxy hop with a backend round-trip so we benefit from
    # ``_call_backend``'s proxy → direct-backend fallback + 3-retry
    # behaviour. Best-effort: a missing script_name or runner error
    # is logged but doesn't tear down the daemon.
    async def _handle_scan_outbox(msg: dict) -> None:
        name = (msg.get("script_name") or "").strip()
        if not name:
            logger.warning(
                "scan_outbox: missing script_name in message %s", msg,
            )
            return
        try:
            dispatched = await script_runner.scan_outbox_for(name)
            if dispatched:
                logger.info(
                    "scan_outbox: delivered %d notify(s) for %s",
                    dispatched, name,
                )
        except Exception:
            logger.exception(
                "scan_outbox: scan_outbox_for(%s) failed", name,
            )

    router.on("scan_outbox", _handle_scan_outbox)
    if variable_manager is not None:
        # Phase 1.5: per-variable binding writes. Defensive guard
        # against the optional kwarg — every production call site
        # passes it, but a test harness wiring a partial router
        # without the variable manager should not crash.
        router.on(
            "script_variable_binding_set",
            lambda msg: handle_script_variable_binding_set(
                msg, variable_manager, secrets_store,
            ),
        )
    router.on(
        "skill_secret_update",
        lambda msg: handle_skill_secret_update(msg, secrets_store),
    )

    # SSH-key add/delete from the chat WS relay. The handler
    # fingerprints + writes the key file (host + live container)
    # and replies with the canonical metadata for the backend to
    # persist. The private key value flows through ``msg`` only —
    # never logged.
    async def _send_to_backend(reply: dict) -> None:
        # ``publish_event`` enriches with the message_uuid +
        # published_at metadata the backend's EventDispatcher uses
        # for idempotency, so two retries of the same ssh_key_added
        # land as one row.
        await router.publish_event(reply)

    async def _handle_ssh_key_add(msg: dict) -> None:
        from src.ssh_keys.handlers import handle_ssh_key_add
        await handle_ssh_key_add(
            msg, office, container_name, _send_to_backend,
        )

    async def _handle_ssh_key_delete(msg: dict) -> None:
        from src.ssh_keys.handlers import handle_ssh_key_delete
        await handle_ssh_key_delete(
            msg, office, container_name, _send_to_backend,
        )

    router.on("ssh_key_add", _handle_ssh_key_add)
    router.on("ssh_key_delete", _handle_ssh_key_delete)

    # Office-secret add/delete from the chat WS relay. Same security
    # rationale as the SSH-key path — the value flows through ``msg``
    # only, never logged, never persisted server-side. The store
    # writes a single host JSON file the Script Runner reads at
    # ``docker exec`` time to inject ``-e NAME=VALUE`` env flags.
    async def _handle_office_secret_set(msg: dict) -> None:
        from src.office_secrets.handlers import handle_office_secret_set
        await handle_office_secret_set(
            msg, office, _send_to_backend,
        )

    async def _handle_office_secret_delete(msg: dict) -> None:
        from src.office_secrets.handlers import (
            handle_office_secret_delete,
        )
        await handle_office_secret_delete(
            msg, office, _send_to_backend,
        )

    router.on("office_secret_set", _handle_office_secret_set)
    router.on("office_secret_delete", _handle_office_secret_delete)

    async def _handle_office_deleted(msg: dict) -> None:
        """P3-G: body in ``src._handlers._office_lifecycle``."""
        await handle_office_deleted(
            msg, delete_queue=delete_queue, office=office,
        )

    router.on("office_deleted", _handle_office_deleted)

    async def _handle_office_created(msg: dict) -> None:
        """P3-G: body in ``src._handlers._office_lifecycle``."""
        await handle_office_created(msg, create_queue=create_queue)

    router.on("office_created", _handle_office_created)

    async def _handle_mcp_list(msg: dict) -> None:
        """On-demand refresh of the MCP list cache.

        ``force=True`` bypasses the 5-second debounce in
        ``refresh_mcp_list``. The user clicked Refresh (or any
        client called ``POST /mcp/refresh``) precisely BECAUSE
        they want the cache busted right now — without ``force``,
        a click landing within 5s of any earlier refresh (very
        common: office-startup syncs + post-mutation refreshes
        all fire one) was silently swallowed and the UI got the
        same stale data back.
        """
        await _refresh_mcp_list(force=True)

    async def _handle_improve_office_config(msg: dict) -> None:
        """P3-G: body in ``src._handlers._setup``."""
        await run_improve_office_config(
            msg,
            router=router,
            container_name=container_name,
        )

    async def _handle_generate_office_config(msg: dict) -> None:
        """P3-G: body in ``src._handlers._setup``."""
        await run_generate_office_config(
            msg, router=router, container_name=container_name,
        )

    async def _handle_analyze_office_description(msg: dict) -> None:
        """P3-G: body in ``src._handlers._setup``."""
        await run_analyze_office_description(
            msg, router=router, container_name=container_name,
        )

    router.on("task_kill", _handle_task_kill)
    router.on("mcp_list", _handle_mcp_list)
    router.on("mcp_add", _handle_mcp_add)
    router.on("mcp_remove", _handle_mcp_remove)
    # MCP connectors that need OAuth are connected in the Claude app, not via
    # Cubicle (see the frontend McpAuthDialog instruction card). The former
    # in-app OAuth-connect handlers (mcp_authenticate / mcp_cli_auth /
    # mcp_write_token) + their modules (mcp_auth.py, _handlers/_oauth.py) were
    # removed as dead code. ``publish_mcp_command`` now only emits add/remove/list.
    router.on("generate_office_config", _handle_generate_office_config)
    router.on("improve_office_config", _handle_improve_office_config)
    router.on("analyze_office_description", _handle_analyze_office_description)

