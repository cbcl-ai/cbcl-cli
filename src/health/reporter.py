"""Health reporter -- writes periodic health data to Redis.

Every 15 seconds (``DEFAULT_REPORT_INTERVAL``), builds a health report JSON
and writes it to the Redis key ``office:{oid}:health`` with a 120-second TTL.
The backend reads this key to serve the ``GET /api/offices/{oid}/status``
endpoint.

If the Orchestrator dies or the health reporter stops, the key expires
after 120 seconds and the backend reports the office as disconnected.

When Redis is not available (Phase 1 / backward-compat mode), the
reporter falls back to sending health reports via WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any  # Any used for manager_controller param

from src.config import get_api_key

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from src.config_sync.sync_service import ConfigStore
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.orchestrator.session_manager import SessionManager
    from src.orchestrator.task_dispatcher import TaskDispatcher
    from src.scripts.script_runner import ScriptRunner

logger = logging.getLogger(__name__)

# Captured at import time so we can compute process uptime.
_PROCESS_START_TIME = time.monotonic()

# TTL for the health key in Redis (seconds).
# If no report is written for this long, the key expires and the backend
# considers the office disconnected.
HEALTH_KEY_TTL_SECONDS = 120

# Default report interval (seconds).
DEFAULT_REPORT_INTERVAL = 15.0


def _wire_agent_status(state_value: str) -> str:
    """Map a supervisor agent state to the ws-protocol agent-status enum
    {idle, working, error} (T8.3.4 / 03/#22).

    ``crashed``/``error`` surface as ``error`` so a dead agent is visible in
    the connection panel — previously every non-``working`` state (including
    ``crashed``) collapsed to ``idle``, hiding crashes. ``spawning``/``ready``
    are transient non-error states → ``idle``.
    """
    if state_value == "working":
        return "working"
    if state_value in ("crashed", "error"):
        return "error"
    return "idle"


class HealthReporter:
    """Writes health data to Redis every 15 seconds (``DEFAULT_REPORT_INTERVAL``).

    When Redis is available (Phase 2), writes to ``office:{oid}:health``.
    When only WebSocket is available (Phase 1), sends via ``ws.send()``.
    """

    def __init__(
        self,
        redis: Redis | None = None,
        office_id: str = "",
        supervisor: AgentSupervisor | None = None,
        dispatcher: TaskDispatcher | None = None,
        session_manager: SessionManager | None = None,
        script_runner: ScriptRunner | None = None,
        config_store: ConfigStore | None = None,
        interval: float = DEFAULT_REPORT_INTERVAL,
        transport: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self._redis = redis
        self._transport = transport  # WsTransport
        self._office_id = office_id
        self._supervisor = supervisor
        self._dispatcher = dispatcher
        self._sessions = session_manager
        self._script_runner = script_runner
        self._config = config_store
        self._interval = interval
        self._task: asyncio.Task | None = None
        # First-publish-failure tolerance. The health reporter starts
        # before/during the WS connection setup; the very first
        # publish often races with the connector handshake and lands
        # while the transport is still in "Not connected" state.
        # That's a benign startup race, not an operator-actionable
        # WARNING. Demote the first failure to DEBUG; bump back to
        # WARNING once we've succeeded at least once.
        self._has_published_once: bool = False

        # Redis key for this office's health data
        self._health_key = f"office:{office_id}:health"

    def start(self) -> None:
        """Start the periodic health reporting loop."""
        self._task = asyncio.create_task(self._report_loop())

    def stop(self) -> None:
        """Stop the health reporting loop."""
        if self._task:
            self._task.cancel()
            self._task = None

    async def send_report(self) -> None:
        """Build and write a single health report to Redis (or WebSocket)."""
        report = await self._build_report()

        # Write health data to Redis keys (health cache + presence)
        if self._redis is not None:
            try:
                report_json = json.dumps(report, default=str)
                await self._redis.set(
                    self._health_key,
                    report_json,
                    ex=HEALTH_KEY_TTL_SECONDS,
                )

                # Refresh the orchestrator presence key so the chat
                # gateway knows the communicator is online.  This key
                # has a 60s TTL and is checked on every chat message.
                presence_key = f"connections:{self._office_id}:orchestrator"
                await self._redis.hset(presence_key, mapping={
                    "registered": "true",
                    "last_report": report_json[:500],
                })
                await self._redis.expire(presence_key, 60)

            except Exception as exc:
                logger.warning("Failed to write health to Redis: %s", exc)

        # Publish health_report event via WebSocket transport
        if self._transport is not None:
            try:
                await self._transport.publish_event(report)
            except Exception as exc:
                if not self._has_published_once:
                    # First-ever publish raced with the WS connect
                    # handshake — benign startup state, not WARNING-
                    # worthy. After one successful publish (next
                    # interval, when the WS is up) failures bump
                    # back to WARNING so a real disconnect is loud.
                    logger.debug(
                        "Initial health publish raced WS connect "
                        "(expected during startup): %s", exc,
                    )
                else:
                    logger.warning(
                        "Failed to publish health via transport: %s",
                        exc,
                    )
            else:
                self._has_published_once = True

    async def _report_loop(self) -> None:
        """Send health report on interval. First report is immediate."""
        try:
            # Send first report immediately so presence key exists right away
            try:
                await self.send_report()
            except Exception as exc:
                logger.warning("First health report failed: %s", exc)

            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self.send_report()
                except Exception as exc:
                    logger.warning(
                        "Failed to send health report: %s", exc,
                    )
        except asyncio.CancelledError:
            pass

    async def _build_report(self) -> dict[str, Any]:
        """Build a health report dictionary.

        Reads agent statuses from the AgentSupervisor (Phase 2) or
        TaskQueue (Phase 1 fallback), running scripts from the
        ScriptRunner, queue size from the TaskDispatcher, and session
        info from the SessionManager.
        """
        # Active Manager sessions
        active_sessions: dict[str, str] = {}
        if self._sessions:
            active_sessions = self._sessions.manager_sessions

        # Agent statuses — prefer supervisor (Phase 2), fall back to
        # task_queue + config (Phase 1)
        agent_statuses: dict[str, dict] = {}
        if self._supervisor:
            all_states = self._supervisor.get_all_statuses()
            for agent_name, state_info in all_states.items():
                if isinstance(state_info, dict):
                    state_value = state_info.get("status", "idle")
                    current_task = state_info.get("current_task")
                    agent_statuses[agent_name] = {
                        "status": _wire_agent_status(state_value),
                        "current_task": current_task,
                    }
                else:
                    agent_statuses[agent_name] = {
                        "status": _wire_agent_status(str(state_info)),
                        "current_task": None,
                    }
        else:
            # Fallback: build from config (no supervisor available)
            if self._config:
                for agent_cfg in self._config.agents:
                    name = agent_cfg.get("name", "")
                    if name and agent_cfg.get("is_active", True):
                        agent_statuses[name] = {
                            "status": "idle",
                            "current_task": None,
                        }
                agent_statuses["manager"] = {
                        "status": "idle",
                        "current_task": "",
                    }

        # Queue sizes — total and per-agent
        queue_size = 0
        per_agent_queues: dict[str, int] = {}
        if self._dispatcher:
            queue_size = await self._dispatcher.get_queue_size()
            if hasattr(self._dispatcher, "_qm"):
                per_agent_queues = await self._dispatcher._qm.get_all_queue_sizes()

        # Running scripts
        running_scripts: list[dict] = []
        if self._script_runner:
            running_scripts = await self._script_runner.get_running_scripts()

        return {
            "type": "health_report",
            "office_id": self._office_id,
            # T8.3.4: this reports whether an API key is CONFIGURED, not that
            # it's valid — real validity is the ``auth_status`` RPC's job
            # (a live ``claude --print`` round-trip). The wire field name is
            # kept (the frontend consumes ``api_key_valid``); the rename to
            # ``api_key_configured`` is flagged for the Phase 9 ws-protocol pass.
            "api_key_valid": bool(get_api_key()),
            "sdk_version": _get_sdk_version(),
            # Process uptime (kept as "container_uptime" for protocol compat)
            "container_uptime": round(
                time.monotonic() - _PROCESS_START_TIME, 1
            ),
            "active_sessions": active_sessions,
            "agent_statuses": agent_statuses,
            "running_scripts": running_scripts,
            "queue_size": queue_size,
            "per_agent_queues": per_agent_queues,
            "errors": [],
        }


def _get_sdk_version() -> str:
    """Return the Claude Agent SDK version if installed."""
    try:
        import claude_agent_sdk

        return getattr(claude_agent_sdk, "__version__", "unknown")
    except ImportError:
        return "not-installed"
