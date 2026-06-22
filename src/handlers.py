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
    handle_script_execute,
    handle_script_secret_update,
    handle_script_variable_binding_set,
    handle_skill_secret_update,
)
from src._handlers._mcp import run_mcp_add, run_mcp_remove
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
# roadmap/general_chat defaults — and applies the verify-silence rule
# (a killed backend-fired verify must NOT poke the Manager; the
# stuck-verifying sweeper owns recovery). Synthetic ids are
# uuid-unique, so a flat module dict is safe across offices. Entries
# are popped on EVERY planner exit path (clean done, worker-emitted
# error, kill); a daemon restart clears it (the consult dies with the
# daemon anyway).
_planner_consults: dict[str, dict] = {}


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

            if event_type == "task_complete":
                task_id = event.get("task_id", "")
                new_status = event.get("status", "review")
                is_review_completion = event.get("is_review_completion", False)

                # Clear active task in queue manager.
                if dispatcher is not None:
                    await dispatcher.on_agent_complete(agent_name)

                # Planner consult completion (execution_improvements_v1):
                # synthetic, non-board assignment. There is no task to move
                # — poke the Manager so it acts on the new plan, then mark
                # the planner idle. Skip the entire move/route flow.
                if event.get("planner_consult"):
                    # Round-2 LOW: prune the spawn-time consult stash on
                    # the clean-completion exit path.
                    _planner_consults.pop(task_id, None)
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

                    async def _ingest_planner_done() -> None:
                        try:
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
                task_id = event.get("task_id") or ""
                logger.warning("Worker %s error (fatal=%s): %s", agent_name, is_fatal, event.get("message", ""))
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
                    # roadmap/general_chat defaults.
                    stashed_consult = _planner_consults.pop(task_id, None)
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
                        # defaults to roadmap/general_chat).
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
            supervisor=supervisor,
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

        mode = (msg.get("mode") or "roadmap").strip()
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

        synthetic_id = f"planner-{_uuid.uuid4().hex[:12]}"
        task_data = {
            "task_id": synthetic_id,
            "readable_id": "PLAN",
            "title": f"Planning consult ({mode})",
            "status": "planning",
            "priority": "high",
            "brief": {},
            "workstream_context": ws_ctx,
            "planner_consult": {
                "mode": mode,
                "objective": objective,
                "workstream_id": workstream_id,
                "scope_id": scope_id,
                "approved_spec_reqs": approved_spec_reqs,
                "scope_covers": scope_covers,
            },
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
            "roadmap": "building the workstream roadmap",
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
            and the SAME consult is re-fired (skeleton / materialize / roadmap /
            scope_plan / verify authoring is overwrite-safe — it converges, it
            doesn't duplicate). Capped at ``CUBICLE_PLANNER_MAX_RESTARTS``
            (default 2); after the cap the Manager is poked to re-consult or
            escalate so the work never stalls silently forever.
            """
            import os as _os
            import time as _time

            ctx = (
                f"workstream:{workstream_id}"
                if workstream_id
                else "general_chat"
            )
            try:
                stall_after = float(
                    _os.environ.get("CUBICLE_PLANNER_STALL_SECONDS", "600")
                )
            except (TypeError, ValueError):
                stall_after = 600.0
            try:
                max_restarts = int(
                    _os.environ.get("CUBICLE_PLANNER_MAX_RESTARTS", "2")
                )
            except (TypeError, ValueError):
                max_restarts = 2
            restart_count = int(msg.get("_restart_count") or 0)
            started = _time.monotonic()
            try:
                while True:
                    await asyncio.sleep(75)
                    if not supervisor.is_agent_busy("planner"):
                        break  # consult finished (or failed) — normal exit
                    elapsed = _time.monotonic() - started
                    # Under the stall threshold — OR a VERIFY consult, whose
                    # recovery is owned by the backend stuck-verifying sweeper
                    # (+ reconnect re-fire); the watchdog only auto-restarts the
                    # Manager-initiated modes that have no backend backstop. In
                    # both cases just pulse "still working".
                    if elapsed < stall_after or mode == "verify":
                        mins = max(1, round(elapsed / 60))
                        await mgr._publish_manager_state(
                            ctx, "working",
                            f"🗺️ Planner {_verb} — {mins}m elapsed…",
                        )
                        continue

                    # ── STALL detected (non-verify consult) ─────────────
                    # Re-confirm the Planner is STILL busy right before we
                    # intervene. A consult that finished at the boundary (its
                    # task_complete event still propagating to IDLE) must not be
                    # falsely killed/restarted — this closes the boundary race
                    # for BOTH the cap and the auto-restart paths below.
                    if not supervisor.is_agent_busy("planner"):
                        break
                    if restart_count >= max_restarts:
                        logger.warning(
                            "planner consult STALLED (mode=%s, %.0fs, restart "
                            "cap %d reached) — killing + escalating to Manager",
                            mode, elapsed, max_restarts,
                        )
                        try:
                            await supervisor._kill_process("planner")
                        except Exception:
                            logger.debug("planner kill failed", exc_info=True)
                        # Visible give-up poke (runs a Manager turn) so the
                        # work doesn't stall silently after the cap.
                        try:
                            await mgr.ingest_planner_result({
                                "planner_consult": consult_marker,
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
                        "planner consult STALLED (mode=%s, %.0fs) — "
                        "auto-restart %d/%d",
                        mode, elapsed, restart_count, max_restarts,
                    )
                    await mgr._publish_manager_state(
                        ctx, "working",
                        f"🗺️ Planner {_verb} — stalled, auto-restarting "
                        f"(attempt {restart_count + 1})…",
                    )
                    try:
                        # _kill_process sets kill_initiated → _monitor_exit
                        # suppresses the duplicate crash event, so no failure
                        # poke fires; we own the recovery here.
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
                    break
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("planner heartbeat ended", exc_info=True)

        # T8.1.6: strong-reference via _spawn_background (not bare
        # create_task) so the GC can't collect this fire-and-forget task
        # mid-flight — every other spawn in this module already uses it.
        _spawn_background(_planner_heartbeat(), name="planner-heartbeat")

    router.on("chat_message", mgr.handle_chat_message)
    router.on("switch_context", mgr.handle_switch_context)
    router.on("cancel_turn", mgr.cancel_current_turn)
    router.on("scope_completed", mgr.ingest_scope_completed)
    router.on("task_completed", mgr.ingest_task_completed)
    router.on("consult_planner", _handle_consult_planner)
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

