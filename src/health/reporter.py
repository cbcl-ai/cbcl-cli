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
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any  # Any used for manager_controller param

from src.utils import get_daemon_version

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

# The installed cbcl package version, resolved once at import (it can't
# change while the daemon runs). Shipped in every health_report as
# ``daemon_version`` so the platform's Connection tab can show which
# daemon build serves the office.
_DAEMON_VERSION = get_daemon_version()

# TTL for the health key in Redis (seconds).
# If no report is written for this long, the key expires and the backend
# considers the office disconnected.
HEALTH_KEY_TTL_SECONDS = 120

# Default report interval (seconds).
DEFAULT_REPORT_INTERVAL = 15.0

# Flow Studio (FS-P2.T10, daemon half): capability flags the backend
# gates features on. ``flow_studio`` = this daemon understands the
# ``flow_block_execute`` connector command and emits
# ``flow_block_result``, so it can serve flow runs; the backend
# refuses ``POST /api/offices/{oid}/flow-runs`` for offices whose
# latest health report lacks the flag (spec §12 — graceful degrade:
# pre-Flow-Studio daemons simply never send the field).
# ``instructions_v2`` (instruction-surfaces D6, cbcl 0.5.8) = the
# generation RPCs understand ``sources`` (scoped source survey),
# workstream ``mode=improve`` + ``current_notes``, and return the
# ``changes`` report; the backend refuses the new request fields with
# a teaching 400 naming the cbcl upgrade when the connected daemon
# lacks the flag.
# ``memory_v1`` (office-memory v1) = this daemon's catalogs serve the
# recall/remember tools, its prompt builders render the injected
# memory indexes fenced, and it runs the on-connect learnings.md
# import. The backend includes the memory index fields
# UNCONDITIONALLY (old daemons ignore unknown fields — the
# ``effort_hint`` precedent), so this flag is observability, not a
# gate, in v1.
# ``office_work_policy_v1`` (F09) = this daemon renders the Office work policy
# (``sync_config.work_policy``) into every agent directory CLAUDE.md, the
# Manager reference section, and retained task-Agent snapshots
# (``office_work_policy`` snapshot key). Older daemons ignore both fields, so
# the backend reports ``OfficeStatus.work_policy_delivery`` and Settings warns.
DAEMON_CAPABILITIES: tuple[str, ...] = (
    "flow_studio",
    "instructions_v2",
    "memory_v1",
    "office_work_policy_v1",
)

# Most task ids one health report lists in ``active_capacity_wait_task_ids``
# and ``capacity_waits`` (the ledger read pages at 100 rows). A capacity wait
# is an operator-enabled, same-host budget, so real offices park far fewer
# tasks than this.
CAPACITY_WAIT_REPORT_LIMIT = 100

# Most tasks one health report lists in ``script_handoff_waits`` (newest
# handoffs first, so a cap drops the oldest).
SCRIPT_HANDOFF_WAIT_REPORT_LIMIT = 100

# The synthetic consult-session id prefixes the spawn sites in
# ``src/handlers.py`` mint for the three CONSULT-ONLY agents:
# ``planner-<hex12>`` (``consult_planner``) and ``flow-consult-<hex12>``
# (``consult_flow_architect`` / ``consult_data_curator``). These ids
# have NO backend ``tasks`` row behind them, so a health report that
# carries one as ``current_task`` invites clients into a board
# deep-link that can only 422 — the report OMITS them instead (the
# agent still shows ``working``). The frontend keeps its own UUID-shape
# guard for daemons older than this change
# (``frontend_v2.1/src/lib/taskId.ts`` shares this vocabulary).
SYNTHETIC_CONSULT_TASK_ID_PREFIXES: tuple[str, ...] = (
    "planner-",
    "flow-consult-",
)


def _is_synthetic_consult_task_id(task_id: object) -> bool:
    """True when ``task_id`` is a daemon-minted consult-session id."""
    return isinstance(task_id, str) and task_id.startswith(
        SYNTHETIC_CONSULT_TASK_ID_PREFIXES
    )


def _timed_waits(
    waits: Iterable[tuple[object, object]], limit: int,
) -> list[dict[str, str]]:
    """``[{task_id, waiting_since}]`` for ``(task_id, epoch seconds)`` pairs.

    One entry per task (its earliest start), ``waiting_since`` as ISO-8601
    UTC, sorted by task id and capped at ``limit``. A pair without a task id
    or a numeric time is left out: the backend bounds how long a listed wait
    suppresses its alarms, so it must never see a made-up start.
    """
    earliest: dict[str, float] = {}
    for task_id, since in waits:
        if not task_id or not isinstance(since, int | float):
            continue
        key = str(task_id)
        earliest[key] = min(since, earliest.get(key, since))
    return [
        {
            "task_id": task_id,
            "waiting_since": datetime.fromtimestamp(since, UTC).isoformat(),
        }
        for task_id, since in sorted(earliest.items())[:limit]
    ]


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
        limits_reconciler: Any | None = None,
        datastore: Any | None = None,
        runtime_state: Any | None = None,
        office_slug: str = "",
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
        # Optional ResourceLimitReconciler — the report loop doubles
        # as the "existing periodic machinery" that re-checks a
        # DEFERRED container-limits recreate until the office goes
        # idle (``recheck_pending`` is a no-op unless one is pending).
        self._limits_reconciler = limits_reconciler
        # Optional OfficeDatastore (Flow Studio FS-P1): per-collection
        # row counts ride the heartbeat so the backend can refresh its
        # cached ``collections.row_count``.
        self._datastore = datastore
        self._runtime_state = runtime_state
        self._office_slug = office_slug
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
        self.quota_recovery = None
        self._health_key = f"office:{office_id}:health"

    def start(self) -> None:
        """Start the periodic health reporting loop."""
        self._task = asyncio.create_task(self._report_loop())

    def stop(self) -> None:
        """Stop the health reporting loop."""
        if self.quota_recovery is not None:
            self.quota_recovery.stop()
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
                # Piggyback the deferred container-limits recheck on
                # this tick (see __init__). Isolated so a reconcile
                # error can never kill the health loop.
                if self._limits_reconciler is not None:
                    try:
                        await self._limits_reconciler.recheck_pending()
                    except Exception as exc:
                        logger.warning(
                            "Deferred resource-limit recheck failed: %s",
                            exc,
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
        if self.quota_recovery is not None:
            self.quota_recovery.request_check()

        # Active Manager sessions
        if self._office_slug:
            try:
                from src.office_secrets.transient import purge_expired_inputs

                await asyncio.to_thread(purge_expired_inputs, self._office_slug)
            except Exception:
                logger.warning("Expired transient input cleanup is unavailable")
        active_sessions: dict[str, str] = {}
        if self._sessions:
            active_sessions = self._sessions.manager_sessions

        # Agent statuses — prefer supervisor (Phase 2), fall back to
        # task_queue + config (Phase 1)
        agent_statuses: dict[str, dict] = {}
        agent_instances: dict[str, dict] = {}
        capabilities = list(DAEMON_CAPABILITIES)
        policy_metadata = {}
        if self._supervisor:
            from src._handlers._instance_telemetry import wire_instance_status

            instances = self._supervisor.get_instance_statuses()
            if isinstance(instances, dict):
                agent_instances = {
                    key: wire_instance_status(value) for key, value in instances.items()
                }
            if self._supervisor.config_ready is True:
                capabilities.append("dynamic_agents_v1")
                policy_metadata["agent_execution_policy"] = (
                    self._supervisor.execution_policy
                )
            all_states = self._supervisor.get_all_statuses()
            for agent_name, state_info in all_states.items():
                if isinstance(state_info, dict):
                    state_value = state_info.get("status", "idle")
                    current_task = state_info.get("current_task")
                    if _is_synthetic_consult_task_id(current_task):
                        # A consult session's synthetic id — no board
                        # task exists behind it, so it never rides the
                        # wire (see the prefix constant above).
                        current_task = None
                    agent_statuses[agent_name] = {
                        "status": _wire_agent_status(state_value),
                        "current_task": current_task,
                    }
                    for pending_field in (
                        "execution_cleanup_failed", "execution_finalization_pending",
                    ):
                        if state_info.get(pending_field) is True:
                            agent_statuses[agent_name]["status"] = "error"
                            agent_statuses[agent_name][pending_field] = True
                    if state_info.get("execution_cleanup_pending") is True:
                        agent_statuses[agent_name]["execution_cleanup_pending"] = True
                    for count_field in ("running_count", "retained_count"):
                        if count_field in state_info:
                            agent_statuses[agent_name][count_field] = state_info[
                                count_field
                            ]
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

        # C3a-G3: tasks parked on an ACCEPTED capacity wait (a task-owned
        # ``execute_script`` that returned HTTP 202 ``waiting_for_capacity``).
        # The worker ended its session on purpose and nothing runs until the
        # daemon resumes the same phase, so the task writes no activity. The
        # backend board sweeper treats these like ``running_scripts`` task ids
        # while this heartbeat is fresh. Read from the durable runtime ledger;
        # best-effort, bounded, and never breaks the report.
        # U14: ``capacity_waits`` adds when each wait began, so the backend
        # can stop suppressing alarms for a wait that never becomes eligible.
        # The id list stays for backends that read only it.
        capacity_wait_task_ids: list[str] = []
        capacity_waits: list[dict[str, str]] = []
        # C3a-G3: tasks parked on a managed-script handoff whose resume is not
        # yet admitted: the script is still running, or it finished and the
        # same phase waits for its agent (a busy executor keeps it queued).
        script_handoff_waits: list[dict[str, str]] = []
        if self._runtime_state is not None:
            try:
                waits = self._runtime_state.active_capacity_waits(
                    limit=CAPACITY_WAIT_REPORT_LIMIT
                )
                capacity_wait_task_ids = sorted(
                    {str(wait["task_id"]) for wait in waits if wait.get("task_id")}
                )[:CAPACITY_WAIT_REPORT_LIMIT]
                capacity_waits = _timed_waits(
                    (
                        (
                            wait.get("task_id"),
                            wait.get("waiting_since") or wait.get("updated_at"),
                        )
                        for wait in waits
                    ),
                    CAPACITY_WAIT_REPORT_LIMIT,
                )
            except Exception as exc:
                logger.debug("Capacity waits unavailable for health report: %s", exc)
            try:
                script_handoff_waits = _timed_waits(
                    (
                        (handoff.get("task_id"), handoff.get("parked_at"))
                        for handoff in self._runtime_state.parked_script_handoffs(
                            limit=SCRIPT_HANDOFF_WAIT_REPORT_LIMIT
                        )
                    ),
                    SCRIPT_HANDOFF_WAIT_REPORT_LIMIT,
                )
            except Exception as exc:  # noqa: BLE001 — a ledger read never breaks the report
                logger.debug(
                    "Script handoff waits unavailable for health report: %s", exc
                )

        runtime_metadata = {}
        if self._runtime_state is not None:
            try:
                active_workers = self._supervisor.active_execution_count() if self._supervisor else 0
                active_scripts = self._script_runner.active_execution_count() if self._script_runner else 0
                self._runtime_state.snapshot(active_workers, active_scripts)
                runtime_metadata["maintenance"] = self._runtime_state.maintenance_status()
                from src.quota_recovery import public_quota_status
                runtime_metadata["execution_status"] = public_quota_status(self._runtime_state.quota_status())
            except Exception:
                logger.exception("Runtime maintenance acknowledgment unavailable")
                runtime_metadata["maintenance"] = {"state": "unknown"}

        # Flow Studio (FS-P1): per-collection row counts from the
        # office-local datastore — {collection_name: count} over the
        # SYNCED collection names (0 when no rows yet). Best-effort:
        # a datastore read failure must never break the health report.
        collections: dict[str, int] = {}
        if self._datastore is not None:
            try:
                collections = await self._datastore.collection_counts()
            except Exception as exc:
                logger.debug(
                    "Collection counts unavailable for health report: %s",
                    exc,
                )

        return {
            **runtime_metadata,
            **policy_metadata,
            "type": "health_report",
            "office_id": self._office_id,
            "sdk_version": _get_sdk_version(),
            # The cbcl daemon's own installed version (importlib
            # metadata of ``cubicle-communicator``) — distinct from
            # ``sdk_version`` (host-side claude-agent-sdk package)
            # and from the in-container Claude CLI (the opt-in
            # ``cli-version`` probe). Backend persists it on
            # ConnectorStatus and surfaces it in the Connection tab.
            "daemon_version": _DAEMON_VERSION,
            # Process uptime (kept as "container_uptime" for protocol compat)
            "container_uptime": round(time.monotonic() - _PROCESS_START_TIME, 1),
            "active_sessions": active_sessions,
            "agent_statuses": agent_statuses,
            "agent_instances": agent_instances,
            "running_scripts": running_scripts,
            "active_capacity_wait_task_ids": capacity_wait_task_ids,
            "capacity_waits": capacity_waits,
            "script_handoff_waits": script_handoff_waits,
            "queue_size": queue_size,
            "per_agent_queues": per_agent_queues,
            # Flow Studio (FS-P1): {collection_name: row_count} for the
            # backend's cached ``collections.row_count`` refresh. Empty
            # when no collections are synced (or no datastore is wired).
            "collections": collections,
            # Flow Studio (FS-P2.T10): daemon capability flags — see
            # ``DAEMON_CAPABILITIES``. The backend's flow-run start
            # gate checks for ``"flow_studio"`` in this list.
            "capabilities": capabilities,
            "errors": (
                [self._supervisor.config_sync_error]
                if self._supervisor is not None
                and isinstance(getattr(self._supervisor, "config_sync_error", None), str)
                and self._supervisor.config_sync_error
                else []
            ),
        }


def _get_sdk_version() -> str:
    """Return the Claude Agent SDK version if installed."""
    try:
        import claude_agent_sdk

        return getattr(claude_agent_sdk, "__version__", "unknown")
    except ImportError:
        return "not-installed"
