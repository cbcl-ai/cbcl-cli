"""Worker task-session runner for ``agent_worker`` (Wave 10 decomposition).

Extracted from ``agent_worker.py`` — the biggest single concern in
the daemon: the assign-task IPC handler + the streaming Claude CLI
runner that drives each worker task. ~750 lines of behaviour
preserved verbatim, with ``self`` → ``worker`` parameter renaming
and one-level dedent.

The handler is the IPC entry point the Orchestrator drops an
``assign_task`` IPC frame at. It builds the worker system prompt
(including subagent definitions, retry guidance, and rework
feedback), then calls ``run_sdk_session`` to drive the Claude CLI
subprocess that does the actual work.

The runner streams per-token output, handles retries, manages
session lock state, and emits ``task_complete`` /
``response_chunk`` / ``activity`` IPC frames back. It's the
hottest path in the daemon — every worker task goes through here.

Adapters in ``agent_worker.py`` keep the class-method call shape
stable for every existing caller.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from src.agent_protocol import MessageType
from ._agent_worker_mcp import _CLAUDE_CLI_BUILTIN_DISALLOW
from ._tool_summary import build_tool_activity

if TYPE_CHECKING:
    from src.agent_worker import AgentWorker

# Runtime imports for symbols this module RAISES / CALLS — not just
# type hints. ``AgentErrorEscalation`` stays defined in
# ``agent_worker`` (its module-public home); we re-import it here so
# the ``raise`` + ``except`` sites work after the Wave 10 extract.
# The import is at module top — the load order is OK because
# ``agent_worker.py`` only imports THIS module lazily (inside the
# adapter method bodies), so no circular import on first load.
from src.agent_worker import (  # noqa: E402
    AgentErrorEscalation,
    _ERROR_PREVIEW_LENGTH,
    _ESCALATION_ORIGINAL_LENGTH,
    _MAX_SESSION_ATTEMPTS,
    _MAX_SESSION_WALLCLOCK_SECONDS,
    _MAX_SYSTEM_PROMPT_SIZE,
)
from src.orchestrator.error_classifier import (  # noqa: E402
    INFRA_OUTAGE_CLASSES,
    ErrorClass,
    classify_error,
)


logger = logging.getLogger(__name__)

# FIX W1 (blink-resilience): infra-classed retry exhaustion gets a bounded
# DEFERRED-RESUME ladder before the terminal blocked escalation. A 529
# outage that outlives the ~9-minute quick-retry budget used to land the
# task in ``blocked`` permanently (blocked → ready is Manager-only and
# bounce-capped; MA triage is document-and-escalate) — dead work for the
# rest of any provider incident, while USAGE_LIMIT got a timed auto-resume.
# Mirror that posture: park the SLEEPING worker process (task stays
# in_progress with a live session, exactly like the usage-limit defer;
# a daemon restart re-dispatches the orphan, which re-enters the wait)
# for 15 then 30 minutes, each wait granting ONE more attempt. Only after
# the ladder is exhausted does the existing AgentErrorEscalation → blocked
# + escalate_blocker path fire. Bounded by the ladder length AND the
# 6-hour wall-clock budget; every wait is user-visible as a ⏸ Paused
# activity. Execute-mode board tasks only — reviewer sessions already
# have the no-rework-cost re-queue path, triage stays in blocked, and
# Planner consults get the one-shot infra re-fire in ``handlers`` instead
# (a sleeping consult would trip the stall watchdog).
_INFRA_DEFER_DELAYS_SECONDS: tuple[float, ...] = (900.0, 1800.0)


def apply_secret_env_allowlist(
    office_secret_env: dict[str, str],
    allowlist: list[str] | None,
) -> dict[str, str]:
    """Filter the office-secret env through a per-agent allowlist (T2.2.3).

    * ``allowlist is None`` → return the env unchanged (inject ALL office
      secrets — the user-mandated default).
    * ``allowlist == []`` → inject NONE.
    * non-empty list → inject ONLY the named secrets that exist.

    Pure + side-effect-free so it can be unit-tested without spinning up a
    worker session. The caller (``run`` below) reads the allowlist from the
    synced ``agent_config`` dict.
    """
    if allowlist is None:
        return office_secret_env
    allowed = set(allowlist)
    return {k: v for k, v in office_secret_env.items() if k in allowed}


def _brief_is_usable(brief: object) -> bool:
    """True when the in-hand brief can render a real contract.

    T3.2.4 (03/#17): reconcile-added queue entries carry NO brief —
    only the fresh ``get_task_detail`` fetch repairs that. The brief
    is "usable" when it's a non-empty dict with a non-empty ``goal``
    (the same signal the Phase-0 empty-task guard keys on: a missing
    goal renders the worker prompt as "Goal: Not specified").
    """
    if not isinstance(brief, dict) or not brief:
        return False
    return bool(str(brief.get("goal") or "").strip())


async def handle_assign_task(worker: "AgentWorker", msg: dict) -> None:
    """Execute a worker task using the Claude Agent SDK.

    This is the main work handler. It receives the full task data
    (brief, agent config, workspace path), builds the worker prompt,
    and runs an SDK session via the container's agent runner.

    On success: sends task_complete with status="review".
    On cancellation: sends task_complete with status="blocked".
    On error: sends error message (non-fatal, agent stays alive).

    Args:
        msg: The assign_task message dict.
    """
    task_id = msg.get("task_id", "")
    readable_id = msg.get("readable_id", "")
    task_status = msg.get("status", "ready")
    worker._current_task_id = task_id
    # Fresh task → forget any cancellation source recorded for a
    # prior assignment, so an external_cancel that fires here
    # isn't misattributed to a stale shutdown / explicit_cancel
    # signal from an earlier life of this subprocess.
    worker._cancellation_source = None

    is_review = task_status == "review"
    is_triage = task_status == "blocked"
    # Planner consult (execution_improvements_v1): a synthetic, non-board
    # assignment. Runs the planner prompt; its completion must NOT move a
    # task (there is none) — it pokes the Manager instead.
    is_planner = bool(msg.get("planner_consult"))
    if is_planner:
        mode = "PLANNER"
    elif is_review:
        mode = "REVIEW"
    elif is_triage:
        mode = "TRIAGE"
    else:
        mode = "EXECUTE"
    logger.info("Assigned task %s (%s) — mode: %s", readable_id, task_id, mode)

    try:
        agent_config = msg.get("agent_config", {})
        worker._agent_config = agent_config

        # Run the SDK session
        session_id, total_cost = await worker._run_sdk_session(
            agent_config=agent_config,
            task_data=msg,
        )

        # FIX U1: dynamic-workflow subagent failures observed during the
        # session ride the completion payload so downstream consumers
        # (the planner outcome gates, reviewer-facing telemetry) can
        # weigh "clean exit with N dead phases" as suspicious instead of
        # silently green. Zero when no workflow ran / nothing failed.
        sidechain_failures = int(
            getattr(worker, "_sidechain_failures", 0) or 0
        )
        # AREA-1 fix 3 (verify turn-end incident 2026-07-17): spawn
        # tool_use ids whose tool_result never arrived by clean stream
        # end — "ended the turn with a workflow still running". Rides
        # the completion payload alongside ``sidechain_failures`` so
        # the planner honesty check can NAME the trap in its poke copy
        # without waiting on the scope fetch.
        pending_spawns = int(getattr(worker, "_pending_spawns", 0) or 0)

        # Check if task was skipped (already done or reassigned)
        if session_id is None and total_cost is None:
            logger.info("Task %s skipped (state changed)", readable_id)
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": task_status,  # Keep current status
                "comment": "Task skipped — state changed since dispatch.",
                "token_cost": 0.0,
                "session_id": "",
                "is_review_completion": True,  # Don't trigger auto-unassign
                # ADD-A5: the session did NO actual work. The orchestrator's
                # MA auto-approve path must NOT treat this as a positive
                # review and ship the task to done unreviewed.
                "review_skipped": True,
            })
            return

        # Report completion — behavior depends on task state
        if is_review:
            # Reviewer: task stays in Review. The reviewer should have
            # posted findings and unassigned the task during execution.
            # We report completion but DON'T trigger status change or
            # auto-unassign — the reviewer handles that explicitly.
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "review",  # Stay in review
                "comment": "Review complete.",
                "token_cost": total_cost or 0.0,
                "session_id": session_id or "",
                "is_review_completion": True,  # Flag: don't auto-unassign
                "sidechain_failures": sidechain_failures,
                "pending_spawns": pending_spawns,
            })
        elif is_triage:
            # Triage dispatch on a blocked task. The MA (or whoever
            # was dispatched here) ran its playbook, posted a
            # synthesis comment, and exited — the task STAYS in
            # blocked. We MUST flag this as a non-status-changing
            # completion so the orchestrator's task_complete handler
            # doesn't try ``move_task(blocked → review)`` (which
            # would fail board-validation and ALSO incorrectly
            # imply the agent finished work). Without this flag
            # the handler kept attempting an invalid transition
            # every triage exit, generating spurious error logs
            # and feeding the reconciler→re-dispatch loop on
            # blocked tasks. (TO-007.T40 regression, 2026-05-14.)
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "blocked",  # Stay in blocked
                "comment": "Triage complete.",
                "token_cost": total_cost or 0.0,
                "session_id": session_id or "",
                "is_review_completion": True,  # Re-use the "don't move" flag
                "sidechain_failures": sidechain_failures,
                "pending_spawns": pending_spawns,
            })
        elif is_planner:
            # Planner consult done. No board task to move — flag the
            # completion as non-status-changing and carry the consult
            # marker so the orchestrator routes it to the Manager poke
            # (ingest_planner_result) instead of move_task.
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "planning",
                "comment": "Planner consult complete.",
                "token_cost": total_cost or 0.0,
                "session_id": session_id or "",
                "is_review_completion": True,  # don't move a task
                "planner_consult": msg.get("planner_consult"),
                "sidechain_failures": sidechain_failures,
                "pending_spawns": pending_spawns,
            })
        else:
            # Executor: move to review for Manager
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "review",
                "comment": "Task execution complete.",
                "token_cost": total_cost or 0.0,
                "session_id": session_id or "",
                "is_review_completion": False,
                "sidechain_failures": sidechain_failures,
                "pending_spawns": pending_spawns,
            })

    except asyncio.CancelledError:
        # Cancellation source is whatever the cancel/shutdown/
        # signal handlers stamped earlier. None means nothing in
        # THIS process initiated the cancel — supervisor reap,
        # heartbeat timeout, daemon restart, container kill, etc.
        # "external_cancel" is the catch-all; the backend's
        # task_errors row makes the distinction queryable.
        cancellation_source = (
            worker._cancellation_source or "external_cancel"
        )
        logger.info(
            "Task %s cancelled (source=%s)",
            readable_id,
            cancellation_source,
        )
        # Telemetry event FIRST — the backend's task_activity
        # handler picks up error rows with details.error_class
        # and writes a queryable task_errors entry. The
        # task_complete frame below changes status and is
        # consumed by a different handler path that DOESN'T
        # carry structured error context, which is exactly why
        # the user sees a bare "Task was cancelled." today.
        try:
            worker._send({
                "type": MessageType.PROGRESS,
                "task_id": task_id,
                "event_type": "error",
                "content": (
                    f"Worker session for {readable_id} was "
                    f"cancelled ({cancellation_source})."
                ),
                "details": {
                    "error_class": "cancelled",
                    "cancellation_source": cancellation_source,
                    "retryable": False,
                },
            })
        except Exception:
            # ``_send`` writes NDJSON to stdout; the only way this
            # raises is if stdout is closed, in which case the
            # process is already dying. Swallow so we still emit
            # the task_complete below if we can.
            logger.exception(
                "Failed to emit cancellation telemetry for task %s",
                task_id,
            )
        if is_review:
            # MEDIUM-3 (mirrors the AgentErrorEscalation review split
            # below): a cancelled REVIEWER session must NOT emit the
            # executor-shaped ``blocked`` completion — that routed
            # through the orchestrator's executor branch and issued
            # ``move_task(review → blocked)``: MA triage noise, a full
            # re-execution, and a consumed blocked-bounce on every
            # daemon restart with an in-flight reviewer. Stamp it as a
            # review completion with ``details.error_class`` so the
            # orchestrator's infra-guard re-queues the review (bounded
            # by the infra re-queue cap) — no board move.
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "review",  # Stay in review — no move
                "comment": "Task was cancelled.",
                "token_cost": 0.0,
                "session_id": "",
                "is_review_completion": True,
                "details": {
                    "error_class": "cancelled",
                    "cancellation_source": cancellation_source,
                },
            })
        else:
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "blocked",
                "comment": "Task was cancelled.",
                "token_cost": 0.0,
                "session_id": "",
                # Planner consults are synthetic (no board task). Carry the
                # marker so the orchestrator routes this to the Manager poke
                # instead of a phantom move_task on a non-existent task
                # (Phase 3 robustness).
                "planner_consult": msg.get("planner_consult"),
            })
    except AgentErrorEscalation as esc:
        # Error-recovery retries exhausted OR non-retryable error.
        # Move the task to blocked with a structured comment so the
        # Manager Assistant (Board Operator) can pick it up, read
        # the classification, and decide next steps (split task,
        # refresh auth, etc.). Do NOT send ERROR — that would be
        # treated as a fatal agent crash by the supervisor.
        logger.warning(
            "Task %s escalated to MA: class=%s msg=%s",
            readable_id, esc.error_class, esc.escalation_message,
        )
        comment = (
            f"ESCALATED ({esc.error_class}): {esc.escalation_message}\n\n"
            f"Original error: {esc.original_error[:_ESCALATION_ORIGINAL_LENGTH]}\n\n"
            "Manager Assistant: please investigate. Options typically "
            "include splitting this task into smaller pieces, reducing "
            "scope, or (for config/auth errors) asking the user to "
            "resolve the underlying issue."
        )
        if is_review:
            # Retry-exhausted REVIEWER session. The task is sitting in
            # ``review`` — emitting the executor-shaped ``blocked``
            # completion here routed the event through the orchestrator's
            # executor branch, which issued ``move_task(review → blocked)``
            # with the reviewer as actor: an invalid/board-churning move
            # (review → blocked is Manager-only unless the actor is the
            # designated reviewer, and even then an infra fault must not
            # convert a review into a blocked task + escalate_blocker).
            # Stamp review-completion metadata instead so the orchestrator's
            # reviewer branch sees ``details.error_class`` (the T1.1.3
            # infra-guard) and RE-QUEUES the review urgently — no board
            # move, no rework cycle consumed.
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "review",  # Stay in review — no move
                "comment": comment,
                "token_cost": esc.total_cost or 0.0,
                "session_id": esc.session_id or "",
                "is_review_completion": True,
                "details": {
                    "error_class": esc.error_class,
                    "escalation_message": esc.escalation_message,
                },
                # NIT-9: no planner_consult marker here — a Planner
                # consult dispatches with status "planning", never
                # "review", so this branch can't be a consult.
            })
        else:
            worker._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "blocked",
                "comment": comment,
                "token_cost": esc.total_cost or 0.0,
                "session_id": esc.session_id or "",
                "details": {
                    "error_class": esc.error_class,
                    "escalation_message": esc.escalation_message,
                },
                # See note above — route planner consults to the poke, not move_task.
                "planner_consult": msg.get("planner_consult"),
            })
    except Exception as exc:
        logger.exception("Task %s failed: %s", readable_id, exc)
        worker._send({
            "type": MessageType.ERROR,
            "message": str(exc)[:1000],
            "task_id": task_id,
            "fatal": False,
            # Carry the marker so the orchestrator's error branch pokes the
            # Manager with a failure note instead of trying to recover a
            # non-existent synthetic task (Phase 3 robustness).
            "planner_consult": msg.get("planner_consult"),
        })
    finally:
        worker._current_task_id = None


async def run_sdk_session(
    worker: "AgentWorker",
    agent_config: dict,
    task_data: dict,
) -> tuple[str | None, float | None]:
    """Run a Claude CLI session via docker exec and stream events.

    Invokes the Claude CLI directly inside the Docker container using
    ``docker exec``. Events are streamed to the Orchestrator via
    stdout NDJSON messages.

    Returns:
        Tuple of (session_id, total_cost).
    """
    from src.docker.session_bridge import stream_cli_session
    from src.orchestrator.worker_prompt import build_worker_prompt

    task_id = task_data.get("task_id", "")
    is_planner_consult = bool(task_data.get("planner_consult"))

    # Always fetch fresh task details from the backend to ensure
    # we have the latest state, brief, activities, and artifacts.
    # Skip for a Planner consult — its task_id is synthetic
    # ("planner-<uuid>") and has no backend task row to fetch.
    detail_fetch_ok = False
    if worker.backend_url and task_id and not is_planner_consult:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{worker.backend_url}/api/offices/{worker.office_id}/tool-call",
                    json={"action": "get_task_detail", "params": {"task_id": task_id}},
                    # SEC3-01: this host-subprocess call authenticates with the
                    # per-office X-Office-Secret (injected into the subprocess
                    # env by the supervisor), so the backend accepts it once
                    # /tool-call auth is enforced.
                    headers={
                        "X-Office-Secret": os.environ.get(
                            "CUBICLE_OFFICE_TOOL_SECRET", ""
                        )
                    },
                )
                if resp.status_code == 200:
                    detail_fetch_ok = True
                    detail = resp.json()
                    task_data["brief"] = detail.get("brief", task_data.get("brief", {}))
                    task_data["title"] = detail.get("title", task_data.get("title", ""))
                    task_data["reviewer"] = detail.get("reviewer") or task_data.get("reviewer", "")
                    task_data["rework_count"] = detail.get("rework_count", task_data.get("rework_count", 0))
                    task_data["recent_activities"] = detail.get("recent_activities", [])
                    task_data["artifacts"] = detail.get("artifacts", [])
                    # ADD-D1: carry the backend's partial-fetch signal so a
                    # review dispatch can abort rather than review blind (the
                    # backend sets this when it could not assemble the full
                    # artifact list, distinct from a legitimately empty one).
                    task_data["artifacts_partial"] = bool(
                        detail.get("artifacts_partial", False)
                    )

                    # Check if task state has changed since dispatch
                    current_status = detail.get("status", "")
                    current_agent = detail.get("assigned_agent") or ""

                    if current_status in ("done", "archived"):
                        logger.info("Task %s already %s — skipping", task_id, current_status)
                        return None, None

                    # Check authorization: agent must be either the
                    # assigned executor OR the designated reviewer.
                    current_reviewer = detail.get("reviewer") or ""
                    is_authorized = (
                        not current_agent
                        or current_agent == worker.agent_name
                        or current_reviewer == worker.agent_name
                    )
                    if not is_authorized:
                        logger.info(
                            "Task %s not assigned to us (%s) — agent=%s reviewer=%s — skipping",
                            task_id, worker.agent_name, current_agent, current_reviewer,
                        )
                        return None, None

                    # Task is assigned to us (as executor or reviewer).
                    # The agent's prompt (review vs execute mode) tells
                    # it what to do based on the task state.

                    # Update status and agent from fresh data
                    task_data["status"] = current_status
                    task_data["assigned_agent"] = worker.agent_name
                    logger.info("Fetched fresh task details for %s (status=%s)", task_id, current_status)
                else:
                    logger.warning(
                        "get_task_detail returned HTTP %d for task %s",
                        resp.status_code, task_id,
                    )
        except Exception as exc:
            logger.warning("Failed to fetch task details: %s", exc)

    # T3.2.4 (03/#17): a failed brief/detail fetch must ABORT the
    # attempt instead of running contract-less. Reconcile-added queue
    # entries carry no brief; if the repair fetch failed AND nothing
    # usable rode the queue entry, the worker prompt would render an
    # empty contract ("Goal: Not specified") — the exact bug class the
    # Phase-0 empty-task guard kills — and burn a full Opus session on
    # un-reviewable output. Emit a non-fatal error activity and return
    # the skip sentinel: no CLI session starts, the orchestrator keeps
    # the task's status untouched, and the 60s reconciler re-adds the
    # entry for a retry once the backend is reachable. Planner consults
    # (which carry their own objective and skip the fetch entirely)
    # keep the existing tolerance.
    if (
        not is_planner_consult
        and task_id
        and not detail_fetch_ok
        and not _brief_is_usable(task_data.get("brief"))
    ):
        logger.warning(
            "Aborting attempt for task %s — detail/brief fetch failed "
            "and no usable brief is in hand (no CLI session started; "
            "the reconciler will re-queue the entry)",
            task_id,
        )
        worker._send({
            "type": MessageType.PROGRESS,
            "task_id": task_id,
            "event_type": "error",
            "content": (
                "Could not fetch the Task Brief from the backend — "
                "aborting this attempt instead of executing without a "
                "contract. The task will be retried automatically."
            ),
            "details": {
                "error_class": "brief_fetch_failed",
                "retryable": True,
            },
        })
        return None, None

    # ADD-D1: a PARTIAL detail fetch (HTTP 200, brief in hand, but the
    # backend could not assemble the artifact list) must not produce a
    # BLIND review — abort and let the 60s reconciler re-queue rather than
    # reviewing with an incomplete deliverable list. Gate on review-status
    # ALONE: any session that reaches here with status=="review" has already
    # passed the authorization check above, so it is EITHER the designated
    # reviewer OR a stranded review with an empty ``assigned_agent`` (the
    # transient window before the assignment sweeper re-binds the executor)
    # that the Manager Assistant picked up and cleared auth via
    # ``not current_agent``. Keying on ``reviewer == agent_name`` would MISS
    # that empty-assignee stranded case and let it ship a blind verdict. We
    # never key on ``len(artifacts) == 0`` — a legitimately artifact-less
    # review must still proceed; only the partial-FETCH flag aborts.
    if (
        not is_planner_consult
        and task_id
        and detail_fetch_ok
        and task_data.get("artifacts_partial")
        and task_data.get("status") == "review"
    ):
        logger.warning(
            "Aborting REVIEW attempt for task %s — backend flagged "
            "artifacts_partial (deliverable list incomplete); re-queueing "
            "rather than reviewing blind",
            task_id,
        )
        worker._send({
            "type": MessageType.PROGRESS,
            "task_id": task_id,
            "event_type": "error",
            "content": (
                "Could not assemble the full artifact list from the "
                "backend — aborting this review attempt instead of "
                "reviewing without the complete deliverable set. The "
                "review will be retried automatically."
            ),
            "details": {
                "error_class": "artifacts_fetch_partial",
                "retryable": True,
            },
        })
        return None, None

    container_name = agent_config.get("_container_name", "")

    # Office secrets → worker shell env. The user-mandated "agents work
    # with secrets directly, out of scripts" model: read the host-only
    # office-secrets store and inject every entry into the agent's CLI
    # session as an env var (name-only ``docker exec -e KEY``; the value
    # rides the subprocess env, never the host argv — same posture as the
    # Script Runner). This lets agents use GITLAB_PAT / API keys directly
    # (git push, curl, CLIs) without the script chokepoint. Best-effort:
    # a missing/corrupt store just means no secret env (the agent still
    # runs). Read once per task; picks up the latest values each attempt.
    office_secret_env: dict[str, str] = {}
    try:
        from pathlib import Path as _Path

        from src.office_secrets.store import read_office_secrets

        _slug = _Path(worker.workspace_path).name
        office_secret_env = read_office_secrets(_slug) or {}
        # T2.2.3 (03/#6): optional per-agent allowlist scopes which office
        # secrets reach this agent's session. ``None`` (the default) =
        # inject all, preserving the user-mandated "agents work with
        # secrets directly" model. A list = inject ONLY those names; ``[]``
        # = inject none. Filter is name-based and applied before the env is
        # threaded into the CLI session.
        allowlist = agent_config.get("secret_env_allowlist")
        if office_secret_env and allowlist is not None:
            before = len(office_secret_env)
            office_secret_env = apply_secret_env_allowlist(
                office_secret_env, allowlist
            )
            logger.info(
                "Applied secret_env_allowlist for %s: %d/%d office "
                "secret(s) pass the allowlist for task %s",
                worker.agent_name, len(office_secret_env), before, task_id,
            )
        if office_secret_env:
            logger.info(
                "Injecting %d office secret(s) into %s session for task %s",
                len(office_secret_env), worker.agent_name, task_id,
            )
    except Exception:
        # Corrupt/unreadable store or unexpected error — never block the
        # task on it; the agent runs without the secret env.
        logger.warning(
            "Could not load office secrets for the worker session "
            "(task %s); continuing without secret env",
            task_id, exc_info=True,
        )

    # F5/R2-F9 (audit): explicit fallback via the central constant.
    # Fires only when the orchestrator dispatched a malformed config;
    # log loudly so the gap surfaces.
    from src.orchestrator._model_defaults import FALLBACK_WORKER_MODEL
    model = agent_config.get("model") or FALLBACK_WORKER_MODEL
    if not agent_config.get("model"):
        logger.warning(
            "Worker agent_config missing 'model' for task %s — "
            "falling back to %s. Investigate the orchestrator "
            "dispatch path.",
            task_id, FALLBACK_WORKER_MODEL,
        )

    # Item-6: reasoning-effort + ultracode (dynamic-workflow) policy for this
    # agent. ``effort`` is opus-tier-only (None = CLI default). For a plain
    # effort level the agent works alone -> the Agent/Task sub-agent tools are
    # disallowed. For ``effort == "ultracode"`` the policy returns a
    # ``--settings '{"ultracode": true}'`` payload (xhigh + dynamic workflows)
    # and leaves the sub-agent tools allowed. Pure functions (unit-tested in
    # test_session_policy). ``agent_config_for_assignment`` applies the
    # per-assignment override: by DEFAULT it is a pass-through — a Planner
    # VERIFY consult keeps the configured ultracode (2026-07-17 user
    # decision; verdict safety = the verdictless-exit honesty check +
    # prompt pins, not effort downgrades). Only when the operator sets
    # CBCL_VERIFY_FORCE_PLAIN_EFFORT is verify forced to plain xhigh
    # (spawn tools disallowed, no ultracode settings).
    from src._session_policy import (
        _SUBAGENT_TOOLS,
        agent_config_for_assignment,
        build_session_policy,
        is_unknown_flag_error,
    )
    session_effort, session_settings_json, session_disallowed = (
        build_session_policy(
            agent_config_for_assignment(agent_config, task_data), model
        )
    )
    # NOTE: We intentionally do NOT pass --allowed-tools to the Claude CLI.
    # The agent's allowed tools are documented in their CLAUDE.md, and the
    # MCP tool server defines which cubicle-tools are available. Passing
    # --allowed-tools would block MCP connector tools (Notion, Figma, etc.)
    # that are configured in the container via `claude mcp add`.
    allowed_tools: list[str] | None = None

    # Build the system prompt from the task brief only.
    # The agent's CLAUDE.md is auto-discovered by Claude CLI from the
    # per-agent working directory (/workspace/agents/{name}/CLAUDE.md).
    # The office-level CLAUDE.md (/workspace/CLAUDE.md) is also
    # auto-discovered via directory hierarchy.
    if is_planner_consult:
        from src.orchestrator.planner_prompt import build_planner_prompt

        system_prompt = build_planner_prompt(task_data)
        prompt = (
            "Carry out the planning consult described in the system prompt."
        )
    else:
        system_prompt = build_worker_prompt(task_data)
        prompt = (
            f"Execute the task as described in the system prompt. "
            f"Task ID: {task_id}"
        )

    # Per-agent working directory for Claude CLI
    agent_cwd = f"/workspace/agents/{worker.agent_name}"

    # Build MCP config for worker tools. ``triage`` is the new
    # mode for MA dispatch on a blocked task — the MCP server
    # uses it to refuse ``update_status`` / ``move_task`` on the
    # current blocked task, enforcing the playbook rule that the
    # MA never moves blocked → ready itself.
    status_now = task_data.get("status")
    if status_now == "review":
        task_mode = "review"
    elif status_now == "blocked":
        task_mode = "triage"
    else:
        task_mode = "execute"
    mcp_config = worker._build_mcp_config(
        "worker",
        task_id,
        task_mode=task_mode,
        workstream_short_code=task_data.get("workstream_short_code") or None,
        scope_readable_id=task_data.get("scope_readable_id") or None,
        task_readable_id=task_data.get("readable_id") or None,
    )

    total_cost: float | None = None
    session_id: str | None = None

    # MCP tool prefixes reported LEAN (name only, no payload) in
    # progress reporting. Cubicle-internal tool params can carry brief
    # text / plan content that doesn't belong in the feed, but total
    # suppression starved it: a Planner materialize/verify phase is
    # almost ALL mcp__cubicle-tools calls, so the feed's sliding TTL
    # expired mid-consult with zero pushes (incident 2026-07-16).
    _skip_prefixes = (
        "mcp__cubicle",
    )

    # Preserve session across rework cycles for context continuity.
    # On rework the backend passes the prior executor/reviewer session_id;
    # we resume it here so the agent sees its own prior work + feedback.
    prior_session_id = task_data.get("prior_session_id") or None
    if prior_session_id:
        logger.info(
            "Resuming prior session %s for task %s (rework cycle)",
            prior_session_id, task_id,
        )

    # Error-aware retry loop. If the CLI returns an error we classify
    # it and either retry with a remedy (raised token limits, adjusted
    # prompt, fresh session) or escalate by raising.
    #
    # Context preserved across retries:
    #   - session_id (most recent non-null from `result` messages)
    #   - total_cost (most recent from `result` messages)
    #   - _output_locked (once a terminal tool is seen, stays locked
    #     for the rest of the worker lifetime — not reset on retry
    #     because the task is already submitted and further output
    #     is spurious)
    _output_locked = False
    current_prompt = prompt
    current_system_prompt = system_prompt
    current_resume = prior_session_id
    current_env: dict[str, str] = {}
    # Item-6: effort / --settings (ultracode) may be dropped on a flag-support
    # mismatch (older container CLI) — track per-attempt so the degrade path
    # can null them and retry without the flags.
    current_effort = session_effort
    current_settings_json = session_settings_json
    # One-shot guard: when an older container CLI rejects --effort/--settings
    # we strip them and grant ONE extra attempt (the flags caused the
    # failure, not the work) — without this, a flag-support gap on the
    # LAST attempt would escalate a task a no-flag retry could complete.
    flags_degraded = False
    # FIX W1: deferred-resume ladder bookkeeping. ``infra_defer_cycle``
    # counts consumed ladder rungs (each grants +1 attempt);
    # ``infra_defer_seconds`` carries the current rung's wait into the
    # backoff sleep below (consumed once, then reset).
    infra_defer_cycle = 0
    infra_defer_seconds = 0.0
    # FIX U1: count of dynamic-workflow subagent (sidechain) failures
    # observed this assignment — spawn-tool ``tool_result`` errors and
    # sidechain "API Error" text. Lives on the worker (not a local) so
    # every exit path — success return, mid-stream escalation raise —
    # leaves ``handle_assign_task`` the up-to-date count to attach to the
    # TASK_COMPLETE payload ("clean exit with N dead phases" is
    # suspicious, not silently green).
    worker._sidechain_failures = 0
    # Verify turn-end incident 2026-07-17 (AREA-1 fix 3): count of
    # ``Agent``/``Task`` spawn tool_use ids whose ``tool_result`` NEVER
    # arrived by the time the stream ended cleanly — the model ended its
    # turn with a workflow still running (under ``--print`` the process
    # exit kills it). Enrichment for the verdict-shaped honesty check's
    # poke copy, NOT a gate: a background spawn may ack with an
    # immediate tool_result, so a zero here never proves anything.
    worker._pending_spawns = 0
    attempt = 0
    max_attempts = _MAX_SESSION_ATTEMPTS
    # P2-E + P2.5-F: track wall-clock so we can fail-fast on
    # slow-burn retries even if the per-attempt CLI timeout
    # never fires. ``time.monotonic`` is the right clock here:
    # immune to wall-clock jumps and doesn't depend on the
    # event-loop instance (avoids the ``get_event_loop()``
    # deprecation surface).
    wallclock_start = time.monotonic()

    while attempt < max_attempts:
        elapsed = time.monotonic() - wallclock_start
        if elapsed > _MAX_SESSION_WALLCLOCK_SECONDS:
            logger.warning(
                "task %s exceeded wall-clock budget (%.0fs > %ds); escalating",
                task_id, elapsed, _MAX_SESSION_WALLCLOCK_SECONDS,
            )
            raise AgentErrorEscalation(
                error_class="TIMEOUT",
                original_error=(
                    f"Wall-clock budget exhausted after "
                    f"{int(elapsed)}s across {attempt} attempt(s)."
                ),
                escalation_message=(
                    "Task exceeded the 6-hour wall-clock budget across "
                    "retries. Investigate why the CLI is taking so long "
                    "(model rate limits, slow tool calls, infinite "
                    "loops in the prompt) before re-queuing."
                ),
                session_id=session_id,
                total_cost=total_cost,
            )
        attempt += 1
        # All three signals live at loop-scope so the classification
        # block below can read them unconditionally (no NameError
        # games across branches).
        #
        # - last_error_text: populated ONLY when session_bridge emits
        #   an `error` stream event (non-zero exit or timeout). This
        #   is the retry trigger — if it stays None, the CLI
        #   succeeded and we return.
        # - last_api_error: captured opportunistically from assistant
        #   text prefixed "API Error:" OR from result.is_error. Used
        #   to ENRICH classification when an error does fire, since
        #   the raw "exited with code N" string matches no pattern.
        # - last_stderr_text: stderr captured by session_bridge and
        #   piggy-backed on the error event. Second-best enrichment.
        last_error_text: str | None = None
        last_api_error: str | None = None
        last_stderr_text: str = ""

        # Tool-call activity buffer (per attempt). We hold each tool_use
        # keyed by its block id, then emit ONE enriched ``tool_run``
        # activity when its ``tool_result`` arrives (command + output),
        # so the feed shows one CLI-style block per tool instead of a
        # contentless "Using Bash". Unmatched entries are flushed
        # input-only at end of stream.
        pending_tools: dict[str, dict] = {}

        # FIX U1: native subagent-spawn tool calls (``Agent``/``Task`` —
        # the ultracode dynamic-workflow surface) tracked by block id so a
        # later ``tool_result`` with ``is_error`` can be surfaced as a
        # sidechain failure. Tracked SEPARATELY from ``pending_tools``
        # (and scanned before the terminal-tool output lock skips) so a
        # phase failure never vanishes just because the parent already
        # submitted or the enrichment buffer was bypassed.
        spawn_tool_ids: dict[str, str] = {}

        async for msg in stream_cli_session(
            container_name=container_name,
            model=model,
            system_prompt=current_system_prompt,
            prompt=current_prompt,
            cwd=agent_cwd,
            mcp_config=mcp_config,
            allowed_tools=allowed_tools,
            # Always exclude Claude CLI's built-in TaskCreate
            # family — see ``_CLAUDE_CLI_BUILTIN_DISALLOW``. The
            # ``allowed_tools`` whitelist passed above does NOT
            # cover these (Claude CLI built-ins land in the
            # model's tool catalog regardless), so explicit
            # ``--disallowed-tools`` is what keeps them out.
            disallowed_tools=session_disallowed,
            effort=current_effort,
            settings_json=current_settings_json,
            resume_session=current_resume,
            env_overrides=current_env or None,
            secret_env=office_secret_env or None,
        ):
            # P2.5-F: per-message wall-clock check. The
            # between-attempts check at the top of the outer
            # while-loop only fires AFTER an attempt fully
            # finishes. Without this inline check, a slow-burn
            # attempt could individually run past the 6-hour
            # budget (the per-attempt CLI timeout is 4 h) before
            # we even look at the clock. The async generator
            # yields many messages, so this fires roughly once
            # per CLI line — cheap.
            elapsed = time.monotonic() - wallclock_start
            if elapsed > _MAX_SESSION_WALLCLOCK_SECONDS:
                logger.warning(
                    "task %s exceeded wall-clock budget mid-attempt "
                    "(%.0fs > %ds); aborting attempt %d/%d",
                    task_id, elapsed, _MAX_SESSION_WALLCLOCK_SECONDS,
                    attempt, max_attempts,
                )
                raise AgentErrorEscalation(
                    error_class="TIMEOUT",
                    original_error=(
                        f"Wall-clock budget exhausted mid-attempt "
                        f"after {int(elapsed)}s "
                        f"(attempt {attempt}/{max_attempts})."
                    ),
                    escalation_message=(
                        "Task exceeded the 6-hour wall-clock budget. "
                        "Check why the CLI is running so long."
                    ),
                    session_id=session_id,
                    total_cost=total_cost,
                )

            if msg.type == "system":
                # SES-03: the CLI emits a `system` (subtype=init) frame at the
                # START of the run carrying the session_id. Capture it here so
                # a KILL-path retry (process killed BEFORE the `result` frame —
                # e.g. output-token-limit / process_killed) genuinely RESUMES
                # the session instead of starting fresh. Without this the retry
                # guidance ("the session has been resumed, so you retain
                # context") is false on exactly those classes.
                _sid = msg.data.get("session_id")
                if _sid:
                    session_id = _sid

            if msg.type == "result":
                session_id = msg.data.get("session_id") or session_id
                total_cost = (
                    msg.data.get("cost_usd")
                    or msg.data.get("total_cost_usd")
                    or total_cost
                )
                # Claude CLI reports terminal API errors via the final
                # result message: is_error=true with the error text in
                # `result`, or subtype=="error_during_execution". Both
                # paths must feed the classifier so we don't fall back
                # to the contentless exit-code string.
                if (
                    msg.data.get("is_error")
                    or msg.data.get("subtype") == "error_during_execution"
                ):
                    result_err = (
                        msg.data.get("result")
                        or msg.data.get("error")
                        or ""
                    )
                    if isinstance(result_err, str) and result_err.strip():
                        last_api_error = result_err.strip()
            elif msg.type == "assistant":
                # Claude CLI stream-json: content blocks may contain
                # text + tool_use mixed in one message.
                blocks = msg.data.get("message", {}).get("content", [])

                # FIX U1: sidechain-failure surfacing runs BEFORE the
                # terminal-tool output-lock skip — a dynamic-workflow phase
                # failing late in a session (after a verdict locked output)
                # must still leave a record. Two signals:
                #   (a) spawn-tool ids collected here, matched to erroring
                #       ``tool_result`` blocks in the ``user`` branch below;
                #   (b) sidechain frames (``parent_tool_use_id`` set) whose
                #       assistant text is an "API Error" — the shape a
                #       529/limit inside a subagent surfaces as (it never
                #       becomes a parent stream ``error`` frame).
                _is_sidechain = bool(msg.data.get("parent_tool_use_id"))
                if isinstance(blocks, list):
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        if (
                            block.get("type") == "tool_use"
                            and block.get("name") in _SUBAGENT_TOOLS
                            and block.get("id")
                        ):
                            spawn_tool_ids[block["id"]] = block.get(
                                "name", "Agent"
                            )
                        elif (
                            _is_sidechain
                            and block.get("type") == "text"
                            and str(block.get("text") or "")
                            .lstrip()
                            .startswith("API Error")
                        ):
                            worker._sidechain_failures += 1
                            _sc_text = str(block.get("text") or "").strip()
                            worker._send({
                                "type": MessageType.PROGRESS,
                                "task_id": task_id,
                                "event_type": "error",
                                "content": (
                                    "workflow subagent failed: "
                                    f"{_sc_text[:200]}"
                                ),
                                # No ``error_class`` key on purpose — the
                                # backend's task_errors CHECK pins its
                                # enum, and a sidechain failure is NOT a
                                # session-terminal error; this row is
                                # visibility, not retry telemetry.
                                "details": {
                                    "sidechain": True,
                                    "sidechain_failures": (
                                        worker._sidechain_failures
                                    ),
                                },
                            })

                # PRE-SCAN: if ANY block is a terminal tool call,
                # lock output BEFORE processing any block. This
                # prevents same-turn leaks (e.g., text + update_status
                # in one message — the text would leak without pre-scan).
                if not _output_locked:
                    _terminal_tools = (
                        "update_status", "mcp__cubicle-tools__update_status",
                        "move_task", "mcp__cubicle-tools__move_task",
                    )
                    for block in blocks:
                        if block.get("type") == "tool_use":
                            if block.get("name", "") in _terminal_tools:
                                _output_locked = True
                                logger.info(
                                    "Output locked — terminal tool detected: %s",
                                    block.get("name"),
                                )
                                break

                if _output_locked:
                    continue  # Skip entire message

                for block in blocks:
                    if block.get("type") == "text" and block.get("text"):
                        text = block["text"]
                        # Claude CLI surfaces API errors as assistant
                        # text prefixed with "API Error:". Capture the
                        # full text so classify_error receives the
                        # specific diagnostic (e.g. output-token-limit)
                        # and can pick the right remedy instead of
                        # falling through to UNKNOWN_FATAL.
                        stripped = text.lstrip()
                        if stripped.startswith("API Error"):
                            last_api_error = stripped.strip()
                            if _is_sidechain:
                                # FIX U1: already surfaced above as a
                                # sidechain-failure error row — a second
                                # checkpoint with the same text would
                                # just duplicate the feed entry.
                                continue
                        worker._send({
                            "type": MessageType.PROGRESS,
                            "task_id": task_id,
                            "event_type": "checkpoint",
                            "content": text[:500],
                        })
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        if any(
                            tool_name.startswith(p) for p in _skip_prefixes
                        ):
                            # Cubicle-internal MCP tool: emit a LEAN row —
                            # tool name only, no input payload, no
                            # pending_tools buffering (so no enriched "end"
                            # row later). Keeps the feed pulsing through
                            # cubicle-tool-dominated phases without leaking
                            # brief/plan content into the feed.
                            worker._send({
                                "type": MessageType.PROGRESS,
                                "task_id": task_id,
                                "event_type": "tool_run",
                                **build_tool_activity(tool_name, None),
                            })
                        else:
                            tool_use_id = block.get("id") or ""
                            tool_input = block.get("input") or {}
                            # Emit the command IMMEDIATELY (a "running" start)
                            # so the feed shows what the agent is doing live —
                            # even for a multi-minute Bash that won't return a
                            # result for a while. Buffer name/input so the
                            # later tool_result (which carries only the id) can
                            # be enriched into the matching "end" row.
                            if tool_use_id:
                                pending_tools[tool_use_id] = {
                                    "name": tool_name,
                                    "input": tool_input,
                                }
                            worker._send({
                                "type": MessageType.PROGRESS,
                                "task_id": task_id,
                                "event_type": "tool_run",
                                **build_tool_activity(
                                    tool_name, tool_input,
                                    tool_use_id=tool_use_id,
                                    running=True,
                                ),
                            })
            elif msg.type == "user":
                # Claude CLI stream-json surfaces tool OUTPUTS as ``user``
                # frames carrying ``tool_result`` blocks. Match each to the
                # buffered tool_use by id and emit the enriched "end" row
                # (command + redacted output preview); the UI collapses it
                # with the matching "running" start by tool_use_id.
                blocks = msg.data.get("message", {}).get("content", [])
                if not isinstance(blocks, list):
                    continue
                # FIX U1(a): spawn-tool (``Agent``/``Task``) results are
                # checked BEFORE the output-lock skip — a dead workflow
                # phase surfaces (at best) only as a ``tool_result`` with
                # ``is_error`` to the parent, never as a stream ``error``
                # frame, so dropping it here made phase failures invisible
                # to the daemon (00-research U1). Emit a lean error row and
                # count it; recovery stays owned by the parent model + the
                # outcome gates — no retry semantics change.
                for block in blocks:
                    if (
                        not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    _sp_name = spawn_tool_ids.pop(
                        block.get("tool_use_id") or "", None,
                    )
                    if _sp_name is None or not block.get("is_error"):
                        continue
                    from ._tool_summary import output_preview

                    worker._sidechain_failures += 1
                    _sp_preview = output_preview(block.get("content"))
                    worker._send({
                        "type": MessageType.PROGRESS,
                        "task_id": task_id,
                        "event_type": "error",
                        "content": (
                            "workflow subagent failed: "
                            f"{(_sp_preview or 'no error detail')[:200]}"
                        ),
                        # No ``error_class`` — see the sidechain-text row
                        # above for the rationale.
                        "details": {
                            "sidechain": True,
                            "tool": _sp_name,
                            "sidechain_failures": worker._sidechain_failures,
                        },
                    })
                if _output_locked:
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    tool_use_id = block.get("tool_use_id") or ""
                    pending = pending_tools.pop(tool_use_id, None)
                    if pending is None:
                        # Result for a skipped/internal (mcp__*) tool, or a
                        # block we never buffered — nothing to enrich.
                        continue
                    worker._send({
                        "type": MessageType.PROGRESS,
                        "task_id": task_id,
                        "event_type": "tool_run",
                        **build_tool_activity(
                            pending["name"],
                            pending["input"],
                            result_content=block.get("content"),
                            is_error=bool(block.get("is_error")),
                            tool_use_id=tool_use_id,
                        ),
                    })
            elif msg.type == "error":
                # Capture and break out of the stream loop so the retry
                # handler below can decide whether to retry or escalate.
                last_error_text = msg.data.get("error") or ""
                last_stderr_text = msg.data.get("stderr") or ""
                logger.warning(
                    "CLI stream error on attempt %d/%d for task %s: "
                    "err=%s; api_err=%s; stderr=%s",
                    attempt, max_attempts, task_id,
                    last_error_text[:200],
                    (last_api_error or "")[:200],
                    last_stderr_text[:200],
                )
                break

        # Tool calls whose ``tool_result`` never arrived (stream ended/errored
        # before the result frame) keep their already-emitted "running" start
        # row as the record of what was invoked — no flush needed (a flush
        # would duplicate the start). Just drop the buffer.
        pending_tools.clear()

        # No error stream event means the CLI finished cleanly — the
        # `assistant` may have mentioned an API error in passing (e.g.
        # quoting documentation), but the process exited 0 so the
        # session is a success. Ignore last_api_error in that case.
        if last_error_text is None:
            # AREA-1 fix 3 (verify turn-end incident 2026-07-17): a clean
            # stream end with UNRESOLVED spawn tool_use ids is the exact
            # incident shape — the model ended its turn on a live
            # workflow, and the ``--print`` process exit killed it. Log
            # loudly and stamp the count so ``handle_assign_task``
            # attaches it to TASK_COMPLETE (``pending_spawns``) for the
            # honesty check's "ended with a workflow still running"
            # poke copy.
            if spawn_tool_ids:
                worker._pending_spawns = len(spawn_tool_ids)
                logger.warning(
                    "task %s: CLI stream ended CLEANLY with %d unresolved "
                    "%s spawn(s) — the model ended its turn with a "
                    "workflow still running; under --print the process "
                    "exit kills it (verify turn-end incident 2026-07-17)",
                    task_id, len(spawn_tool_ids),
                    "/".join(sorted(set(spawn_tool_ids.values()))),
                )
            return session_id, total_cost

        # Pick the richest classification signal available, in order:
        # 1. API error text surfaced via assistant/result (specific
        #    diagnostic produced by the model/API).
        # 2. Stderr from the CLI subprocess (often contains the
        #    underlying auth/connection failure).
        # 3. The synthetic "Claude CLI exited with code N" string —
        #    last resort; matches no pattern but keeps the loop safe.
        if last_api_error:
            error_for_classify = last_api_error
        elif last_stderr_text.strip():
            error_for_classify = last_stderr_text.strip()
        else:
            error_for_classify = last_error_text

        # Classify and decide what to do.
        remedy = classify_error(error_for_classify)

        # Post a structured `error` activity so the task's feed shows
        # exactly what happened + what the system decided.
        worker._send({
            "type": MessageType.PROGRESS,
            "task_id": task_id,
            "event_type": "error",
            "content": (
                f"CLI error ({remedy.error_class.value}) on attempt "
                f"{attempt}/{max_attempts}: {last_error_text[:_ERROR_PREVIEW_LENGTH]}"
            ),
            "details": {
                "error_class": remedy.error_class.value,
                "retryable": remedy.retryable,
                "attempt": attempt,
                "max_attempts": max_attempts,
            },
        })

        # Item-6 graceful-degrade: an older container CLI may not know
        # --effort / --settings (ultracode). If the error says so, drop them
        # and retry without — a CLI flag-support gap must never block a task.
        if (current_effort or current_settings_json) and is_unknown_flag_error(
            error_for_classify
        ):
            logger.warning(
                "Container CLI rejected --effort/--settings (ultracode) for "
                "task %s; retrying without reasoning-effort / dynamic-workflow "
                "flags (update the agent image's Claude CLI to enable them).",
                task_id,
            )
            current_effort = None
            current_settings_json = None
            if not flags_degraded:
                # First degrade: the flags — not the task — caused this
                # failure, so don't let a CLI flag-support gap consume the
                # retry budget. Grant one extra attempt so the no-flag
                # retry runs even if this was the final attempt. The
                # loop-top wall-clock guard still bounds total runtime.
                flags_degraded = True
                max_attempts += 1
            # Recoverable config mismatch, not a real failure — retry
            # now without the flags, bypassing the escalate path.
            continue

        # FIX W1: infra-classed retry exhaustion → deferred-resume ladder
        # (see the ``_INFRA_DEFER_DELAYS_SECONDS`` rationale above). Each
        # rung grants ONE more attempt after a long wait; the wait itself
        # is applied through the ``backoff`` sleep below (the process
        # stays alive, the task stays in_progress — a daemon restart
        # re-dispatches the orphan, which re-enters the wait). A rung
        # that would blow the wall-clock budget escalates NOW with the
        # resume time named, mirroring the usage-limit posture.
        if (
            remedy.retryable
            and attempt >= max_attempts
            and task_mode == "execute"
            and not is_planner_consult
            and remedy.error_class in INFRA_OUTAGE_CLASSES
            and infra_defer_cycle < len(_INFRA_DEFER_DELAYS_SECONDS)
        ):
            from datetime import datetime, timedelta, timezone

            defer_secs = _INFRA_DEFER_DELAYS_SECONDS[infra_defer_cycle]
            resume_at = datetime.now(timezone.utc) + timedelta(
                seconds=defer_secs
            )
            when = resume_at.strftime("%H:%M UTC")
            if elapsed + defer_secs > _MAX_SESSION_WALLCLOCK_SECONDS:
                raise AgentErrorEscalation(
                    error_class=remedy.error_class.value,
                    original_error=last_error_text,
                    escalation_message=(
                        f"{remedy.escalation_message} A deferred "
                        f"auto-resume at {when} would exceed this task's "
                        "runtime budget — escalating now; re-dispatch "
                        "once the provider recovers."
                    ),
                    session_id=session_id,
                    total_cost=total_cost,
                )
            infra_defer_cycle += 1
            max_attempts += 1
            infra_defer_seconds = defer_secs
            worker._send({
                "type": MessageType.PROGRESS,
                "task_id": task_id,
                "event_type": "checkpoint",
                "content": (
                    f"⏸ Paused — provider outage "
                    f"({remedy.error_class.value}) outlived the quick "
                    f"retries. Auto-resuming at {when} "
                    f"(~{int(defer_secs / 60)} min; deferred retry "
                    f"{infra_defer_cycle}/"
                    f"{len(_INFRA_DEFER_DELAYS_SECONDS)})."
                ),
                "details": {
                    "error_class": remedy.error_class.value,
                    "paused": True,
                    "resume_at": resume_at.isoformat(),
                    "deferred_cycle": infra_defer_cycle,
                },
            })

        if not remedy.retryable or attempt >= max_attempts:
            # Non-retryable OR exhausted — escalate. Raise with a
            # structured message so the enclosing _handle_assign_task
            # can move the task to blocked.
            escalation_message = remedy.escalation_message
            if infra_defer_cycle:
                # W1: name the consumed ladder so the escalate_blocker /
                # MA triage knows auto-resume was already attempted.
                escalation_message += (
                    f" Auto-resume was already attempted "
                    f"{infra_defer_cycle} time(s) on the deferred ladder "
                    "before this escalation."
                )
            raise AgentErrorEscalation(
                error_class=remedy.error_class.value,
                original_error=last_error_text,
                escalation_message=escalation_message,
                session_id=session_id,
                total_cost=total_cost,
            )

        # Apply remedy and loop. Log a recovery checkpoint so the
        # activity feed tells the user what's happening.
        worker._send({
            "type": MessageType.PROGRESS,
            "task_id": task_id,
            "event_type": "checkpoint",
            "content": (
                f"Recovering from {remedy.error_class.value} "
                f"(attempt {attempt + 1}/{max_attempts}). Applying remedy: "
                f"{'fresh session; ' if remedy.reset_session else ''}"
                f"{'env=' + ','.join(remedy.env_overrides) + '; ' if remedy.env_overrides else ''}"
                f"appending guidance hint to system prompt."
            ),
            "details": {
                "error_class": remedy.error_class.value,
                "env_overrides": list(remedy.env_overrides),
                "reset_session": remedy.reset_session,
                "backoff_seconds": remedy.backoff_seconds,
            },
        })

        # Session/usage limit (#3): don't burn the retry on a fixed backoff
        # while a multi-hour window is exhausted — wait until the window
        # REOPENS (from the parsed reset_at, capped at the wall-clock budget),
        # then resume the SAME session. During a usage limit the whole account
        # is blocked, so holding this process asleep is harmless; on a daemon
        # restart the dispatcher re-dispatches the in-flight task, which simply
        # re-enters this wait. A reset beyond the budget falls through to the
        # loop-top wall-clock guard, which escalates with the reset time named.
        backoff = remedy.backoff_seconds
        if infra_defer_seconds:
            # FIX W1: a deferred-resume rung replaces the class's quick
            # backoff — the long sleep IS the remedy. Consumed once.
            backoff = infra_defer_seconds
            infra_defer_seconds = 0.0
        if (
            remedy.error_class is ErrorClass.USAGE_LIMIT_EXCEEDED
            and remedy.reset_at is not None
        ):
            from datetime import datetime, timezone

            when = remedy.reset_at.astimezone(timezone.utc).strftime("%H:%M UTC")
            secs_until_reset = (
                remedy.reset_at - datetime.now(timezone.utc)
            ).total_seconds() + 15.0  # small buffer past the reset
            # ``elapsed`` (time already spent this session, from the loop top)
            # is part of the per-task wall-clock budget — a sleep consumes it.
            # Escalate now if the wait won't leave room to actually resume.
            if elapsed + secs_until_reset > _MAX_SESSION_WALLCLOCK_SECONDS:
                # Reset is beyond this task's remaining runtime budget (e.g. a
                # weekly cap, or a long wait late in a long task) — escalate NOW
                # with the reset time rather than sleep then time out anyway.
                raise AgentErrorEscalation(
                    error_class=remedy.error_class.value,
                    original_error=last_error_text,
                    escalation_message=(
                        "Claude usage window is exhausted and won't reset until "
                        f"{when} (beyond this task's runtime budget). The task "
                        "is paused — re-dispatch it after the reset."
                    ),
                    session_id=session_id,
                    total_cost=total_cost,
                )
            if secs_until_reset > 0:
                backoff = secs_until_reset
                worker._send({
                    "type": MessageType.PROGRESS,
                    "task_id": task_id,
                    "event_type": "checkpoint",
                    "content": (
                        f"⏸ Paused — Claude usage limit reached. Resuming "
                        f"automatically at {when} (~{int(backoff / 60)} min)."
                    ),
                    "details": {
                        "error_class": remedy.error_class.value,
                        "reset_at": remedy.reset_at.isoformat(),
                        "paused": True,
                    },
                })

        if backoff > 0:
            await asyncio.sleep(backoff)

        # Fold the remedy's env into the next call. Later remedies
        # override earlier ones (e.g. repeated token-limit errors
        # keep the bumped var, not stack).
        current_env.update(remedy.env_overrides)

        # Append guidance to the system prompt. We keep previous
        # guidance (if any) so the agent sees the full history of
        # what went wrong — capped to a few entries to avoid bloat.
        #
        # P2.5-E: use a sentinel-style delimiter unlikely to
        # appear in the user's task brief (the previous Markdown
        # heading "## AUTOMATIC RECOVERY — READ THIS" could
        # collide with a brief that copy-pastes that exact
        # phrase, and rotation would corrupt the base prompt).
        # The HTML-comment form is invisible to most renderings
        # but still readable in the model's literal prompt.
        _MARKER = "\n\n<!--CBCL_RECOVERY_BLOCK_START-->\n"
        guidance_block = (
            f"{_MARKER}"
            f"## AUTOMATIC RECOVERY — READ THIS\n"
            f"Attempt {attempt} failed: {remedy.error_class.value}. "
            f"{remedy.guidance}"
        )

        if (
            len(current_system_prompt) + len(guidance_block)
            <= _MAX_SYSTEM_PROMPT_SIZE
        ):
            current_system_prompt = current_system_prompt + guidance_block
        else:
            # P2-F + P2.5-E: rotate oldest blocks out until the
            # new block fits. Previous behaviour rotated AT MOST
            # ONE block, so a prompt that hit the cap with N>=2
            # blocks would simply drop the new guidance — and on
            # the next attempt drop it again, leaving the agent
            # without the latest remedy on every single retry.
            # We now drop blocks oldest-first until the new
            # guidance fits or no blocks remain. If even an
            # empty-block prompt + guidance overflows, we drop
            # the new guidance as a last resort.
            offsets: list[int] = []
            idx = current_system_prompt.find(_MARKER)
            while idx >= 0:
                offsets.append(idx)
                idx = current_system_prompt.find(_MARKER, idx + 1)

            rotated = current_system_prompt
            rotated_count = 0
            for i in range(len(offsets)):
                next_block_idx = (
                    offsets[i + 1] if i + 1 < len(offsets) else None
                )
                if next_block_idx is None:
                    rotated = rotated[: offsets[i]]
                else:
                    # Drop everything from this block's start to
                    # the next block's start.
                    rotated = (
                        rotated[: offsets[i]]
                        + rotated[next_block_idx:]
                    )
                    # Recompute offsets after drop — easiest is
                    # to break and re-scan, but the loop math
                    # above shifts subsequent offsets. Simpler:
                    # break and rebuild offsets each pass.
                rotated_count += 1
                if (
                    len(rotated) + len(guidance_block)
                    <= _MAX_SYSTEM_PROMPT_SIZE
                ):
                    break
                # Re-scan offsets relative to rotated for next pass.
                offsets = []
                idx = rotated.find(_MARKER)
                while idx >= 0:
                    offsets.append(idx)
                    idx = rotated.find(_MARKER, idx + 1)
                if not offsets:
                    break

            if (
                len(rotated) + len(guidance_block)
                <= _MAX_SYSTEM_PROMPT_SIZE
            ):
                logger.warning(
                    "Prompt size cap hit; rotated %d guidance "
                    "block(s) to keep attempt %d's remedy",
                    rotated_count, attempt,
                )
                current_system_prompt = rotated + guidance_block
            else:
                logger.warning(
                    "Prompt size cap hit and rotation could not free "
                    "enough room (%d chars after dropping %d blocks); "
                    "dropping new guidance for attempt %d",
                    len(rotated), rotated_count, attempt,
                )

        if remedy.reset_session:
            current_resume = None  # start a fresh session
        else:
            # Resume the session the CLI just used (if any) so the
            # next attempt sees the partial work — tool calls made,
            # files written, observations recorded — instead of
            # starting a blank conversation. Without this, the
            # retry starts a new session and the agent has no
            # visibility into what it already did on disk, forcing
            # it to redo discovery work and risking duplicate
            # writes / divergent output.
            if session_id:
                current_resume = session_id

    # Unreachable by construction: the loop body returns on success
    # and raises AgentErrorEscalation on the final failure. This
    # raise only fires if someone refactors the loop incorrectly —
    # it fails loud rather than silently returning None, None.
    raise RuntimeError(
        "agent_worker retry loop exited without return or raise — "
        "this indicates a logic bug. Please file it."
    )
