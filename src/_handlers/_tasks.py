"""Task-routing helper bodies (split from handlers.py).

The communicator receives ``task_updated`` (state delta carrying the
full task row) and ``task_moved`` (status transition only) from the
backend. Both must:

1. Update per-agent Redis queues (assign / unassign / clear).
2. Force-release any worker currently working on a task that just
   reached a terminal state (done / archived / done-via-review).
3. Trigger Manager-Assistant pickup for orphan / unassigned tasks.

Splitting these out of the 1900-LOC ``handlers.py`` shrinks the
file substantially while keeping the closure registrar intact —
the closures just delegate to the helpers below with their
captured deps as explicit args.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def decide_ma_review_completion(
    task_status: str,
    review_skipped: bool,
    ma_is_reviewer: bool = False,
) -> str:
    """ADD-A5: decide what to do when the Manager Assistant finishes a
    review-mode assignment.

    The MA is the "benefit-of-the-doubt" reviewer: a clean session end with
    the task still in ``review`` is treated as APPROVE. But a session that
    was SKIPPED (no deliverables read, no verdict posted) must NOT be
    auto-approved, or unreviewed work ships to ``done``.

    A skip can be transient (state changed since dispatch) OR structural:
    the worker's authorization gate skips when the MA is neither the
    ``assigned_agent`` nor the ``reviewer`` (a task that genuinely has no
    designated reviewer). A naive "always re-dispatch" on skip would loop
    forever on the structural case (re-dispatch → re-skip). ``ma_is_reviewer``
    breaks the loop:

    - ``"noop"``               — task already left review; or skipped while
      the MA WAS already the reviewer (re-dispatch would just re-skip — leave
      it for the reconciler/sweeper instead of spinning).
    - ``"authorize_requeue"``  — skipped while the MA was NOT the reviewer:
      designate the MA as reviewer (so it's authorized) and retry ONCE; the
      next session is authorized and won't re-skip.
    - ``"approve"``            — the MA actually reviewed and left it in review.
    """
    if task_status != "review":
        return "noop"
    if review_skipped:
        return "noop" if ma_is_reviewer else "authorize_requeue"
    return "approve"


async def route_task_updated(
    msg: dict,
    *,
    queue_manager,
    dispatcher,
    supervisor,
    router,
    platform_url: str = "",
    office_id: str = "",
    security_token: str = "",
    config_store=None,
) -> None:
    """React to task updates (assignment changes, status changes).

    Event-driven queue updates:
    - Unassigned review/blocked → Manager Assistant queue
    - Assigned to agent → that agent's queue
    - Done/archived → remove from all queues
    """
    task_data = msg.get("task_data", msg)
    task_id = task_data.get("task_id") or msg.get("task_id", "")
    status = task_data.get("status", "")
    agent = task_data.get("assigned_agent") or ""
    old_agent = msg.get("old_assigned_agent", "")

    if status in ("done", "archived"):
        # Terminal — clean every queue and release any worker.
        await queue_manager.remove_task_from_all(task_id)
        all_statuses = supervisor.get_all_statuses()
        for a_name, a_info in all_statuses.items():
            if a_info.get("current_task") == task_id:
                logger.info(
                    "Task %s moved to %s — releasing agent '%s'",
                    task_id[:8], status, a_name,
                )
                await queue_manager.clear_active(a_name)
                try:
                    await supervisor._kill_process(a_name)
                except Exception:
                    pass
                await router.publish_event({
                    "type": "agent_status_changed",
                    "agent_name": a_name,
                    "display_name": a_name,
                    "status": "idle",
                    "current_task": None,
                    "current_task_title": None,
                })
                dispatcher.wake()
        return

    # Blocked tasks always route to the Manager Assistant, regardless
    # of ``assigned_agent``. Force the override BEFORE the executor-
    # branch below would otherwise queue the task on the executor's
    # queue. The executor's assignment is preserved on the task row;
    # only the dispatch routing is overridden. Mirrors the rule in
    # ``AgentQueueManager.full_sync`` / ``reconcile`` /
    # ``TaskDispatcher.add_task`` — keeping all four call sites
    # aligned is what makes the rule "end-to-end". Without this, a
    # ``task_updated`` event on a blocked task (e.g. priority change,
    # reassignment) would enqueue the task on the executor's queue
    # and the dispatcher's defensive guard would only catch it on
    # spawn, leaving a stale entry until the next 60s reconcile.
    if status == "blocked" and agent and agent != "manager-assistant":
        logger.info(
            "task_updated %s blocked — overriding assigned_agent '%s' "
            "→ MA (only the Manager Assistant triages blocked tasks)",
            task_id[:8], agent,
        )
        agent = ""  # Fall through to the "unassigned blocked" branch.

    # Avoid re-queueing what MA is already on.
    ma_active = await queue_manager.get_active("manager-assistant")
    ma_active_task = ma_active.get("task_id", "") if ma_active else ""

    if status == "review":
        reviewer = task_data.get("reviewer") or ""
        # ADD-A4: a deactivated/deleted reviewer can't be dispatched (the
        # dispatch loop only visits active in-config agents), so treat it as
        # "no reviewer" and let the Manager Assistant pick the review up
        # instead of the task starving in the dead reviewer's queue.
        if (
            reviewer
            and config_store is not None
            and not config_store.is_agent_dispatchable(reviewer)
        ):
            logger.warning(
                "Review task %s reviewer '%s' inactive/missing — falling "
                "back to Manager Assistant",
                task_id[:8], reviewer,
            )
            # M2: persist reviewer=manager-assistant so the MA's FIRST
            # dispatch is authorized (the worker re-fetches and only reviews
            # when it's the assigned_agent or reviewer). Without this the MA
            # skips (unauthorized) and recovery has to re-dispatch.
            if platform_url and office_id:
                from src.backend_client import designate_ma_reviewer
                await designate_ma_reviewer(
                    platform_url, office_id, task_id, security_token,
                )
            reviewer = ""
            agent = ""  # force the MA fallback branch below
        if reviewer:
            # Designated reviewer overrides assigned_agent (which stays
            # as the executor for audit-trail).
            if supervisor.is_agent_busy(reviewer):
                active = await queue_manager.get_active(reviewer)
                if active and active.get("task_id") == task_id:
                    logger.debug(
                        "Skipping re-queue: reviewer '%s' already on %s",
                        reviewer, task_id[:8],
                    )
                    return
            await queue_manager.add_task(reviewer, {
                "task_id": task_id,
                "readable_id": task_data.get("readable_id", ""),
                "reviewer": reviewer,
                "status": "review",
                "priority": "urgent",
            })
            await dispatcher.dispatch_agent(reviewer)
            logger.info(
                "Review task %s -> reviewer '%s' queue",
                task_id[:8], reviewer,
            )
            return
        elif not agent:
            if ma_active_task == task_id:
                logger.debug(
                    "Skipping re-queue: MA already working on %s",
                    task_id[:8],
                )
            else:
                await queue_manager.add_task("manager-assistant", {
                    "task_id": task_id,
                    "readable_id": task_data.get("readable_id", ""),
                    "status": "review",
                    "priority": "urgent",
                })
                await dispatcher.dispatch_agent("manager-assistant")
                logger.info(
                    "Review task %s unassigned -> MA queue (no reviewer)",
                    task_id[:8],
                )
            return

    elif not agent and status in ("blocked", "ready", "in_progress"):
        if ma_active_task != task_id:
            # Same pending-action-request guard as the worker-driven
            # routing path in ``handlers.py:_on_agent_event`` and the
            # Manager-driven path below in ``route_task_moved``.
            # Without this an orphan blocked task with a pending
            # request would still get re-enqueued every time the
            # backend fires a ``task_updated`` event for it (e.g.
            # the Manager unassigns it), reopening the spam window
            # that the dedup at create-time only partly prevents.
            if status == "blocked" and platform_url and office_id:
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
                        "Orphan task %s blocked — pending action "
                        "request exists, skipping MA queue routing",
                        task_id[:8],
                    )
                    return
            await queue_manager.add_task("manager-assistant", {
                "task_id": task_id,
                "readable_id": task_data.get("readable_id", ""),
                "status": status,
                "priority": "high" if status == "blocked" else "medium",
            })
            await dispatcher.dispatch_agent("manager-assistant")
            logger.info(
                "Orphan task %s (status=%s) -> MA queue",
                task_id[:8], status,
            )

    elif agent and agent != "manager":
        if supervisor.is_agent_busy(agent):
            active = await queue_manager.get_active(agent)
            if active and active.get("task_id") == task_id:
                logger.debug(
                    "Skipping queue for %s — agent '%s' already on it",
                    task_id[:8], agent,
                )
                return

        if old_agent and old_agent != agent:
            await queue_manager.remove_task(old_agent, task_id)

        if not old_agent:
            await queue_manager.remove_task("manager-assistant", task_id)

        queue_task = {
            "task_id": task_id,
            "readable_id": task_data.get("readable_id", ""),
            "assigned_agent": agent,
            "priority": task_data.get("priority", "high"),
            "status": status,
            "scope_id": task_data.get("scope_id"),
            "scope_state": task_data.get("scope_state"),
            "scope_readable_id": task_data.get("scope_readable_id"),
        }
        await queue_manager.add_task(agent, queue_task)
        await dispatcher.dispatch_agent(agent)
        logger.info("Task %s assigned to %s -> queue", task_id[:8], agent)


async def route_task_moved(
    msg: dict,
    *,
    queue_manager,
    dispatcher,
    supervisor,
    router,
    platform_url: str = "",
    office_id: str = "",
    security_token: str = "",
    config_store=None,
) -> None:
    """React to task status changes."""
    task_id = msg.get("task_id", "")
    new_status = msg.get("new_status", "")
    agent = msg.get("assigned_agent", "")

    if new_status in ("done", "archived"):
        await queue_manager.remove_task_from_all(task_id)
        all_statuses = supervisor.get_all_statuses()
        for a_name, a_info in all_statuses.items():
            if a_info.get("current_task") == task_id:
                logger.info(
                    "Task %s moved to %s — releasing agent '%s' (task_moved)",
                    task_id[:8], new_status, a_name,
                )
                await queue_manager.clear_active(a_name)
                try:
                    await supervisor._kill_process(a_name)
                except Exception:
                    pass
                await router.publish_event({
                    "type": "agent_status_changed",
                    "agent_name": a_name,
                    "display_name": a_name,
                    "status": "idle",
                    "current_task": None,
                    "current_task_title": None,
                })
                dispatcher.wake()

    elif new_status == "review":
        reviewer = msg.get("reviewer") or ""
        # ADD-A4: deactivated/deleted reviewer → fall back to the MA so the
        # review doesn't starve in a queue the dispatch loop never visits.
        if (
            reviewer
            and config_store is not None
            and not config_store.is_agent_dispatchable(reviewer)
        ):
            logger.warning(
                "Review task %s reviewer '%s' inactive/missing — falling "
                "back to Manager Assistant",
                task_id[:8], reviewer,
            )
            # M2: persist reviewer=manager-assistant so the MA's FIRST
            # dispatch is authorized (see route_task_updated above).
            if platform_url and office_id:
                from src.backend_client import designate_ma_reviewer
                await designate_ma_reviewer(
                    platform_url, office_id, task_id, security_token,
                )
            reviewer = ""
            agent = ""  # force the MA fallback branch below
        if reviewer:
            await queue_manager.add_task(reviewer, {
                "task_id": task_id,
                "readable_id": msg.get("readable_id", ""),
                "reviewer": reviewer,
                "status": "review",
                "priority": "urgent",
            })
            await dispatcher.dispatch_agent(reviewer)
        elif not agent:
            await queue_manager.add_task("manager-assistant", {
                "task_id": task_id,
                "status": "review",
                "priority": "urgent",
            })
            await dispatcher.dispatch_agent("manager-assistant")

        # FORCE-KILL the executor if still running. The executor
        # MUST stop after submitting for review — the STOP signal in
        # the tool response is advisory, Claude can ignore it. This
        # is the enforcement mechanism.
        executor = agent or ""
        if (
            executor
            and executor != reviewer
            and supervisor.is_agent_busy(executor)
        ):
            active = await queue_manager.get_active(executor)
            if active and active.get("task_id") == task_id:
                logger.info(
                    "Force-killing executor '%s' — task %s moved to review",
                    executor, task_id[:8],
                )
                try:
                    await supervisor._kill_process(executor)
                except Exception:
                    pass
                await queue_manager.clear_active(executor)
                dispatcher.wake()

    elif new_status == "blocked":
        # Step 1: FORCE-KILL the assigned executor if it's still busy
        # on this task. Without this, a Manager-driven move to
        # "blocked" only flips the DB status — the agent subprocess
        # keeps running and producing artefacts as if it were
        # in_progress. Mirrors the review path above.
        #
        # Only kill if the agent is busy AND its active task is the
        # one being blocked; otherwise the agent has moved on to
        # something else (e.g. self-blocked then idled) and killing
        # would abort unrelated work.
        if agent and supervisor.is_agent_busy(agent):
            active = await queue_manager.get_active(agent)
            if active and active.get("task_id") == task_id:
                logger.info(
                    "Force-killing executor '%s' — task %s moved to "
                    "blocked",
                    agent, task_id[:8],
                )
                try:
                    await supervisor._kill_process(agent)
                except Exception:
                    # Best-effort. A kill failure leaves the agent
                    # running on a now-blocked task; the next
                    # status_update from the agent will surface the
                    # inconsistency in logs.
                    logger.exception(
                        "Failed to kill executor '%s' on blocked "
                        "transition", agent,
                    )
                await queue_manager.clear_active(agent)
                dispatcher.wake()

        # Step 2: queue Manager Assistant for triage UNLESS MA is
        # already actively working on this exact task. Guards
        # against a re-bounce loop where MA blocks a task it's
        # holding (e.g. while waiting on an action_request) and
        # we'd otherwise re-enqueue the same task to MA on every
        # block.
        #
        # The contract is: every blocked task gets MA attention,
        # regardless of who blocked it.
        #
        # MA's CLAUDE.md instructs it to:
        #   1. Read the latest activity entries to understand WHY
        #      the worker (or Manager) blocked the task.
        #   2. Decide the next step — answer a worker question
        #      (`add_activity` with event_type="answer"), propose
        #      an action_request to the user, or create a helper
        #      task with `depends_on=[<blocked_task_readable_id>]`
        #      so the blocked task auto-promotes to ready once the
        #      helper finishes.
        #
        # Pre-fix this branch only queued MA when ``agent`` was
        # empty — worker-self-blocked tasks sat in the Blocked
        # column with nobody triaging them.
        ma_active = await queue_manager.get_active("manager-assistant")
        if not ma_active or ma_active.get("task_id") != task_id:
            # Same pending-action-request guard as the worker-driven
            # routing path in ``handlers.py:_on_agent_event``. Without
            # this, a Manager-driven move to "blocked" would re-flood
            # the MA queue (and the inbox) on every block even when
            # the user already has a pending decision on the task.
            has_pending = False
            if platform_url and office_id:
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
                    "skipping MA queue routing (route_task_moved)",
                    task_id[:8],
                )
            else:
                await queue_manager.add_task("manager-assistant", {
                    "task_id": task_id,
                    "readable_id": msg.get("readable_id", ""),
                    "status": "blocked",
                    "priority": "high",
                })
                await dispatcher.dispatch_agent("manager-assistant")

    elif new_status == "ready":
        dispatcher.wake()
