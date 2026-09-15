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
from inspect import iscoroutinefunction

logger = logging.getLogger(__name__)


async def _handoff_is_current(
    task_id: str, status: str, platform_url: str, office_id: str, security_token: str
) -> bool:
    if not platform_url or not office_id:
        return True
    import httpx

    from src.backend_client import auth_headers

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{platform_url}/api/offices/{office_id}/tasks/{task_id}",
                headers=auth_headers(security_token),
            )
        return response.status_code == 200 and response.json().get("status") == status
    except Exception:
        logger.exception("Cannot verify current phase for task %s", task_id)
        return False


async def route_task_kill(
    msg: dict,
    *,
    queue_manager,
    dispatcher,
    supervisor,
    router=None,
    terminal_event: bool = False,
    script_runner=None,
) -> None:
    """Cancel only matching task executions, preserving unrelated successors."""
    task_id = str(msg.get("task_id") or "")
    if not task_id:
        return
    if not terminal_event and not msg.get("stop_request_id"):
        logger.warning("Ignoring uncorrelated legacy task_kill for %s", task_id)
        return
    supervisor.suppress_task(task_id)
    if script_runner is not None:
        script_runner.suppress_task(task_id)
    stopped_agents = []
    failed_agents = []
    agent_name = str(msg.get("agent_name") or "")
    all_agents = bool(msg.get("all_agents")) or not agent_name
    if all_agents:
        await queue_manager.remove_task_from_all(task_id)
        names = [
            name for name, info in supervisor.get_all_statuses().items()
            if info.get("current_task") == task_id
        ]
    else:
        names = [agent_name]
        await queue_manager.remove_task(agent_name, task_id)
    for name in names:
        try:
            stopped = await supervisor.stop_task(name, task_id)
            if stopped:
                active = await queue_manager.get_active(name)
                if active and active.get("task_id") == task_id:
                    await queue_manager.clear_active(name, task_id)
                stopped_agents.append(name)
                if router is not None:
                    async with supervisor._get_lock(name):
                        if not supervisor.is_agent_busy(name):
                            await router.publish_event({
                                "type": "agent_status_changed",
                                "agent_name": name,
                                "display_name": name,
                                "status": "idle",
                                "current_task": None,
                                "current_task_title": None,
                            })
        except Exception:
            failed_agents.append(name)
            logger.exception(
                "Execution stop is unconfirmed for task %s / agent %s; "
                "retry Stop after restoring daemon/container access",
                task_id, name,
            )
    retained_stop = getattr(supervisor, "stop_retained_task_executions", None)
    if iscoroutinefunction(retained_stop):
        try:
            if await retained_stop(task_id):
                stopped_agents.append("isolated-executions")
        except Exception:
            failed_agents.append("isolated-executions")
            logger.exception("Retained isolated execution cleanup is unconfirmed for task %s", task_id)
    if router is not None and msg.get("stop_request_id"):
        errors = [
            {"agent_name": name, "detail": "Container cleanup is unconfirmed"}
            for name in failed_agents
        ]
        if script_runner is not None:
            try:
                scripts_pending = script_runner.has_active_scripts(task_id)
            except Exception:
                scripts_pending = True
                logger.exception("Cannot confirm linked script state for %s", task_id)
            if scripts_pending:
                errors.append({
                    "agent_name": "office-scripts",
                    "detail": "Linked Office scripts are running or uncertain; reconcile using script controls or an operator",
                })
        await router.publish_event({
            "type": "task_stop_result",
            "task_id": task_id,
            "stop_request_id": msg["stop_request_id"],
            "status": (
                "unconfirmed" if errors else
                "stopped" if stopped_agents else "not_running"
            ),
            "stopped_agents": stopped_agents,
            "errors": errors,
        })
    dispatcher.wake()


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
    script_runner=None,
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
        await route_task_kill(
            {**msg, "task_id": task_id, "all_agents": True},
            queue_manager=queue_manager,
            dispatcher=dispatcher,
            supervisor=supervisor,
            router=router,
            terminal_event=True,
            script_runner=script_runner,
        )
        return

    if agent and status in ("review", "blocked"):
        execution_marker = supervisor.get_task_execution_marker(agent, task_id)
        if not await _handoff_is_current(
            task_id, status, platform_url, office_id, security_token
        ):
            return
        if execution_marker and (status != "review" or agent != task_data.get("reviewer")):
            try:
                if await supervisor.stop_task(
                    agent, task_id, expected_mode="execute",
                    expected_execution_marker=execution_marker,
                ):
                    await queue_manager.clear_active(agent, task_id)
            except Exception:
                logger.exception("Task handoff cleanup remains unconfirmed for %s", task_id)
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
        from src.review_routing import default_reviewer

        reviewer = task_data.get("reviewer") or default_reviewer(task_data)
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
                "back to an independent default reviewer",
                task_id[:8], reviewer,
            )
            reviewer = default_reviewer(task_data)
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
    script_runner=None,
) -> None:
    """React to task status changes."""
    task_id = msg.get("task_id", "")
    new_status = msg.get("new_status", "")
    agent = msg.get("assigned_agent", "")
    execution_marker = supervisor.get_task_execution_marker(agent, task_id) if agent else None

    if new_status in ("review", "blocked") and not await _handoff_is_current(
        task_id, new_status, platform_url, office_id, security_token
    ):
        return

    if new_status in ("done", "archived"):
        await route_task_kill(
            {**msg, "all_agents": True},
            queue_manager=queue_manager,
            dispatcher=dispatcher,
            supervisor=supervisor,
            router=router,
            terminal_event=True,
            script_runner=script_runner,
        )

    elif new_status == "review":
        from src.review_routing import default_reviewer

        reviewer = msg.get("reviewer") or default_reviewer(msg)
        if agent and agent != reviewer and execution_marker:
            try:
                if await supervisor.stop_task(
                    agent, task_id, expected_mode="execute",
                    expected_execution_marker=execution_marker,
                ):
                    await queue_manager.clear_active(agent, task_id)
                    dispatcher.wake()
            except Exception:
                logger.exception(
                    "Review handoff withheld: executor cleanup is unconfirmed for %s",
                    task_id,
                )
                return
        # ADD-A4: deactivated/deleted reviewer → fall back to the MA so the
        # review doesn't starve in a queue the dispatch loop never visits.
        if (
            reviewer
            and config_store is not None
            and not config_store.is_agent_dispatchable(reviewer)
        ):
            logger.warning(
                "Review task %s reviewer '%s' inactive/missing — falling "
                "back to an independent default reviewer",
                task_id[:8], reviewer,
            )
            reviewer = default_reviewer(msg)
        if reviewer:
            await queue_manager.add_task(reviewer, {
                "task_id": task_id,
                "readable_id": msg.get("readable_id", ""),
                "reviewer": reviewer,
                "status": "review",
                "priority": "urgent",
            })
            await dispatcher.dispatch_agent(reviewer)
    elif new_status == "blocked":
        if agent and execution_marker:
            try:
                if await supervisor.stop_task(
                    agent, task_id, expected_mode="execute",
                    expected_execution_marker=execution_marker,
                ):
                    await queue_manager.clear_active(agent, task_id)
                    dispatcher.wake()
            except Exception:
                logger.exception(
                    "Blocked handoff withheld: cleanup is unconfirmed for %s", task_id
                )
                return

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
