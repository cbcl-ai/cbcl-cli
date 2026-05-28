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
import os
import logging
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.script_sync import ScriptSyncer
from src.config_sync.sync_service import ConfigStore
from src.config_sync.workspace_setup import WorkspaceSetup
from src.dispatch import (
    handle_script_execute,
    handle_script_secret_update,
    handle_script_variable_binding_set,
    handle_skill_secret_update,
)
from src._handlers._mcp import run_mcp_add, run_mcp_remove
from src._handlers._mcp_listing import MCPRefreshState, refresh_mcp_list
from src._handlers._oauth import (
    run_cli_auth,
    run_mcp_authenticate,
    run_mcp_oauth_callback,
    run_mcp_token_ready,
    run_mcp_write_token,
)
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
from src.recovery import mark_stale_script_executions
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

# After this many rework cycles, a reviewer session that completes without
# explicitly moving the task auto-approves (circuit breaker). Below this,
# ambiguous completion returns the task for another rework cycle.
# Matches the Manager system prompt ("Maximum 2 rework cycles").
MAX_REWORK_CYCLES = int(os.environ.get("CUBICLE_MAX_REWORK_CYCLES", "2"))


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
    deadline = asyncio.get_event_loop().time() + 60.0
    while asyncio.get_event_loop().time() < deadline:
        if ws_client is None or getattr(ws_client, "connected", False):
            break
        await asyncio.sleep(0.5)
    else:
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
            # ``mark_stale_script_executions`` earlier in office
            # init has already flipped those to ``failed`` on
            # disk, so by the time we get here every row that
            # WAS hung is now terminal.
            if status not in ("completed", "failed"):
                continue
            total_attempted += 1
            payload = {
                "script_name": script_dir.name,
                "execution_id": run_dir.name,
                "status": status,
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
                    "agents": agents,
                    "workstreams": workstreams,
                    "scripts": [],
                }
            }
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

    # 4. Startup cleanup
    orphaned = script_runner.cleanup_orphaned_run_files()
    if orphaned:
        logger.info("Cleaned up %d orphaned _run.py file(s)", orphaned)

    stale = mark_stale_script_executions(office.workspace_path)
    if stale:
        logger.info("Marked %d stale script execution(s) as failed", stale)

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
        # Strong-reference the task so Python's GC doesn't collect
        # it mid-execution (per asyncio docs: "Save a reference to
        # the result of this function, to avoid a task disappearing
        # mid-execution"). _BACKGROUND_TASKS is module-level + the
        # done callback removes the entry on completion so we don't
        # leak references for the life of the daemon.
        _flush_task = asyncio.create_task(_flush_orphan_outboxes())
        _BACKGROUND_TASKS.add(_flush_task)
        _flush_task.add_done_callback(_BACKGROUND_TASKS.discard)

    # 7. Create AgentQueueManager (per-agent queues)
    queue_manager = AgentQueueManager(redis_client, office.id)

    # 8. Forward-declare dispatcher so event handler can reference it
    dispatcher = None  # Set after creation

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

            if event_type == "task_complete":
                task_id = event.get("task_id", "")
                new_status = event.get("status", "review")
                is_review_completion = event.get("is_review_completion", False)

                # Clear active task in queue manager.
                if dispatcher is not None:
                    await dispatcher.on_agent_complete(agent_name)

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
                    import httpx
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
                                    logger.info("Moved task %s to %s — routing", task_id[:8], new_status)

                                    # Fetch task to get reviewer + readable_id.
                                    from src.backend_client import auth_headers as _auth_headers
                                    task_resp = await client.get(
                                        f"{platform_url}/api/offices/{office.id}/tasks/{task_id}",
                                        headers=_auth_headers(security_token),
                                    )
                                    task_info = task_resp.json() if task_resp.status_code == 200 else {}
                                    reviewer = task_info.get("reviewer") or ""
                                    readable_id = task_info.get("readable_id") or task_id[:8]

                                    if new_status == "review":
                                        if reviewer:
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
                                            office_id=str(office.id),
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

                                if task_status == "review":
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
                                    )
                                else:
                                    logger.info("MA completed review of %s (already %s)", readable_id, task_status)
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
                                elif designated == agent_name and task_status == "review":
                                    # Reviewer completed WITHOUT moving task. Decision:
                                    # - rework_count < MAX_REWORK_CYCLES → return for rework
                                    # - rework_count >= MAX_REWORK_CYCLES → auto-approve (circuit breaker)
                                    rework_count = int(task_info.get("rework_count") or 0)
                                    if rework_count >= MAX_REWORK_CYCLES:
                                        logger.info(
                                            "Reviewer %s completed task %s, rework_count=%d (>=%d) — auto-approving (circuit breaker)",
                                            agent_name, readable_id, rework_count, MAX_REWORK_CYCLES,
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
                                            )
                                        except Exception:
                                            logger.warning("Auto-approve failed for %s", readable_id)
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
                                    # Non-designated reviewer (old flow): log, unassign, MA.
                                    await router.publish_event({
                                        "type": "task_activity",
                                        "task_id": task_id,
                                        "event_type": "checkpoint",
                                        "actor": agent_name,
                                        "content": event.get("comment", "Review complete."),
                                        "token_cost": event.get("token_cost", 0),
                                    })
                                    await client.post(
                                        f"{platform_url}/api/offices/{office.id}/tool-call",
                                        json={"action": "update_task", "params": {
                                            "task_id": task_id,
                                            "assigned_agent": "",
                                        }},
                                    )
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
                await router.publish_event({
                    "type": "task_activity",
                    "task_id": event.get("task_id", ""),
                    "event_type": event.get("event_type", "checkpoint"),
                    "actor": agent_name,
                    "content": event.get("content", ""),
                    "details": details,
                    "token_cost": event.get("token_cost"),
                })
            elif event_type == "error":
                is_fatal = event.get("fatal", False)
                task_id = event.get("task_id", "")
                logger.warning("Worker %s error (fatal=%s): %s", agent_name, is_fatal, event.get("message", ""))
                if is_fatal and dispatcher is not None:
                    await queue_manager.clear_active(agent_name)
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
                                elif task_status == "review" and task_reviewer:
                                    # Reviewer crashed during review — re-queue to
                                    # reviewer for another attempt. Do NOT move to
                                    # Ready (that would lose the review verdict if
                                    # it was already posted).
                                    await queue_manager.add_task(task_reviewer, {
                                        "task_id": task_id,
                                        "readable_id": task_info.get("readable_id", ""),
                                        "reviewer": task_reviewer,
                                        "status": "review",
                                        "priority": "urgent",
                                    })
                                    if dispatcher is not None:
                                        await dispatcher.dispatch_agent(task_reviewer)
                                    logger.info("Reviewer %s crashed on %s — re-queued to reviewer", agent_name, task_id[:8])
                                else:
                                    # Executor crashed or no reviewer — move to Ready.
                                    await client.post(
                                        f"{platform_url}/api/offices/{office.id}/tool-call",
                                        json={"action": "move_task", "params": {
                                            "task_id": task_id,
                                            "new_status": "ready",
                                            "actor": "manager-assistant",
                                            "comment": f"Agent {agent_name} crashed — auto-recovering task.",
                                        }},
                                    )
                                    logger.info("Recovered crashed task %s back to ready", task_id[:8])
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
    # Strong-reference in ``_BACKGROUND_TASKS`` or Python's GC can
    # collect the task mid-sleep and silently drop the backfill.
    _backfill_task = asyncio.create_task(
        _run_history_backfill(
            workspace_path=_history_backfill_workspace,
            router=router,
            office_id=str(office.id),
        ),
    )
    _BACKGROUND_TASKS.add(_backfill_task)
    _backfill_task.add_done_callback(_BACKGROUND_TASKS.discard)

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
    supervisor.set_tool_proxy(proxy_url, tool_proxy.token)
    logger.info("Tool proxy server started for office %s on port %d", office.id, actual_port)

    # 10c. Register filesystem handler for backend file operation requests
    from src.fs_handler import FsHandler

    fs_handler = FsHandler(office.workspace_path)

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

    # Mutable ref for watchdog access in handlers
    _watchdog_ref: list = []

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
    )

    # 14. Create TaskWatchdog (simplified — no review/blocked handling)
    from src.watchdog import TaskWatchdog, HttpBoardClient

    board_client = HttpBoardClient(platform_url, office.id)

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
        await script_syncer.sync_from_config(msg)
        claude_md_writer.sync_all(msg.get("config", {}))
        if workspace_setup:
            workspace_setup.sync_agent_workspaces(msg.get("config", {}).get("agents", []))
            workspace_setup.sync_workstream_outputs(
                msg.get("config", {}).get("workstreams", [])
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
            "workstream_name": msg.get("workstream_name", ""),
            "workstream_short_code": msg.get("workstream_short_code", ""),
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

    async def _handle_mcp_oauth_callback(msg: dict) -> None:
        """Start a localhost callback server for MCP OAuth connect flow.

        P3-G: full body lives in ``src._handlers._oauth``.
        """
        await run_mcp_oauth_callback(
            msg,
            container_name=container_name,
            office_id=str(office.id),
            redis_client=redis_client,
            refresh_mcp_list=_refresh_mcp_list,
        )

    # P3-G: refresh + parse helpers live in ``src._handlers._mcp_listing``.
    # ``_mcp_refresh_state`` is a small dataclass that the OAuth helpers
    # mutate when they need to bypass the 5-s debounce.
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

    # Initial MCP list cache on startup. Strong-reference in
    # ``_BACKGROUND_TASKS`` — bare ``ensure_future`` was getting
    # GC'd before the populate completed, leaving the cache empty
    # until the first user-triggered refresh. ``get_running_loop``
    # also fails fast in test environments that build a router
    # without an event loop, so we skip the auto-kick there; the
    # first user-triggered refresh still warms the cache.
    try:
        _mcp_init_loop = asyncio.get_running_loop()
    except RuntimeError:
        _mcp_init_loop = None
    if _mcp_init_loop is not None:
        _mcp_init_task = _mcp_init_loop.create_task(_refresh_mcp_list())
        _BACKGROUND_TASKS.add(_mcp_init_task)
        _mcp_init_task.add_done_callback(_BACKGROUND_TASKS.discard)

    router.on("chat_message", mgr.handle_chat_message)
    router.on("switch_context", mgr.handle_switch_context)
    router.on("cancel_turn", mgr.cancel_current_turn)
    router.on("scope_completed", mgr.ingest_scope_completed)
    router.on(
        "action_request_decided",
        mgr.ingest_action_request_decided,
    )
    router.on(
        "action_request_auto_decide",
        mgr.ingest_action_request_auto_decide,
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

    async def _handle_mcp_authenticate(msg: dict) -> None:
        """P3-G: body in ``src._handlers._oauth``."""
        await run_mcp_authenticate(
            msg,
            container_name=container_name,
            router=router,
            refresh_mcp_list=_refresh_mcp_list,
        )

    router.on("task_kill", _handle_task_kill)
    router.on("mcp_list", _handle_mcp_list)
    router.on("mcp_add", _handle_mcp_add)
    router.on("mcp_remove", _handle_mcp_remove)
    router.on("mcp_authenticate", _handle_mcp_authenticate)
    router.on("mcp_oauth_callback", _handle_mcp_oauth_callback)
    async def _handle_mcp_token_ready(msg: dict) -> None:
        """P3-G: body in ``src._handlers._oauth``."""
        await run_mcp_token_ready(
            msg,
            container_name=container_name,
            office_id=str(office.id),
            redis_client=redis_client,
            refresh_mcp_list=_refresh_mcp_list,
        )

    router.on("generate_office_config", _handle_generate_office_config)
    router.on("improve_office_config", _handle_improve_office_config)
    router.on("analyze_office_description", _handle_analyze_office_description)
    router.on("mcp_token_ready", _handle_mcp_token_ready)

    async def _handle_cli_auth(msg: dict) -> None:
        """P3-G: body in ``src._handlers._oauth``."""
        await run_cli_auth(
            msg,
            router=router,
            container_name=container_name,
            refresh_mcp_list=_refresh_mcp_list,
        )

    router.on("mcp_cli_auth", _handle_cli_auth)

    async def _handle_mcp_write_token(msg: dict) -> None:
        """P3-G: body in ``src._handlers._oauth`` (the helper catches
        and logs its own exceptions)."""
        await run_mcp_write_token(
            msg,
            container_name=container_name,
            mcp_refresh_state=_mcp_refresh_state,
            refresh_mcp_list=_refresh_mcp_list,
        )

    router.on("mcp_write_token", _handle_mcp_write_token)
