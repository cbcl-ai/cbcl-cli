"""Task Lifecycle Watchdog — crash recovery for stuck in-progress tasks.

Simplified watchdog that only handles crash recovery:
1. IN_PROGRESS tasks with no active agent session → move to blocked/ready
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

    def __init__(self, platform_url: str, office_id: str) -> None:
        self._base = f"{platform_url.rstrip('/')}/api/offices/{office_id}"
        self._office_id = office_id

    @property
    def office_id(self) -> str:
        return self._office_id

    async def request(
        self, action: str, params: dict, timeout: float = 10.0,
    ) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self._base}/tool-call",
                json={"action": action, "params": params},
            )
            if resp.status_code == 200:
                return resp.json()
            return {"error": resp.text}

    async def send(self, msg: dict) -> None:
        """Fire-and-forget send (used by watchdog for status updates)."""
        msg_type = msg.get("type", "")
        if msg_type == "task_status_update":
            await self.request("move_task", {
                "task_id": msg.get("task_id", ""),
                "new_status": msg.get("new_status", ""),
                "actor": msg.get("agent_name", "manager"),
                "comment": msg.get("comment", ""),
            })

    async def safe_send(self, msg: dict, context: str = "") -> None:
        """Send with error swallowing."""
        try:
            await self.send(msg)
        except Exception:
            pass

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
            board = await self._ws.request("get_board", {}, timeout=10.0)
        except Exception:
            return

        items = board.get("items", [])
        if not items:
            return

        # Prune tracking dicts
        active_ids = {t.get("id", "") for t in items}
        self._task_crash_count = {
            tid: v for tid, v in self._task_crash_count.items() if tid in active_ids
        }
        self._move_failed = {
            tid: v for tid, v in self._move_failed.items() if tid in active_ids
        }
        self._recently_dispatched = {
            tid: v for tid, v in self._recently_dispatched.items() if tid in active_ids
        }

        in_progress = [t for t in items if t.get("status") == "in_progress"]

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
        crash_count = self._task_crash_count.get(task_id, 0)
        if crash_count >= 3:
            fail_count = self._move_failed.get(task_id, 0)
            if fail_count >= 3:
                return
            # Annotate the block comment with the last error class (if any)
            # so Manager Assistant has upstream context. The last `error`
            # activity carries a `details.error_class` when our retry loop
            # emitted it.
            classification_hint = await self._peek_last_error_class(task_id)
            hint_suffix = (
                f" Last observed error class: {classification_hint}."
                if classification_hint
                else ""
            )
            logger.warning(
                "Watchdog: %s crashed %d times, moving to blocked%s",
                readable_id, crash_count,
                f" (class={classification_hint})" if classification_hint else "",
            )
            ok = await self._move_task(
                task_id,
                "blocked",
                "manager",
                (
                    f"System: agent '{agent_name}' session ended without completing "
                    f"the task after {crash_count} attempts. Moved to blocked."
                    f"{hint_suffix} Manager Assistant: please investigate — "
                    "options include splitting the task, reducing scope, or "
                    "refreshing credentials if this is an auth class."
                ),
            )
            if ok:
                self._move_failed.pop(task_id, None)
            else:
                self._move_failed[task_id] = fail_count + 1
            return

        # Move back to Ready for re-dispatch.
        logger.warning(
            "Watchdog: %s stuck in_progress (agent '%s' idle) -> moving to ready",
            readable_id, agent_name,
        )
        self._task_crash_count[task_id] = crash_count + 1

        fail_count = self._move_failed.get(task_id, 0)
        ok = await self._move_task(
            task_id,
            "ready",
            "manager",
            f"System: agent '{agent_name}' session ended. Re-queuing for pickup.",
        )
        if ok:
            self._move_failed.pop(task_id, None)
            self._recently_dispatched[task_id] = time.monotonic()
        else:
            self._move_failed[task_id] = fail_count + 1

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
