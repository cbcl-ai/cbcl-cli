"""task_dispatcher.py -- Dispatches tasks from per-agent queues to agent processes.

Thin layer over AgentQueueManager. No locks, no board scans, no heartbeats.
Queue state is maintained by event handlers; the dispatcher just pops and spawns.

The dispatcher:
  - Runs as a background asyncio task
  - On wake: iterates all known agents, dispatches tasks for idle ones
  - Uses AgentQueueManager for all queue operations
  - Publishes task_status_update when moving ready/blocked -> in_progress
  - Runs periodic reconciliation as a safety net (every 60s)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from src.orchestrator.agent_queue import AgentQueueManager
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.config_sync.sync_service import ConfigStore

logger = logging.getLogger("cbcl.dispatcher")

# How often to poll when there are no wake events (safety net).
POLL_INTERVAL_SECONDS: float = 2.0

# Reconciliation interval (seconds).
RECONCILE_INTERVAL_SECONDS: float = 60.0

# How often a recurring "still in state X" log line is allowed to
# re-emit at INFO level. The polling loop fires the same lines
# every 2s while a task waits on deps or while the MA triage
# cooldown is active; without throttling the log fills with hundreds
# of duplicate lines per hour. Within the window we still emit at
# DEBUG so verbose tracing (``LOG_LEVEL=DEBUG``) shows everything.
STATE_LOG_INTERVAL_SECONDS: float = 300.0  # 5 minutes


class TaskDispatcher:
    """Dispatches tasks from per-agent queues to agent processes.

    The dispatcher coordinates with the AgentSupervisor to determine which
    agents are free and spawns worker processes for available tasks.
    """

    def __init__(
        self,
        redis: Redis,
        office_id: str,
        supervisor: AgentSupervisor,
        config_store: ConfigStore,
        queue_manager: AgentQueueManager,
        backend_url: str = "http://localhost:8000",
        security_token: str | None = None,
    ) -> None:
        self._redis = redis
        self._office_id = office_id
        self._supervisor = supervisor
        self._config = config_store
        self._qm = queue_manager
        self._backend_url = backend_url
        # Company Token for the office-scoped REST surface — see
        # backend_client.auth_headers + the CLI-010 / cron-401 audit
        # findings. The /tool-call endpoint currently doesn't require
        # this; the other office-scoped reads do.
        self._security_token = security_token
        self._wake_event = asyncio.Event()
        self._running = False
        # Per-state log throttle. Maps a stable string key (e.g.
        # ``f"deps:{task_id}"``) to the monotonic timestamp of the
        # last INFO emission for that key. ``_log_state`` rate-limits
        # repeated "same state, no change" lines to one INFO per
        # ``STATE_LOG_INTERVAL_SECONDS``; in-between calls emit at
        # DEBUG so the full firehose is still available under
        # ``LOG_LEVEL=DEBUG``. Self-healing: if the task moves to a
        # different state the new log key bypasses this throttle and
        # logs immediately at INFO.
        self._last_state_log: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def wake(self) -> None:
        """Signal the dispatcher to check queues immediately."""
        self._wake_event.set()

    def _log_state(
        self, key: str, message: str, *args: Any,
    ) -> None:
        """Log a recurring "still in state X" line at INFO at most
        once per :data:`STATE_LOG_INTERVAL_SECONDS`; in-between
        calls drop to DEBUG.

        ``key`` is a stable per-state identifier (e.g.
        ``f"deps:{task_id}"``). State transitions naturally re-arm
        the throttle because the key changes when the reason
        changes. ``args`` are forwarded as ``logger.log`` format
        arguments so the value is interpolated lazily — no string
        formatting happens when the line is dropped.

        Prunes the throttle dict opportunistically (every ~128 calls)
        to bound memory. Without pruning, every task_id that has
        ever transitioned through any of the throttled state paths
        leaves a permanent entry — fine for a single run, but a
        slow leak over a daemon's multi-day lifetime. The prune
        drops entries older than 2× the throttle interval (i.e.
        already "fresh enough to log again on next call" anyway,
        so dropping them is lossless).
        """
        now = time.monotonic()
        last = self._last_state_log.get(key, 0.0)
        if now - last >= STATE_LOG_INTERVAL_SECONDS:
            self._last_state_log[key] = now
            logger.info(message, *args)
        else:
            logger.debug(message, *args)
        # Opportunistic prune. ``len & 0x7f`` is cheap and runs
        # roughly once per 128 calls; the threshold scales with
        # usage so a busy office never blocks on a giant scan.
        if len(self._last_state_log) & 0x7f == 0:
            cutoff = now - (STATE_LOG_INTERVAL_SECONDS * 2)
            stale = [
                k for k, t in self._last_state_log.items() if t < cutoff
            ]
            for k in stale:
                del self._last_state_log[k]

    # Legacy compatibility: handlers.py calls add_task/remove_task on the
    # dispatcher. Now these delegate to the queue manager directly.

    async def add_task(self, task_data: dict[str, Any]) -> None:
        """Add a task to the appropriate agent's queue and wake dispatch."""
        agent = task_data.get("assigned_agent", "")
        task_id = task_data.get("task_id") or task_data.get("id", "")
        status = task_data.get("status", "")

        # Blocked tasks ALWAYS go to the Manager Assistant queue,
        # regardless of ``assigned_agent``. The executor's
        # assignment is preserved on the task row for when the
        # task transitions back to ready. Original spec: "No other
        # agent picks up a task from the Blocked column".
        if status == "blocked":
            agent = "manager-assistant"
            task_data = dict(task_data, assigned_agent=agent)
        elif not agent:
            # Unassigned review -> Manager Assistant
            if status == "review":
                agent = "manager-assistant"
                task_data = dict(task_data, assigned_agent=agent)
            else:
                logger.warning(
                    "Cannot enqueue task %s without assigned_agent",
                    task_data.get("readable_id", task_id),
                )
                return

        if agent == "manager":
            return  # Manager is not a worker

        await self._qm.add_task(agent, task_data)
        logger.info(
            "Queued task %s for agent '%s'",
            task_data.get("readable_id", task_id), agent,
        )
        self.wake()

    async def remove_task(self, task_id: str) -> bool:
        """Remove a task from all queues."""
        await self._qm.remove_task_from_all(task_id)
        return True

    async def get_queue_size(self) -> int:
        """Return total tasks across all queues."""
        sizes = await self._qm.get_all_queue_sizes()
        return sum(sizes.values())

    # ------------------------------------------------------------------
    # Dispatch logic
    # ------------------------------------------------------------------

    async def dispatch_agent(self, agent_name: str) -> bool:
        """Try to dispatch the next task for a specific agent.

        Returns True if a task was dispatched.
        """
        if self._supervisor.is_agent_busy(agent_name):
            return False

        if not self._supervisor.can_spawn():
            return False

        # Resolve agent config FIRST — before popping the task. If the
        # daemon's ConfigStore doesn't know this agent yet (the
        # backend has added an agent but the sync_config push hasn't
        # landed, or the daemon was started against an older config),
        # we attempt one in-line refetch. If that still fails we
        # leave the task in the queue and bail; the next dispatch
        # tick (after the missing sync_config arrives) will pick it
        # up. Pre-loop-3 the code popped the task FIRST, found the
        # agent unknown, and silently dropped the task — manifested
        # as "I added an agent, reassigned a task to them, and they
        # never picked it up while the task vanished from the queue".
        agent_config = self._config.get_agent(agent_name)
        if not agent_config:
            agent_config = await self._refetch_agent_config(agent_name)
        if not agent_config:
            logger.warning(
                "Agent '%s' not in config — leaving queued task in place "
                "until next sync_config arrives. Trigger a refresh: "
                "save any agent in the UI (Agents page) or restart cbcl.",
                agent_name,
            )
            return False

        task = await self._qm.pop_next(agent_name)
        if not task:
            return False

        task_id = task.get("task_id") or task.get("id", "")
        readable_id = task.get("readable_id", task_id)
        task_status = task.get("status", "ready")

        # Refresh the current status from the backend before we spawn.
        # The popped queue entry can be stale — e.g. a task that was
        # in ``review`` when enqueued may have been moved to
        # ``blocked`` (or vice-versa) by another path. Dispatching
        # against stale status drives the agent into the wrong mode
        # (the TO-007.T40 regression on 2026-05-14 saw the auditor
        # repeatedly dispatched in review-mode on a blocked task
        # because the queue entry retained ``status=review`` after
        # the task had been moved back to blocked). The fresh fetch
        # is one backend round-trip per dispatch — acceptable
        # overhead for the correctness guarantee.
        fresh_status = await self._fetch_task_status(task_id)
        if fresh_status is None:
            # Backend unreachable or task missing — drop the queue
            # entry and let the next reconcile cycle decide.
            logger.info(
                "Dropping queue entry for %s — backend status lookup "
                "failed", readable_id,
            )
            return False
        if fresh_status in ("done", "archived"):
            logger.info(
                "Task %s is %s — dropping stale queue entry",
                readable_id, fresh_status,
            )
            return False
        if fresh_status != task_status:
            logger.info(
                "Task %s status changed since enqueue (%s → %s); "
                "dropping stale queue entry, reconciler will re-route",
                readable_id, task_status, fresh_status,
            )
            return False

        # Blocked tasks: ONLY the Manager Assistant triages.
        # Defensive guard against a stale queue entry that routes a
        # blocked task to any other agent (the executor, a custom
        # agent, the auditor). The reconciler now enforces "blocked
        # → MA queue" upstream, but a queue entry left over from
        # before the task transitioned to blocked could still slip
        # through. Drop it; the next reconcile cycle will route it
        # to the MA queue if appropriate. See
        # docs/specs/task-spec.md — original spec rule "No other
        # agent picks up a task from the Blocked column".
        if task_status == "blocked" and agent_name != "manager-assistant":
            self._log_state(
                f"blocked-wrong-agent:{task_id}:{agent_name}",
                "Skipping dispatch of blocked task %s to '%s' — "
                "only the Manager Assistant triages blocked tasks",
                readable_id, agent_name,
            )
            return False

        # Cooldown lock for blocked-task dispatch to the Manager
        # Assistant. When the MA has already triaged this task
        # recently (and either posted a synthesis comment or
        # proposed an action_request for the user), re-dispatching
        # it within the cooldown window produces a no-op run that
        # just spams the activity feed. The cooldown lock is what
        # the user explicitly designed to prevent this; without
        # this dispatcher-side check the reconciler would re-add
        # the task every 60s regardless of recent triage. See
        # docs/specs/task-spec.md Hard Rule #10.
        if task_status == "blocked" and agent_name == "manager-assistant":
            if await self._is_blocked_triage_in_cooldown(task_id):
                self._log_state(
                    f"ma-cooldown:{task_id}",
                    "Skipping MA dispatch on blocked task %s — "
                    "triage cooldown still active",
                    readable_id,
                )
                return False

        # Check task dependencies before dispatching
        depends_on = task.get("depends_on") or []
        if depends_on and task_status in ("ready", "blocked"):
            deps_met = await self._check_dependencies(task_id)
            if not deps_met:
                self._log_state(
                    f"deps:{task_id}",
                    "Task %s has unmet dependencies, re-queuing",
                    readable_id,
                )
                await self._qm.add_task(agent_name, task)
                return False

        # Scope gate: a task belonging to a non-executing scope must not
        # be dispatched. The payload may have a stale scope_state, so
        # re-verify via backend if the cached value suggests gating.
        scope_id = task.get("scope_id")
        if scope_id:
            scope_state = task.get("scope_state")
            if scope_state not in ("executing", None):
                # Confirm with backend before taking action — payload may
                # be stale after an activation event.
                fresh_state = await self._fetch_scope_state(scope_id)
                if fresh_state and fresh_state != "executing":
                    self._log_state(
                        f"scope-gate:{task_id}:{fresh_state}",
                        "Task %s belongs to scope in '%s' state — skipping",
                        readable_id, fresh_state,
                    )
                    # Don't re-queue — the scope activation will trigger a
                    # fresh task_ready event when tasks become eligible.
                    # Leaving it in the queue would waste dispatch cycles.
                    return False
                # Fresh state is executing: update in-memory and continue
                task["scope_state"] = fresh_state or "executing"

        # Enrich task with workstream context for the worker prompt
        workstream_id = task.get("workstream_id", "")
        if workstream_id and "workstream_context" not in task:
            ws = self._config.get_workstream(workstream_id)
            if ws:
                task["workstream_context"] = {
                    "name": ws.get("name", ""),
                    "description": ws.get("description", ""),
                    "goals": ws.get("goals", ""),
                }

        logger.info("Dispatching %s to agent '%s'", readable_id, agent_name)

        success = await self._supervisor.spawn_worker(
            agent_name, agent_config, task,
        )

        if success:
            # Track active task in queue manager.
            statuses = self._supervisor.get_all_statuses()
            agent_pid = statuses.get(agent_name, {}).get("pid", 0) or 0

            # Mode mapping:
            #   review  → review (reviewer works in-place on a review task)
            #   blocked → triage (MA triages; task STAYS blocked — see below)
            #   ready   → execute (worker picks the task up)
            #
            # The blocked → triage path is the C3 fix: pre-flipping
            # ``blocked → ready → in_progress`` before the MA reads
            # the task contradicted the MA playbook's "NEVER call
            # move_task(blocked → ready)" rule and burned one bounce
            # per dispatch. With triage mode the task stays blocked,
            # the MA reads it as-is, and the playbook's three
            # resolution paths (answer-and-stop / helper-task /
            # propose_action) operate on a truthful task state.
            if task_status == "review":
                mode = "review"
            elif task_status == "blocked":
                mode = "triage"
            else:
                mode = "execute"
            await self._qm.set_active(
                agent_name, task_id, readable_id, task_status, mode, agent_pid,
            )

            # Status-flip + assign via HTTP.
            #   ready  → in_progress (worker picks up)
            #   blocked → no flip; just assign the MA so the task
            #             carries the assigned_agent on activity feeds
            #             without changing column
            #   review → no flip (reviewer works in-place)
            if task_status == "ready":
                moved = await self._move_and_assign(
                    task_id, agent_name, "in_progress",
                )
                if not moved:
                    # The HTTP move failed — board is still showing
                    # ``ready`` and the worker would otherwise execute
                    # invisibly. Roll back: clear the active marker so
                    # the task re-enters the queue on the next reconciler
                    # tick. The spawned worker subprocess is left to the
                    # watchdog to reap (sending it an explicit kill from
                    # here would race the supervisor's own
                    # task-assignment write that we already did).
                    logger.warning(
                        "dispatch %s: ready→in_progress failed; "
                        "clearing active marker so the task re-enters "
                        "the queue on the next reconciler tick",
                        readable_id,
                    )
                    await self._qm.clear_active(agent_name)
                    return False
            elif task_status == "blocked":
                await self._assign_only(task_id, agent_name)

            return True
        else:
            # Spawn failed — put task back in queue.
            logger.warning("Spawn failed for %s, re-queuing", readable_id)
            await self._qm.add_task(agent_name, task)
            return False

    async def dispatch_all_idle(self) -> int:
        """Try to dispatch tasks for ALL idle agents. Returns count."""
        dispatched = 0
        agent_names = self._get_all_agent_names()
        for agent_name in agent_names:
            if await self.dispatch_agent(agent_name):
                dispatched += 1
        return dispatched

    async def on_agent_complete(self, agent_name: str) -> None:
        """Called when an agent finishes a task. Dispatch next immediately.

        The supervisor sets worker state to IDLE on task_complete, so
        dispatch_agent will succeed. The old process is cleaned up by
        spawn_worker before creating the new one.
        """
        await self._qm.clear_active(agent_name)
        await self.dispatch_agent(agent_name)

    # ------------------------------------------------------------------
    # Main dispatch loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main dispatch loop. Runs until stopped.

        On startup, does a full board sync to populate queues, then
        continuously dispatches tasks to idle agents.
        Reconciliation runs every RECONCILE_INTERVAL_SECONDS as safety net.
        """
        self._running = True
        logger.info("Task dispatcher started for office %s", self._office_id)

        # Full sync on startup — populate per-agent queues from board.
        # Only sync if we can fetch tasks from the backend. If fetch fails
        # or returns empty, leave existing queue state (may have been
        # pre-populated via add_task).
        try:
            tasks = await self._fetch_board_tasks()
            if tasks:
                sizes = await self._qm.full_sync(tasks)
                logger.info("Startup sync: %s", sizes)
            else:
                logger.info("No board tasks found — using existing queue state")
        except Exception as exc:
            logger.warning("Startup board sync failed: %s", exc)

        # Initial dispatch for all idle agents.
        try:
            dispatched = await self.dispatch_all_idle()
            if dispatched:
                logger.info("Initial dispatch: %d agents working", dispatched)
        except Exception as exc:
            logger.warning("Initial dispatch failed: %s", exc)

        last_reconcile = time.monotonic()

        while self._running:
            try:
                dispatched = await self.dispatch_all_idle()
            except Exception as exc:
                logger.exception("Dispatch cycle error: %s", exc)
                await asyncio.sleep(5)
                continue

            # Periodic reconciliation — safety net.
            if time.monotonic() - last_reconcile > RECONCILE_INTERVAL_SECONDS:
                # If no agents are known yet (config fetch failed at startup),
                # re-fetch config from backend before reconciling.
                if not self._get_all_agent_names():
                    try:
                        import httpx
                        from src.backend_client import auth_headers
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            resp = await client.get(
                                f"{self._backend_url}/api/offices/{self._office_id}/agents",
                                headers=auth_headers(self._security_token),
                            )
                            if resp.status_code == 200:
                                agents = resp.json()
                                if agents:
                                    self._config.agents = agents
                                    logger.info("Config retry: loaded %d agents", len(agents))
                    except Exception:
                        pass

                try:
                    tasks = await self._fetch_board_tasks()
                    await self._qm.reconcile(tasks)
                except Exception as exc:
                    logger.debug("Reconciliation error: %s", exc)
                last_reconcile = time.monotonic()

            # If we dispatched, immediately check again.
            if dispatched > 0:
                continue

            # Wait for wake signal or poll interval.
            self._wake_event.clear()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=POLL_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

        logger.info("Task dispatcher stopped for office %s", self._office_id)

    async def stop(self) -> None:
        """Stop the dispatch loop gracefully."""
        self._running = False
        self._wake_event.set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_all_agent_names(self) -> list[str]:
        """Get all known agent names from config."""
        return [
            a.get("name", "")
            for a in self._config.agents
            if a.get("name") and a.get("is_active", True)
        ]

    async def _refetch_agent_config(
        self, agent_name: str,
    ) -> dict | None:
        """Refetch the full agents list from the backend when an
        agent is missing from the local ConfigStore.

        Belt-and-braces against the case where the backend's
        ``push_sync_config_to_daemon`` push raced ahead of an
        ``update_task`` event OR was lost in transit. The
        refetched list is written back into ``ConfigStore.agents``
        so subsequent ``dispatch_agent`` ticks find the agent
        without another round trip.
        """
        import httpx

        from src.backend_client import auth_headers

        url = (
            f"{self._backend_url}/api/offices/{self._office_id}/agents"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url, headers=auth_headers(self._security_token),
                )
            if resp.status_code != 200:
                return None
            agents = resp.json() or []
            if not agents:
                return None
            self._config.agents = agents
            logger.info(
                "ConfigStore refreshed via on-demand refetch (%d "
                "agents) — was missing '%s'",
                len(agents), agent_name,
            )
            return self._config.get_agent(agent_name)
        except Exception as exc:
            logger.debug(
                "On-demand agent refetch failed: %s", exc,
            )
            return None

    async def _fetch_board_tasks(self) -> list[dict]:
        """Fetch all actionable tasks from the backend."""
        import httpx
        from src.backend_client import auth_headers

        backend_url = self._backend_url
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{backend_url}/api/offices/{self._office_id}/tasks",
                    headers=auth_headers(self._security_token),
                    params={
                        "status": "ready,in_progress,review,blocked",
                        "limit": 200,
                    },
                )
                if resp.status_code == 200:
                    return resp.json().get("items", [])
        except Exception as exc:
            from src.utils import describe_exception
            logger.warning(
                "Failed to fetch board tasks: %s", describe_exception(exc),
            )
        return []

    async def _assign_only(self, task_id: str, agent_name: str) -> None:
        """Assign an agent to a task WITHOUT flipping its status.

        Used for the triage-mode dispatch path (blocked tasks → MA).
        The task stays in blocked so the MA reads the true column
        state and its playbook's "blocked-mode is DOCUMENT-AND-
        ESCALATE only" rule applies to a task that is actually
        still blocked — not one the dispatcher silently unblocked.
        """
        import httpx

        from src.backend_client import auth_headers

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._backend_url}/api/offices/{self._office_id}/tool-call",
                    json={"action": "update_task", "params": {
                        "task_id": task_id,
                        "assigned_agent": agent_name,
                    }},
                    # SEC3-01: Company-Token bearer so the backend accepts this
                    # host-dispatcher call once /tool-call auth is enforced.
                    headers=auth_headers(self._security_token),
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "assign-only HTTP %d for task %s: %s",
                        resp.status_code, task_id[:8], resp.text[:200],
                    )
                else:
                    logger.info(
                        "Assigned %s to blocked task %s (no status flip)",
                        agent_name, task_id[:8],
                    )
        except Exception as exc:
            logger.warning(
                "Failed to assign-only task %s: %s", task_id[:8], exc,
            )

    async def _move_and_assign(
        self, task_id: str, agent_name: str, new_status: str,
    ) -> bool:
        """Assign the agent AND move task ``ready → new_status`` via HTTP.

        Returns True iff EVERY step succeeded. The caller is expected
        to check this — pre-0.2.26 each ``client.post(...)`` was
        fire-and-check-nothing, so a 400/500 response (or a 200 with
        ``{"error": "..."}`` in the body) was silently swallowed and
        the worker was spawned anyway. Symptom: the task stayed in
        the source column visually while the worker chewed through
        it in the background.

        Uses synchronous HTTP (not fire-and-forget Redis) to ensure the
        backend DB is updated before the agent starts working. This prevents
        the UI showing a stale status/assignment.

        Blocked tasks do NOT flow through here: the dispatcher's
        ``dispatch_agent`` calls ``_assign_only`` for them so the task
        stays in ``blocked`` (the MA's triage playbook needs a truthful
        column state). An earlier draft supported a two-hop
        ``blocked → ready → in_progress`` transition here; that branch
        was dead AND dangerous because it would have burned the
        ``blocked_bounce_count`` cap (see
        ``docs/specs/task-spec.md`` rule #11).
        """
        import httpx

        from src.backend_client import auth_headers

        async def _post(client, action: str, params: dict, step: str) -> bool:
            try:
                resp = await client.post(
                    f"{self._backend_url}/api/offices/{self._office_id}/tool-call",
                    json={"action": action, "params": params},
                    # SEC3-01: Company-Token bearer so the backend accepts this
                    # host-dispatcher call once /tool-call auth is enforced
                    # (in-container agents authenticate with X-Office-Secret).
                    headers=auth_headers(self._security_token),
                )
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(
                    "dispatch _move_and_assign %s [%s]: HTTP error: %s",
                    task_id[:8], step, exc,
                )
                return False
            if resp.status_code >= 400:
                logger.warning(
                    "dispatch _move_and_assign %s [%s]: HTTP %d: %s",
                    task_id[:8], step, resp.status_code,
                    resp.text[:300],
                )
                return False
            # 200 may still carry an in-body error envelope from the
            # tool-call dispatcher — check that explicitly.
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get("error"):
                logger.warning(
                    "dispatch _move_and_assign %s [%s]: body error: %s",
                    task_id[:8], step, body.get("error"),
                )
                return False
            return True

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Step 1: Assign the agent.
                if not await _post(
                    client, "update_task",
                    {"task_id": task_id, "assigned_agent": agent_name},
                    step="assign",
                ):
                    return False
                # Step 2: Move ready → new_status as the agent.
                if not await _post(
                    client, "move_task",
                    {
                        "task_id": task_id,
                        "new_status": new_status,
                        "actor": agent_name,
                    },
                    step=f"ready->{new_status}",
                ):
                    return False
                logger.info(
                    "Moved task %s to %s (agent=%s) via HTTP",
                    task_id[:8], new_status, agent_name,
                )
                return True
        except Exception as exc:
            logger.warning(
                "dispatch _move_and_assign %s: unexpected: %s",
                task_id[:8], exc,
            )
            return False

    async def _fetch_task_status(self, task_id: str) -> str | None:
        """Fetch the task's CURRENT status from the backend.

        Returns the status string, or ``None`` if the lookup failed
        (network error, task missing, etc.). Callers treat ``None``
        as "drop the queue entry and let the reconciler decide" —
        we can't reason about staleness without ground truth, but
        we also don't want to spawn on a possibly-wrong status."""
        import httpx
        from src.backend_client import auth_headers

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._backend_url}/api/offices/{self._office_id}"
                    f"/tasks/{task_id}",
                    headers=auth_headers(self._security_token),
                )
                if resp.status_code == 200:
                    return resp.json().get("status")
                logger.info(
                    "Task status lookup %s returned HTTP %d",
                    task_id[:8], resp.status_code,
                )
        except Exception as exc:
            logger.warning(
                "Failed to fetch task status for %s: %s",
                task_id[:8], exc,
            )
        return None

    async def _is_blocked_triage_in_cooldown(self, task_id: str) -> bool:
        """Return True when the MA must NOT be re-dispatched on this
        blocked task — either because a pending ``action_request``
        is already in the user's inbox or because the cooldown lock
        (``last_blocked_triage_at`` within
        ``CUBICLE_BLOCKED_TRIAGE_COOLDOWN_SECONDS``) is still active.

        Delegates to ``task_should_skip_ma_routing`` so the
        dispatcher and the WS routing paths share exactly one
        check — drift between them is what produced the TO-007.T40
        regression on 2026-05-14 (the dispatcher used to call the
        narrower ``task_blocked_triage_within_cooldown`` helper with
        a missing ``cooldown_seconds`` kwarg, which raised TypeError
        and was silently swallowed by the surrounding ``except``).

        Fail-open on transport errors — a transient backend blip
        must not permanently park triage."""
        try:
            from src.backend_client import task_should_skip_ma_routing
        except ImportError:
            return False
        try:
            return await task_should_skip_ma_routing(
                platform_url=self._backend_url,
                office_id=self._office_id,
                task_id=task_id,
                security_token=self._security_token,
            )
        except Exception as exc:
            logger.debug(
                "Triage-cooldown check failed for %s (fail-open): %s",
                task_id[:8], exc,
            )
            return False

    async def _fetch_scope_state(self, scope_id: str) -> str | None:
        """Fetch a scope's current state from the backend.

        Returns the state string, or None if fetch failed. The caller
        should treat None as 'allow dispatch' (fail-open) to avoid
        permanently blocking tasks when backend is transiently down.
        """
        import httpx
        from src.backend_client import auth_headers

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._backend_url}/api/offices/{self._office_id}"
                    f"/scopes/{scope_id}",
                    headers=auth_headers(self._security_token),
                )
                if resp.status_code == 200:
                    return resp.json().get("state")
        except Exception as exc:
            logger.warning("Failed to fetch scope state %s: %s", scope_id, exc)
        return None

    async def _check_dependencies(self, task_id: str) -> bool:
        """Check if a task's dependencies are all done via backend API.

        Returns True if all dependency tasks are in 'done' status,
        or if the task has no dependencies.
        """
        import httpx
        from src.backend_client import auth_headers

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._backend_url}/api/offices/{self._office_id}/tasks/{task_id}",
                    headers=auth_headers(self._security_token),
                )
                if resp.status_code == 200:
                    task_data = resp.json()
                    return task_data.get("dependencies_met", True)
        except Exception as exc:
            logger.warning("Failed to check dependencies for %s: %s", task_id[:8], exc)
        # If we can't check, allow dispatch (fail open)
        return True
