"""Task Lifecycle Watchdog — crash recovery for stuck in-progress tasks.

Simplified watchdog that only handles crash recovery:
1. IN_PROGRESS tasks with no active agent session → re-queue for
   re-spawn-in-place (NO status flip — ``in_progress → ready`` is not
   a valid board transition); after 3 crashes → move to blocked (MA triage)
2. Ready tasks with assigned agent but not being worked on → wake dispatcher

Review and blocked task management is handled by the Manager Assistant
(Board Operator) via per-agent queues — NOT by the watchdog.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx


class HttpBoardClient:
    """HTTP adapter that mimics the WS client's request() interface.

    The watchdog was written for the WebSocket client but we now use
    HTTP (REST API) for all backend communication. This adapter lets
    the watchdog code work unchanged.
    """

    def __init__(
        self, platform_url: str, office_id: str, security_token: str = "",
    ) -> None:
        self._base = f"{platform_url.rstrip('/')}/api/offices/{office_id}"
        self._office_id = office_id
        # SEC3-01: the Company-Token bearer so the watchdog's /tool-call POSTs
        # (move_task on crash recovery) are accepted once auth is enforced.
        self._security_token = security_token

    @property
    def office_id(self) -> str:
        return self._office_id

    async def request(
        self, action: str, params: dict, timeout: float = 10.0,
    ) -> dict:
        from src.backend_client import auth_headers

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self._base}/tool-call",
                json={"action": action, "params": params},
                headers=auth_headers(self._security_token),
            )
            if resp.status_code == 200:
                return resp.json()
            return {"error": resp.text}


if TYPE_CHECKING:
    from src.config_sync.sync_service import ConfigStore
    from src.connection.ws_client import PlatformWSClient
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.orchestrator.manager_controller import ManagerController
    from src.orchestrator.task_dispatcher import TaskDispatcher

logger = logging.getLogger("cbcl.watchdog")

# Safety net interval — crash recovery check.
WATCHDOG_FALLBACK_INTERVAL = 30  # seconds
RECENTLY_DISPATCHED_TTL = 30  # seconds

# Tier-1 respawn cap: after this many crash-respawns of the same task,
# the watchdog escalates it to ``blocked`` (with its error class) instead
# of re-spawning again. The dispatcher reads this (via ``respawn_capped``)
# so its own reconcile-driven re-spawn path honors the same ceiling and
# can't leak extra CLI spawns past the cap (T8/1.1).
MAX_CRASH_RESPAWNS = 3

# How often to re-log "waking dispatcher for stuck ready tasks" at
# INFO when the SAME set of tasks is still stuck. Without this gate
# the watchdog fires every 30s and the log line scrolls forever for
# any task waiting on a dependency. In-between firings drop to DEBUG.
WATCHDOG_STATE_LOG_INTERVAL = 300.0  # 5 minutes


class TaskWatchdog:
    """Monitors the board for crash recovery only.

    Review and blocked task management is handled by the Manager Assistant
    via per-agent queues. The watchdog only handles:
    - In-progress tasks with no active agent (crash recovery)
    - Ready tasks that missed dispatch events
    """

    def __init__(
        self,
        ws: PlatformWSClient | None,
        executor: object | None,
        manager: ManagerController,
        config_store: ConfigStore,
        task_queue: object | None,
        office_id: str,
        supervisor: AgentSupervisor | None = None,
        dispatcher: TaskDispatcher | None = None,
    ) -> None:
        self._ws = ws
        self._manager = manager
        self._config = config_store
        self._office_id = office_id
        self._supervisor = supervisor
        self._dispatcher = dispatcher
        self._recently_dispatched: dict[str, float] = {}
        self._move_failed: dict[str, int] = {}
        self._task_crash_count: dict[str, int] = {}
        # Tasks already escalated to blocked by the circuit breaker.
        # Once the blocked move is issued, subsequent ticks must NOT
        # re-spawn or re-move the task — we wait for the move to land
        # on the board. Cleared (with the crash count) when the task
        # leaves in_progress, so a later genuine retry (e.g. the user
        # unblocks it) gets a fresh crash budget.
        self._blocked_escalated: set[str] = set()
        self._wake_event = asyncio.Event()
        # State throttle for the recurring "waking dispatcher for
        # stuck ready tasks" log. Maps a tuple-key of the stuck task
        # set to the last INFO-emit timestamp. When the set changes
        # (one becomes unstuck or a new one joins), the new tuple
        # is a fresh key and logs immediately.
        self._last_ready_log: dict[tuple[str, ...], float] = {}

    def wake(self) -> None:
        """Signal the watchdog to run an immediate check."""
        self._wake_event.set()

    async def run(self) -> None:
        """Main watchdog loop — event-driven with a fallback interval."""
        await asyncio.sleep(3)  # let other systems initialize

        while True:
            try:
                await self._check_board()
            except Exception as exc:
                logger.debug("Watchdog error: %s", exc)

            self._wake_event.clear()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=WATCHDOG_FALLBACK_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass

    async def _check_board(self) -> None:
        """Fetch board and handle crash recovery."""
        try:
            # Fetch ONLY the statuses the watchdog acts on — crash recovery on
            # `in_progress`, ready-dwell on `ready`. Filtering + a high limit
            # means the active set is never truncated by the default 100-row
            # page (a busy office's done/archived tail can't crowd out an
            # in_progress task), so the crash cap is enforced for every live
            # task regardless of board size.
            board = await self._ws.request(
                "get_board",
                {"status": "ready,in_progress", "limit": 1000},
                timeout=10.0,
            )
        except Exception:
            return

        items = board.get("items", [])

        in_progress = [t for t in items if t.get("status") == "in_progress"]
        in_progress_ids = {t.get("id", "") for t in in_progress}
        active_ids = {t.get("id", "") for t in items}

        # The crash/move counters track a LIVE `in_progress` execution attempt,
        # so they are scoped to `in_progress_ids`: the moment a task LEAVES
        # in_progress (completed → review, escalated → blocked, returned →
        # ready) its crash budget resets, so a later re-run (rework or
        # re-dispatch) starts fresh. Previously these were pruned by
        # `active_ids`, which kept the count alive across review/blocked and
        # force-blocked a reworked task after far fewer than the intended
        # crashes. The prune runs even when the active set is empty so an idle
        # office still resets stale budgets. `_recently_dispatched` is a
        # dispatch-grace guard for in_progress/ready tasks, so it stays scoped
        # to the active set.
        self._task_crash_count = {
            tid: v for tid, v in self._task_crash_count.items()
            if tid in in_progress_ids
        }
        self._move_failed = {
            tid: v for tid, v in self._move_failed.items()
            if tid in in_progress_ids
        }
        self._recently_dispatched = {
            tid: v for tid, v in self._recently_dispatched.items()
            if tid in active_ids
        }
        # Release circuit-breaker markers for tasks that left in_progress (the
        # blocked move landed, or the task moved on); the crash-count reset is
        # already handled by the in_progress-scoped prune above.
        for tid in list(self._blocked_escalated):
            if tid not in in_progress_ids:
                self._blocked_escalated.discard(tid)

        if not items:
            return

        # Crash recovery: in_progress tasks with no active agent
        for task in in_progress:
            await self._handle_in_progress(task)

        # Ready-dwell recovery: tasks stuck in Ready with an assigned_agent
        # that's idle but the dispatcher hasn't picked it up (e.g. enqueue
        # event was lost, queue desynced). Wake the dispatcher so it
        # reconciles and pulls the task.
        ready = [
            t for t in items
            if t.get("status") == "ready" and t.get("assigned_agent")
        ]
        if ready and self._dispatcher is not None:
            idle_ready = []
            for t in ready:
                agent = t.get("assigned_agent", "")
                if not agent:
                    continue
                if self._supervisor and self._supervisor.is_agent_busy(agent):
                    continue
                idle_ready.append(t.get("readable_id", "?"))
            if idle_ready:
                # Throttle: same stuck-set logged at INFO at most once
                # per WATCHDOG_STATE_LOG_INTERVAL; in-between firings
                # log at DEBUG so verbose tracing still shows them.
                # The dispatcher.wake() call ALWAYS runs — only the
                # log line is rate-limited.
                #
                # The key is a tuple of the SORTED set so any
                # churning subset (e.g. T143+T145 alternating with
                # T143) produces stable keys for stable states.
                # We prune entries older than 2× the interval to
                # bound memory growth on offices whose stuck-set
                # churns frequently.
                key = tuple(sorted(idle_ready))
                now = time.monotonic()
                last = self._last_ready_log.get(key, 0.0)
                if now - last >= WATCHDOG_STATE_LOG_INTERVAL:
                    self._last_ready_log[key] = now
                    logger.info(
                        "Watchdog: waking dispatcher — %d ready task(s) with idle agents: %s",
                        len(idle_ready), ", ".join(idle_ready[:5]),
                    )
                else:
                    logger.debug(
                        "Watchdog: waking dispatcher — %d ready task(s) with idle agents: %s",
                        len(idle_ready), ", ".join(idle_ready[:5]),
                    )
                # Opportunistic prune: same pattern as the dispatcher's
                # throttle. Runs once per ~128 ticks; stale entries are
                # already past the re-log window so dropping them is
                # lossless.
                if len(self._last_ready_log) & 0x7f == 0:
                    cutoff = now - (WATCHDOG_STATE_LOG_INTERVAL * 2)
                    stale = [
                        k for k, t in self._last_ready_log.items()
                        if t < cutoff
                    ]
                    for k in stale:
                        del self._last_ready_log[k]
                try:
                    self._dispatcher.wake()
                except Exception as exc:
                    logger.debug("Dispatcher wake failed: %s", exc)

    async def _handle_in_progress(self, task: dict) -> None:
        """Re-dispatch in_progress tasks that have no active agent session."""
        agent_name = task.get("assigned_agent", "")
        task_id = task.get("id", "")
        readable_id = task.get("readable_id", "?")

        if not agent_name:
            return

        # Skip tasks recently dispatched (race window).
        now = time.monotonic()
        dispatched_at = self._recently_dispatched.get(task_id)
        if dispatched_at is not None:
            if now - dispatched_at < RECENTLY_DISPATCHED_TTL:
                return
            del self._recently_dispatched[task_id]

        # Check if agent is working.
        if self._supervisor and self._supervisor.is_agent_busy(agent_name):
            return

        # Agent is NOT busy but task is in_progress → crash recovery.
        # Already escalated to blocked: the move was issued; wait for it
        # to land on the board (no further spawns or moves).
        if task_id in self._blocked_escalated:
            return

        crash_count = self._task_crash_count.get(task_id, 0)
        if crash_count >= MAX_CRASH_RESPAWNS:
            fail_count = self._move_failed.get(task_id, 0)
            if fail_count >= 3:
                return
            # Annotate the block comment with the last error class (if any)
            # so Manager Assistant has upstream context. The last `error`
            # activity carries a `details.error_class` when our retry loop
            # emitted it.
            classification_hint = await self._peek_last_error_class(task_id)
            error_class = classification_hint or "unknown_fatal"
            logger.warning(
                "Watchdog: %s crashed %d times, moving to blocked (class=%s)",
                readable_id, crash_count, error_class,
            )
            # Structured ESCALATED comment — the same template workers use
            # (``_agent_worker_task.py``), so the Manager Assistant's triage
            # playbook parses the class and routes accordingly.
            ok = await self._move_task(
                task_id,
                "blocked",
                "manager",
                (
                    f"ESCALATED ({error_class}): agent '{agent_name}' session "
                    f"died without completing the task after {crash_count} "
                    "re-spawn attempts.\n\n"
                    "Original error: see the most recent `error` activity "
                    "on this task (if any).\n\n"
                    "Manager Assistant: please investigate. Options typically "
                    "include splitting this task into smaller pieces, reducing "
                    "scope, or (for config/auth classes) asking the user to "
                    "resolve the underlying issue."
                ),
            )
            if ok:
                self._move_failed.pop(task_id, None)
                self._blocked_escalated.add(task_id)
            else:
                self._move_failed[task_id] = fail_count + 1
                # LOW-7: log the give-up LOUDLY exactly once — the
                # ``fail_count >= 3: return`` guard above short-circuits
                # every later tick silently, so this transition is the
                # only signal the user gets.
                if fail_count + 1 >= 3:
                    logger.warning(
                        "Watchdog: giving up on %s — the blocked move "
                        "failed %d times; task stays in_progress until "
                        "the Manager / board sweeper intervenes",
                        readable_id, fail_count + 1,
                    )
            return

        # Re-spawn in place. ``in_progress → ready`` is NOT a valid board
        # transition (the backend removed it — a ready bounce could strand
        # a live worker), so recovery is: re-add the task to the executor's
        # queue and wake the dispatcher (``dispatcher.add_task`` wakes
        # internally). An ``in_progress`` queue entry dispatches in execute
        # mode with NO status flip, so the agent simply resumes the task.
        # The dispatcher's 60s reconciler re-adds in_progress orphans too;
        # the explicit re-add here makes recovery immediate and is what the
        # crash counter below meters.
        self._task_crash_count[task_id] = crash_count + 1
        logger.warning(
            "Watchdog: %s stuck in_progress (agent '%s' idle) — re-queuing "
            "for re-spawn-in-place (crash %d/3)",
            readable_id, agent_name, crash_count + 1,
        )
        if self._dispatcher is not None:
            try:
                await self._dispatcher.add_task({
                    "task_id": task_id,
                    "readable_id": readable_id,
                    "assigned_agent": agent_name,
                    "status": "in_progress",
                    "priority": task.get("priority", "medium"),
                    "workstream_id": task.get("workstream_id", ""),
                    "scope_id": task.get("scope_id"),
                    "scope_state": task.get("scope_state"),
                })
            except Exception as exc:
                logger.warning(
                    "Watchdog re-queue failed for %s: %s", readable_id, exc,
                )
        # Grace window so the next tick doesn't double-count the same
        # crash while the dispatcher is still re-spawning.
        self._recently_dispatched[task_id] = time.monotonic()

    # ── Read-only crash-state accessors (consumed by the dispatcher) ──────
    # The watchdog is the single crash-metering authority. The dispatcher
    # re-spawns in_progress orphans on its own reconcile cadence too, so it
    # consults these to (a) stop re-spawning a task that's already hit the
    # cap / been escalated (T8/1.1), and (b) not arm the deadlock detector
    # against a holder that's merely under crash recovery (T8/2.1).

    def respawn_capped(self, task_id: str) -> bool:
        """True iff this task has exhausted its crash-respawn budget.

        Either already escalated to ``blocked`` by the watchdog, or its
        crash count has reached ``MAX_CRASH_RESPAWNS`` (so the next
        watchdog tick will escalate). The dispatcher must NOT re-spawn it.
        """
        return (
            task_id in self._blocked_escalated
            or self._task_crash_count.get(task_id, 0) >= MAX_CRASH_RESPAWNS
        )

    def is_crash_recovering(self, task_id: str) -> bool:
        """True iff this task is under active crash recovery (any crash
        counted, or escalated). A holder in this state is being recovered,
        not deadlocked — the dispatcher should not arm the deadlock timer
        on it."""
        return (
            task_id in self._blocked_escalated
            or self._task_crash_count.get(task_id, 0) > 0
        )

    async def _move_task(
        self, task_id: str, new_status: str, actor: str, comment: str,
    ) -> bool:
        """Move a task via the HTTP adapter."""
        try:
            result = await self._ws.request(
                "move_task",
                {
                    "task_id": task_id,
                    "new_status": new_status,
                    "actor": actor,
                    "comment": comment,
                },
                timeout=10.0,
            )
            return "error" not in result
        except Exception as exc:
            logger.warning("Watchdog move failed for %s: %s", task_id, exc)
            return False

    async def _peek_last_error_class(self, task_id: str) -> str | None:
        """Look up the most recent `error` activity on a task and return
        its structured ``details.error_class`` field, if any.

        The retry loop in agent_worker emits `error` activities with the
        classification attached. When the watchdog later decides to block
        a crashed task, we include that class in the block comment so
        MA has context (e.g. "OUTPUT_TOKEN_LIMIT" signals "try splitting
        the task" rather than generic "agent crashed").

        Returns None on any lookup failure — non-fatal, just means no hint.
        """
        try:
            result = await self._ws.request(
                "get_task_detail",
                {"task_id": task_id},
                timeout=5.0,
            )
            if not isinstance(result, dict) or "error" in result:
                return None
            activities = result.get("recent_activities") or []
            # recent_activities is newest-last per task_service enrichment;
            # walk in reverse to find the most recent `error` event.
            for act in reversed(activities):
                if act.get("event_type") != "error":
                    continue
                details = act.get("details") or {}
                cls = details.get("error_class")
                if isinstance(cls, str) and cls:
                    return cls
                # Fallback: classify the content text if details missing.
                content = act.get("content") or ""
                if content:
                    try:
                        from src.orchestrator.error_classifier import (
                            classify_error,
                        )
                        return classify_error(content).error_class.value
                    except Exception:
                        return None
            return None
        except Exception as exc:
            logger.debug(
                "Peek last error class failed for %s: %s", task_id, exc,
            )
            return None
