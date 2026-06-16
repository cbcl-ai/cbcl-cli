"""task_dispatcher.py -- Dispatches tasks from per-agent queues to agent processes.

Thin layer over AgentQueueManager. No locks, no board scans, no heartbeats.
Queue state is maintained by event handlers; the dispatcher just pops and spawns.

The dispatcher:
  - Runs as a background asyncio task
  - On wake: iterates all known agents, dispatches tasks for idle ones
  - Uses AgentQueueManager for all queue operations
  - Moves tasks ready -> in_progress via the backend tool-call/move path (NOT
    a ``task_status_update`` event — that legacy event is a backend compat
    shim only; T8.3.8/D-13)
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

# T4.2.1 (07/P2): statuses that count as "the agent is still working" for
# the strict-serialization predicate. ``blocked`` is deliberately EXCLUDED
# (DECISION-2: a blocked task is parked on a human/MA decision — keeping its
# executor idle gains nothing and would deadlock MA helper tasks).
_STRICT_BUSY_STATUSES: frozenset[str] = frozenset({"in_progress", "review"})

# T4.2.1 deadlock backstop: when every candidate agent has been
# strict-blocked on a ready pop for longer than this, log CRITICAL and
# escalate once (cheap insurance for cycles the design analysis missed).
STRICT_DEADLOCK_SECONDS: float = 900.0  # 15 min

# Round-2 LOW: per-task cap on the kill+clear+requeue rollback after a
# failed ready→in_progress move. The fresh-status pre-check makes a
# DETERMINISTIC move failure near-impossible (transient-only exposure),
# but cap it anyway: after this many failed moves for the same task the
# entry is DROPPED (not re-queued) with one WARNING — the 60s
# reconciler + the backend's stuck-ready sweeper own it from there.
MOVE_ROLLBACK_REQUEUE_CAP: int = 3

# T3.2.2 (03/§4.3 #28): sentinel returned by ``_fetch_task_status``
# when the lookup failed TRANSIENTLY (network error / backend 5xx) —
# distinct from ``None``, which means the task itself is gone/denied
# (404 etc., a deliberate drop). On the sentinel the dispatcher
# RE-QUEUES the in-hand entry instead of dropping it, so recovery
# doesn't ride the 60s reconciler that is failing during the same
# backend outage. A plain str so the ``str | None`` signature (and
# every test stub of this method) stays valid.
_STATUS_FETCH_FAILED = "__status_fetch_failed__"

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
        # Round-2 LOW: per-task counter of failed ready→in_progress
        # moves (the kill+clear+requeue rollback). Mirrors the
        # watchdog's in-memory ``_task_crash_count`` pattern: pruned on
        # a successful move, dropped (with the queue entry) at
        # ``MOVE_ROLLBACK_REQUEUE_CAP``; a daemon restart resets it.
        self._move_rollback_failures: dict[str, int] = {}
        # T4.2.1 (07/P2) strict serialization. The reconciler caches its
        # last SUCCESSFUL board fetch here; ``dispatch_agent`` reads it to
        # decide whether an agent already holds another in_progress/review
        # task before letting it pop a NEW ``ready`` task (execute-mode
        # only — review/triage stay always-dispatchable to avoid the
        # reviewer-cycle deadlock; ``blocked`` releases the executor per
        # DECISION-2). Snapshot-derived, not new state → self-healing.
        self._last_board_snapshot: list[dict] = []
        # agent → monotonic ts when it was FIRST strict-blocked on a ready
        # pop this episode (cleared on any successful dispatch). Feeds the
        # >15min deadlock detector.
        self._strict_block_since: dict[str, float] = {}
        # Per-agent one-shot: an agent that has been escalated for a
        # strict-serialization wedge stays in this set until IT dispatches
        # (re-armed in _clear_strict_block). Per-agent — NOT a global bool —
        # so another agent's routine review/triage dispatch can't silently
        # disarm a genuinely-wedged agent's escalation.
        self._strict_deadlock_escalated_agents: set[str] = set()
        # T8/1.1+2.1: read-only handle to the crash-metering authority
        # (the watchdog). Set via ``set_watchdog`` after both are built.
        # ``dispatch_agent`` consults it so it doesn't re-spawn a task that
        # already hit the respawn cap, and the deadlock detector doesn't
        # arm against a holder that's merely under crash recovery. Optional
        # — when unset (older wiring / tests) behavior is unchanged.
        self._watchdog: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_watchdog(self, watchdog: Any) -> None:
        """Wire the crash-metering watchdog (read-only). See ``_watchdog``."""
        self._watchdog = watchdog

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
            # The agent is making progress (running its own task) — it is
            # not wedged. Drop any stale strict-block timer so the deadlock
            # detector can't false-fire on a busy agent (T4.2.1 hardening).
            self._clear_strict_block(agent_name)
            return False

        if not self._supervisor.can_spawn():
            # Office at the concurrency cap. Do NOT clear the strict-block
            # timer here: if the cap is itself filled by wedged/phantom
            # agents, a genuinely-wedged agent behind the cap must still age
            # into an escalation rather than have its timer reset every tick.
            # (Only real progress — is_agent_busy — or an actual dispatch
            # clears it.)
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
        if fresh_status == _STATUS_FETCH_FAILED:
            # TRANSIENT lookup failure (backend unreachable / 5xx) —
            # the entry is in hand, so put it BACK instead of dropping
            # it (T3.2.2 / 03 #28). Dropping forced recovery onto the
            # 60s reconciler, whose board fetch fails during the same
            # outage. The deliberate drops below (task missing, status
            # drift, blocked-wrong-agent, scope-gate) stay
            # reconciler-recovered as designed.
            self._log_state(
                f"status-fetch-failed:{task_id}",
                "Backend status lookup failed transiently for %s — "
                "re-queuing the entry for the next dispatch tick",
                readable_id,
            )
            await self._qm.add_task(agent_name, task)
            return False
        if fresh_status is None:
            # Task missing/denied on the backend — drop the queue
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

        # T8/1.1: respawn-cap honoring. The watchdog is the crash-metering
        # authority and escalates a crash-looping task to ``blocked`` at the
        # cap. But the dispatcher ALSO re-adds in_progress orphans on its
        # reconcile cadence, so without this guard it would keep re-spawning
        # a doomed task past the cap (leaking CLI spawns) until the
        # watchdog's slower tick finally lands the blocked move. Drop the
        # entry instead — the watchdog owns the escalation; the next
        # reconcile won't re-add once the task shows ``blocked``.
        if (
            task_status == "in_progress"
            and self._watchdog is not None
            and self._watchdog.respawn_capped(task_id)
        ):
            self._log_state(
                f"respawn-capped:{task_id}",
                "Not re-spawning in_progress orphan %s — crash-respawn cap "
                "reached; watchdog owns escalation to blocked",
                readable_id,
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

        # T4.2.1 (07/P2): strict serialization for EXECUTE-mode pops. An
        # agent must not pick up a NEW ``ready`` task while it still holds
        # ANOTHER task in in_progress/review (own-task exempt so rework
        # returns / respawn-in-place still flow; ``blocked`` releases the
        # executor per DECISION-2). Review/triage dispatch (task_status
        # ``review``/``blocked``) is EXEMPT — it must always be
        # dispatchable or the reviewer-cycle / MA-triage path deadlocks.
        if task_status == "ready" and self._agent_has_other_active_task(
            agent_name, task_id
        ):
            # Block the ready pop for BOTH an in_progress and a review
            # holder (P2). But only ARM the deadlock detector for a phantom
            # in_progress block: a worker actively running its own task is
            # ``is_agent_busy`` and never reaches here, so reaching the gate
            # with an in_progress holder means that prior task is stuck with
            # an idle process — genuinely deadlock-suspect. A review-wait is
            # BOUNDED (the reviewer dispatches independently and is capped),
            # so it is normal operation; arming the timer on it would
            # false-fire a CRITICAL + user escalation on every >15min review
            # (the executor stays assigned through review per Rule #15).
            holder_id = self._in_progress_holder_id(agent_name, task_id)
            holder_recovering = (
                holder_id is not None
                and self._watchdog is not None
                and self._watchdog.is_crash_recovering(holder_id)
            )
            if holder_id is not None and not holder_recovering:
                self._note_strict_block(agent_name)
            else:
                # No in_progress holder (review-wait → bounded), OR the
                # holder is under active crash recovery (being respawned /
                # escalated, not deadlocked). T8/2.1: arming here would
                # false-fire a CRITICAL + user escalation on a crash-loop
                # whose blocked-escalation is in flight.
                self._clear_strict_block(agent_name)
            self._log_state(
                f"strict-serialize:{agent_name}",
                "Holding ready task %s for '%s' — agent still has another "
                "in_progress/review task (strict serialization)",
                readable_id, agent_name,
            )
            await self._qm.add_task(agent_name, task)
            return False
        # Cleared the gate (or exempt mode) — this agent is dispatching, so
        # drop any strict-block timer + the deadlock one-shot.
        self._clear_strict_block(agent_name)

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
                    # ``ready`` but a live worker subprocess has already
                    # been spawned and assigned this task. NOTHING else
                    # reaps a healthy process (the watchdog only catches
                    # stale/silent sessions), so left alone the worker
                    # would execute the full task invisibly, its final
                    # ready→review move would be rejected by the backend,
                    # and the reconciler would re-dispatch the same task
                    # — double execution, with the second run able to
                    # clobber the first's artifacts. Roll back fully:
                    # kill the rogue worker NOW (safe — spawn_worker's
                    # task-assignment write has settled by the time we
                    # see the move result), clear the active marker, and
                    # RE-QUEUE the entry so the next dispatch tick
                    # retries it instead of dropping it on the floor.
                    # Round-2 LOW: bounded per task — after
                    # MOVE_ROLLBACK_REQUEUE_CAP failed moves the entry
                    # is dropped (one WARNING); the 60s reconciler +
                    # the backend stuck-ready sweeper own it from there.
                    await self._supervisor._kill_process(agent_name)
                    await self._qm.clear_active(agent_name)
                    failures = (
                        self._move_rollback_failures.get(task_id, 0) + 1
                    )
                    if failures > MOVE_ROLLBACK_REQUEUE_CAP:
                        self._move_rollback_failures.pop(task_id, None)
                        logger.warning(
                            "dispatch %s: ready→in_progress failed %d "
                            "times — dropping the queue entry (NOT "
                            "re-queuing); the reconciler / backend "
                            "stuck-ready sweeper own recovery",
                            readable_id, failures,
                        )
                        return False
                    self._move_rollback_failures[task_id] = failures
                    logger.warning(
                        "dispatch %s: ready→in_progress failed; killing "
                        "spawned worker '%s', clearing active marker and "
                        "re-queuing the task for the next dispatch tick "
                        "(attempt %d/%d)",
                        readable_id, agent_name, failures,
                        MOVE_ROLLBACK_REQUEUE_CAP,
                    )
                    await self._qm.add_task(agent_name, task)
                    return False
                # Successful move: prune any rollback-failure counter.
                self._move_rollback_failures.pop(task_id, None)
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
                self._last_board_snapshot = tasks  # T4.2.1 snapshot seed
                sizes = await self._qm.full_sync(tasks)
                logger.info("Startup sync: %s", sizes)
            elif tasks is None:
                # Fetch FAILED (not "board is empty") — keep whatever
                # queue state exists rather than syncing against a lie.
                logger.warning(
                    "Startup board fetch failed — using existing "
                    "queue state",
                )
            else:
                logger.info("No board tasks found — using existing queue state")
        except Exception as exc:
            logger.warning("Startup board sync failed: %s", exc)

        # Self-heal any agent left stuck in a busy state with no live process
        # (e.g. a reviewer whose session was cancelled/shut down on an old
        # task) BEFORE the initial dispatch, so its queued review/work task
        # can be assigned on the very first cycle after a restart.
        try:
            healed = self._supervisor.reconcile_stuck_agents()
            if healed:
                logger.info("Self-heal reset stuck agents: %s", healed)
        except Exception as exc:
            logger.debug("Stuck-agent self-heal failed: %s", exc)

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
                # Self-heal stuck-busy agents (no live process) each cycle so a
                # reviewer/worker left "working" by a cancelled session recovers
                # within one tick instead of waiting for a daemon restart.
                healed = self._supervisor.reconcile_stuck_agents()
                if healed:
                    logger.info("Self-heal reset stuck agents: %s", healed)
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

                await self._reconcile_once()
                last_reconcile = time.monotonic()

            # If we dispatched, immediately check again.
            if dispatched > 0:
                continue

            # T4.2.1 deadlock backstop: nothing dispatched this cycle —
            # if agents have been strict-blocked past the threshold, log
            # CRITICAL + raise a user-visible escalate_blocker AR once per
            # agent. Cheap insurance for serialization cycles the design
            # analysis missed.
            try:
                wedged = self._detect_strict_deadlock()
                if wedged:
                    await self._escalate_strict_deadlock(wedged)
            except Exception as exc:  # never let the backstop crash the loop
                logger.debug("Deadlock-detector tick error: %s", exc)

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

    async def _reconcile_once(self) -> None:
        """One periodic queue-reconcile tick.

        T3.2.2 (07/G13): a FAILED board fetch must skip ``reconcile()``
        entirely. The old inline code passed the error-path ``[]``
        straight into ``reconcile``, which interpreted it as "the board
        is empty" and wiped every per-agent queue on a transient
        backend 500 (S24) — self-healing 60s later, but popped /
        dispatched state churned meanwhile.
        """
        try:
            tasks = await self._fetch_board_tasks()
            if tasks is None:
                logger.warning(
                    "Board fetch failed — skipping queue reconcile "
                    "this cycle (queues left intact)",
                )
                return
            # T4.2.1: cache the last SUCCESSFUL snapshot for the strict-
            # serialization predicate in dispatch_agent.
            self._last_board_snapshot = tasks
            await self._qm.reconcile(tasks)
        except Exception as exc:
            # _fetch_board_tasks already swallows transient fetch errors
            # (returns None → early-return above), so reaching here means
            # a real bug in reconcile()/snapshot handling — surface it.
            logger.warning(
                "Reconciliation error: %s", exc, exc_info=True,
            )

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

    # ------------------------------------------------------------------
    # T4.2.1 — strict serialization (07/P2)
    # ------------------------------------------------------------------

    @staticmethod
    def _task_identity(task: dict) -> set[str]:
        """All identifiers a task might be keyed by across surfaces."""
        return {
            str(task.get(k))
            for k in ("task_id", "id", "readable_id")
            if task.get(k)
        }

    def _agent_has_other_active_task(
        self, agent_name: str, this_task_id: str,
    ) -> bool:
        """True iff the last board snapshot shows ANOTHER in_progress/review
        task assigned to ``agent_name`` (own-task exemption: ``this_task_id``
        is excluded so rework returns / crash-respawn-in-place are not
        blocked). ``blocked`` is not counted (DECISION-2 — releases the
        executor). Empty snapshot → never blocks (fail-open; the
        process-level ``is_agent_busy`` gate still applies upstream).
        """
        this_id = str(this_task_id)
        for task in self._last_board_snapshot:
            if task.get("assigned_agent") != agent_name:
                continue
            if task.get("status") not in _STRICT_BUSY_STATUSES:
                continue
            if this_id in self._task_identity(task):
                continue  # own task — exempt
            return True
        return False

    def _in_progress_holder_id(
        self, agent_name: str, this_task_id: str,
    ) -> str | None:
        """Return the task_id of ANOTHER ``in_progress`` task held by
        ``agent_name`` (own task excluded), or None. The id lets the
        deadlock-arming site cross-check the holder against the watchdog's
        crash state (T8/2.1)."""
        this_id = str(this_task_id)
        for task in self._last_board_snapshot:
            if task.get("assigned_agent") != agent_name:
                continue
            if task.get("status") != "in_progress":
                continue
            if this_id in self._task_identity(task):
                continue  # own task — exempt
            return task.get("task_id") or task.get("id", "")
        return None

    def _agent_blocked_by_in_progress(
        self, agent_name: str, this_task_id: str,
    ) -> bool:
        """True iff the last board snapshot shows ANOTHER ``in_progress``
        task for ``agent_name`` (own task excluded).

        This is the ONLY strict-block kind that is deadlock-suspect. A
        worker actually running its own task is ``is_agent_busy`` and never
        reaches the strict gate, so reaching it with an in_progress holder
        means that prior task is stuck with an idle executor process —
        worth escalating if it persists. A ``review`` holder is a bounded
        wait (the reviewer dispatches independently and is rework-capped),
        so it must NOT arm the deadlock detector or it would false-fire on
        every long review while the executor correctly waits for its next
        ready task.
        """
        return self._in_progress_holder_id(agent_name, this_task_id) is not None

    def _note_strict_block(self, agent_name: str) -> None:
        """Record (once) when an agent first gets strict-blocked."""
        self._strict_block_since.setdefault(agent_name, time.monotonic())

    def _clear_strict_block(self, agent_name: str) -> None:
        """An agent dispatched — clear its strict-block timer and re-arm
        ITS OWN deadlock-escalation one-shot (per-agent)."""
        self._strict_block_since.pop(agent_name, None)
        self._strict_deadlock_escalated_agents.discard(agent_name)

    def _detect_strict_deadlock(self) -> list[str]:
        """Return agents NEWLY detected wedged > STRICT_DEADLOCK_SECONDS
        this tick (excluding agents already escalated this episode).

        A non-empty result means the ready queue has work but a candidate
        executor has been strict-blocked too long — a likely serialization
        deadlock the design analysis missed. Logs CRITICAL and marks the
        per-agent one-shot so the same agent isn't re-escalated until IT
        dispatches. Pure (timer map + monotonic clock) so it unit-tests
        cleanly; the caller fires the user-visible escalation AR.
        """
        if not self._strict_block_since:
            return []
        now = time.monotonic()
        newly = sorted(
            agent
            for agent, since in self._strict_block_since.items()
            if now - since >= STRICT_DEADLOCK_SECONDS
            and agent not in self._strict_deadlock_escalated_agents
        )
        if not newly:
            return []
        self._strict_deadlock_escalated_agents.update(newly)
        logger.critical(
            "STRICT-SERIALIZATION DEADLOCK suspected: agent(s) %s have been "
            "strict-blocked on ready tasks for >%.0fs with nothing "
            "dispatching. The ready queue has work but every candidate "
            "executor holds another in_progress/review task. Escalating to "
            "the user. office=%s",
            ", ".join(newly), STRICT_DEADLOCK_SECONDS, self._office_id,
        )
        return newly

    async def _escalate_strict_deadlock(self, agents: list[str]) -> None:
        """POST an office-level ``escalate_blocker`` action request so the
        user sees the strict-serialization wedge in the Inbox (not just a
        daemon log line). Best-effort: the CRITICAL log already fired, so a
        failed POST never blocks the loop. Uses the dispatcher's Company
        Token (the communicator-internal Bearer surface), the only auth it
        holds — distinct from the office-tool-secret the /tool-call path
        needs.
        """
        import httpx

        from src.backend_client import auth_headers

        url = (
            f"{self._backend_url}/api/offices/{self._office_id}"
            f"/action-requests/system-escalation"
        )
        body = {
            "reason": "strict_serialization_deadlock",
            "detail": (
                f"Agents {', '.join(agents)} have been strict-blocked on "
                f"ready tasks for over {int(STRICT_DEADLOCK_SECONDS)}s with "
                "nothing dispatching — a suspected one-task-per-agent "
                "serialization deadlock. Reprioritize, reassign, or unblock "
                "one of the held tasks to break it."
            ),
            "agents": agents,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url, json=body,
                    headers=auth_headers(self._security_token),
                )
            if resp.status_code not in (200, 201):
                logger.warning(
                    "strict-deadlock escalation POST returned %s for "
                    "office %s", resp.status_code, self._office_id,
                )
            else:
                # A 200 with ``created: false`` means no inbox card was
                # anchored (e.g. no workstream) — surface it so the
                # user-visible guarantee doesn't fail silently.
                try:
                    if resp.json().get("created") is False:
                        logger.warning(
                            "strict-deadlock escalation for office %s was "
                            "accepted but NO inbox card was created "
                            "(created=false); the CRITICAL log is the only "
                            "user signal.", self._office_id,
                        )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(
                "strict-deadlock escalation POST failed (office %s): %s — "
                "the CRITICAL log above remains the operator signal",
                self._office_id, exc,
            )

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

    async def _fetch_board_tasks(self) -> list[dict] | None:
        """Fetch ALL actionable tasks from the backend.

        T3.2.2 (07/G13 + 03/§4.8):

        * Returns ``None`` (NOT ``[]``) on any failure so callers can
          distinguish "empty board" from "fetch failed" — reconciling
          against a failed fetch wiped every per-agent queue on a
          transient backend 500 (S24).
        * Pages past the backend's response cap. The old single
          ``limit: 200`` request silently EVICTED every task beyond
          the first 200 from the queues each reconcile (S25); now we
          loop ``offset`` until a short page and reconcile against the
          full set. A failure on ANY page fails the whole fetch
          (a partial board would wipe the entries past the failed page).
        * 401/403 logs at ERROR (07/G20) — that's a revoked/parked
          Company Token, an operator-actionable condition, not a
          transient blip to bury at DEBUG/WARNING.
        """
        import httpx
        from src.backend_client import auth_headers

        backend_url = self._backend_url
        page_size = 200
        offset = 0
        items: list[dict] = []
        # Defensive cap: a backend that ignores ``offset`` and always
        # returns a full page would otherwise loop forever. 500 pages
        # (100k active tasks) is far above any real office; hitting it
        # means the backend is misbehaving, so fail the fetch (skip
        # reconcile) rather than spin.
        max_pages = 500
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                for _ in range(max_pages):
                    resp = await client.get(
                        f"{backend_url}/api/offices/{self._office_id}/tasks",
                        headers=auth_headers(self._security_token),
                        params={
                            "status": "ready,in_progress,review,blocked",
                            "limit": page_size,
                            "offset": offset,
                        },
                    )
                    if resp.status_code in (401, 403):
                        logger.error(
                            "Board fetch rejected with HTTP %d — the "
                            "Company Token was likely revoked or the "
                            "office re-parked. Re-pair the daemon "
                            "(cbcl setup) or check Office Settings → "
                            "Connection. Skipping queue reconcile.",
                            resp.status_code,
                        )
                        return None
                    if resp.status_code != 200:
                        logger.warning(
                            "Board fetch failed with HTTP %d at "
                            "offset %d — skipping queue reconcile "
                            "this cycle",
                            resp.status_code, offset,
                        )
                        return None
                    page = resp.json().get("items", [])
                    items.extend(page)
                    if len(page) < page_size:
                        return items
                    offset += page_size
                logger.error(
                    "Board fetch exceeded %d pages without a short page "
                    "(backend may be ignoring offset) — skipping queue "
                    "reconcile this cycle",
                    max_pages,
                )
        except Exception as exc:
            from src.utils import describe_exception
            logger.warning(
                "Failed to fetch board tasks: %s", describe_exception(exc),
            )
        return None

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

        Returns:

        * the status string on a 200;
        * ``None`` when the task itself is gone/missing (404) —
          callers treat this as "drop the queue entry and let the
          reconciler decide";
        * :data:`_STATUS_FETCH_FAILED` on a TRANSIENT failure
          (network error, backend 5xx, or a 401/403 token
          revoke/park) — callers re-queue the in-hand entry instead
          of dropping it (T3.2.2 / 03 #28). A 401/403 here mirrors
          the board-fetch posture: an auth blip is operator-
          actionable, not a reason to silently shed work in hand.
        """
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
                if resp.status_code in (401, 403) or resp.status_code >= 500:
                    return _STATUS_FETCH_FAILED
        except Exception as exc:
            logger.warning(
                "Failed to fetch task status for %s: %s",
                task_id[:8], exc,
            )
            return _STATUS_FETCH_FAILED
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
