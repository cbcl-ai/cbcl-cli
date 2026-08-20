"""agent_supervisor.py -- Manages agent subprocess lifecycle.

Spawns one OS process per agent session. Each process runs agent_worker.py
with its own event loop and MCP servers. The supervisor monitors all processes,
routes IPC messages, and handles crash recovery.

Key design decisions:
- One process per agent (crash isolation, no MCP contention).
- Per-agent asyncio.Lock prevents race conditions on concurrent spawn/kill.
- Dedicated _reader_loop per process prevents pipe deadlock (Amendment C-2).
- Heartbeat: PING every 30s, kill after 90s no response (Amendment A4).
- Agent state machine: IDLE -> SPAWNING -> READY -> WORKING -> IDLE/CRASHED.

Usage:
    supervisor = AgentSupervisor(
        workspace_path="/workspace",
        office_id="uuid",
        on_event=handle_agent_event,
    )
    await supervisor.spawn_manager(manager_config)
    await supervisor.spawn_worker("analyst", agent_config, task_data)
    await supervisor.shutdown(timeout=30)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger("cbcl.supervisor")

# Maximum time to wait for a process to become ready after spawning.
SPAWN_TIMEOUT_SECONDS = 30

# Maximum time to wait for a process to exit after sending shutdown.
SHUTDOWN_GRACE_SECONDS = 30

# Amendment A4: Heartbeat configuration.
# Effective upper bound for how long an unresponsive agent may live
# before the supervisor treats it as dead. With the current PID-based
# check the supervisor detects death on the next PING after the pipe
# breaks (i.e. one HEARTBEAT_INTERVAL at most), but documenting the
# 3× interval ceiling matches the amendment-A4 contract and gives
# tests a single constant to assert on. Do NOT compute this inline —
# tuning either knob alone should be a one-line change.
HEARTBEAT_TIMEOUT_SECONDS = 90
# Orchestrator sends PING every HEARTBEAT_INTERVAL_SECONDS to verify
# the stdin pipe is open.
HEARTBEAT_INTERVAL_SECONDS = 30

# Maximum concurrent agent processes per office.
DEFAULT_MAX_AGENTS = int(os.environ.get("CUBICLE_MAX_AGENTS", "20"))

# asyncio StreamReader buffer limit for agent IPC over stdin/stdout.
# Default 64KB would truncate large response_chunk/progress messages —
# a single text block from Claude can easily exceed this. 16MB bounds
# memory while covering realistic payloads.
_STREAM_LIMIT = 16 * 1024 * 1024


class AgentState(str, Enum):
    """Agent subprocess state machine states.

    IDLE:     No process running. Ready to accept next task.
    SPAWNING: Process started, waiting for "ready" message.
    READY:    Process running, waiting for task assignment.
    WORKING:  Process running, executing a task or chat query.
    CRASHED:  Process exited unexpectedly. After recovery, returns to IDLE.
    """

    IDLE = "idle"
    SPAWNING = "spawning"
    READY = "ready"
    WORKING = "working"
    CRASHED = "crashed"


@dataclass
class AgentProcess:
    """Tracks one agent subprocess.

    Contains all state needed to manage the subprocess lifecycle:
    process handle, state, current task, timing, and background tasks.
    """

    agent_name: str
    role: str  # "manager" or "worker"
    state: AgentState = AgentState.IDLE
    process: asyncio.subprocess.Process | None = None
    pid: int | None = None
    current_task_id: str | None = None
    current_readable_id: str | None = None
    # T1.1.8 (G19): frozen copy of the task in flight at the moment
    # ``_kill_process`` reset this record. ``_monitor_exit`` races the
    # killer's continuation — if the kill's state reset lands before the
    # monitor reads ``current_task_id``, the synthesized fatal error
    # event would carry ``task_id=None`` and the crash-recovery routing
    # in handlers.py (gated on ``if task_id:``) silently skips, leaving
    # recovery to the 60s reconciler. Set ONLY when a task was actually
    # in flight at kill time, so a task_complete (which clears
    # ``current_task_id``) followed by a late kill/exit never resurrects
    # a finished task.
    killed_task_id: str | None = None
    # Event-hygiene (Issue 4): set by ``_kill_process`` so ``_monitor_exit``
    # knows the (non-zero) exit was killer-initiated — the killer already
    # reset the record to IDLE, and a later CRASHED overwrite would be a
    # misleading state read.
    kill_initiated: bool = False
    # Event-hygiene (Issue 4): set by the heartbeat loop after it emits the
    # fatal heartbeat_timeout error for the in-flight task, so
    # ``_monitor_exit`` doesn't emit a SECOND fatal error event for the
    # same process+task when the killed process's exit is observed.
    fatal_error_emitted: bool = False
    started_at: float = 0.0
    # `last_message_at` is informational — every reader-loop message
    # bumps it, including PONG. Used by debug-status surfaces, not
    # by the heartbeat (which uses `last_pong_at`).
    last_message_at: float = 0.0
    # P6.10 v2 (review): the heartbeat uses `last_pong_at` directly
    # to distinguish "agent is wedged" from "agent is busy in a long
    # Claude call". `last_pong_at` is bumped whenever the reader
    # loop receives a `pong` message (and as a seeding gesture by
    # the `ready` handshake). If `now - last_pong_at` exceeds the
    # timeout we kill — guaranteed liveness signal independent of
    # whether the agent is emitting progress events.
    last_pong_at: float = 0.0
    reader_task: asyncio.Task | None = None
    monitor_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None
    exit_code: int | None = None


# Type for event callback: (agent_name, message_dict) -> None
EventCallback = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class AgentSupervisor:
    """Manages agent subprocesses for one office.

    The supervisor is the Orchestrator's core abstraction for process
    management. It spawns, monitors, and communicates with agent
    subprocesses. Each agent runs agent_worker.py in its own OS process.

    Thread safety: All methods are coroutines that run on the Orchestrator's
    asyncio event loop. Per-agent asyncio.Lock prevents race conditions
    when multiple dispatch cycles try to spawn/kill the same agent.

    Attributes:
        _workspace: Path to the workspace directory.
        _office_id: The office ID this supervisor manages.
        _backend_url: URL of the platform backend.
        _container_name: Docker container name for this office.
        _max_agents: Maximum concurrent agent processes.
        _on_event: Callback for agent events (forwarded to message router).
        _agents: Dict of agent_name -> AgentProcess.
        _locks: Dict of agent_name -> asyncio.Lock.
    """

    def __init__(
        self,
        workspace_path: str,
        office_id: str,
        backend_url: str = "",
        container_name: str = "",
        max_agents: int = DEFAULT_MAX_AGENTS,
        on_event: EventCallback | None = None,
        _agent_command: list[str] | None = None,
    ) -> None:
        self._workspace = workspace_path
        self._office_id = office_id
        self._backend_url = backend_url
        self._container_name = container_name
        self._max_agents = max_agents
        self._on_event = on_event

        # Per-office tool-proxy URL + bearer token. Set via
        # set_tool_proxy() once the ToolProxyServer has started (it
        # binds to a random OS-assigned port and mints a random
        # token). Each office has its OWN proxy — passed explicitly
        # to spawned workers rather than via shared os.environ vars
        # (which would get overwritten when a second office starts,
        # cross-wiring tool calls to the wrong office's WS).
        self._tool_proxy_url: str = ""
        self._tool_proxy_token: str = ""
        # Narrow collections-only proxy token (spec ui-ux-aug19 D4.2)
        # — set alongside the pair above via set_tool_proxy().
        self._collections_token: str = ""

        # Per-office /tool-call capability secret (SEC3-01). Handed to us by
        # the backend in sync_config; threaded into each spawned agent's MCP
        # env so the in-container MCP server can authenticate its DIRECT
        # (non-proxy) tool-call POSTs to the backend. Empty until the first
        # sync_config arrives — the proxy→WS path doesn't need it.
        self._office_tool_secret: str = ""

        # Override the subprocess argv for testing (mock agent
        # process). When None, ``_resolve_agent_argv`` returns the
        # real default.
        self._agent_command = _agent_command

        # Tracked agent processes by agent_name
        self._agents: dict[str, AgentProcess] = {}

        # Per-agent lock to prevent concurrent spawn/kill for the same agent
        self._locks: dict[str, asyncio.Lock] = {}

        # P2-B: Per-agent stdin-write lock. Distinct from ``_locks``
        # (which gates spawn/kill) because the heartbeat loop and the
        # task dispatcher can BOTH call ``_send_to_agent`` while a
        # third caller is sending a chat_message — stdin is a single
        # shared resource and concurrent writes can interleave NDJSON
        # frames mid-line, corrupting the IPC stream. The lock is
        # held only for the write+drain (microseconds), so contention
        # is bounded.
        self._write_locks: dict[str, asyncio.Lock] = {}

    def set_tool_proxy(
        self, url: str, token: str, collections_token: str = "",
    ) -> None:
        """Set both the per-office proxy URL AND its bearer token.

        The token is the ``ToolProxyServer.token`` property — a
        per-process random secret that the in-container MCP must
        present on every ``/tool-call`` and ``/script-execute-host``
        POST.

        ``collections_token`` is the NARROW second token (spec
        ui-ux-aug19 D4.2) valid ONLY on ``/collections/rpc``. It rides
        the agent env chain so the in-container script-exec path can
        hand it (and nothing stronger) to script subprocesses.
        """
        self._tool_proxy_url = url or ""
        self._tool_proxy_token = token or ""
        self._collections_token = collections_token or ""

    def set_office_tool_secret(self, secret: str) -> None:
        """Set the per-office /tool-call capability secret (from sync_config).

        Threaded into each spawned agent's MCP env so the in-container MCP
        server can authenticate its direct tool-call POSTs (SEC3-01).
        """
        self._office_tool_secret = secret or ""

    # -----------------------------------------------------------------
    # Public: state queries
    # -----------------------------------------------------------------

    def _get_lock(self, agent_name: str) -> asyncio.Lock:
        """Get or create the asyncio.Lock for a specific agent."""
        if agent_name not in self._locks:
            self._locks[agent_name] = asyncio.Lock()
        return self._locks[agent_name]

    def _get_write_lock(self, agent_name: str) -> asyncio.Lock:
        """Get or create the per-agent stdin-write lock."""
        if agent_name not in self._write_locks:
            self._write_locks[agent_name] = asyncio.Lock()
        return self._write_locks[agent_name]

    def _resolve_agent_argv(self) -> list[str]:
        # ``sys.executable`` so the spawn works on Ubuntu 24.04+
        # (only ``python3`` on PATH) and inside pipx venvs. The
        # ``_agent_command`` override exists for the mock-subprocess
        # test harness.
        return self._agent_command or [
            sys.executable, "-m", "src.agent_worker",
        ]

    def _build_subprocess_env(self) -> dict[str, str]:
        # Per-office tool-proxy URL + token must be passed explicitly
        # (NOT via shared os.environ) so a second office starting up
        # can't cross-wire its proxy onto this office's workers.
        env = {**os.environ}
        if self._tool_proxy_url:
            env["CUBICLE_TOOL_PROXY_URL"] = self._tool_proxy_url
        if self._tool_proxy_token:
            env["CUBICLE_TOOL_PROXY_TOKEN"] = self._tool_proxy_token
        # Narrow collections token for the in-container script-exec
        # path — scripts get THIS one, never the main proxy token
        # (spec ui-ux-aug19 D4.2/D4.3).
        if self._collections_token:
            env["CUBICLE_COLLECTIONS_TOKEN"] = self._collections_token
        # Per-office secret so the agent's MCP server can authenticate the
        # DIRECT /tool-call fallback (the proxy path is office-pinned and
        # doesn't need it). Per-office, like the proxy token above.
        if self._office_tool_secret:
            env["CUBICLE_OFFICE_TOOL_SECRET"] = self._office_tool_secret
        return env

    @property
    def active_count(self) -> int:
        """Number of non-IDLE agent processes."""
        return sum(
            1
            for a in self._agents.values()
            if a.state not in (AgentState.IDLE, AgentState.CRASHED)
        )

    def can_spawn(self) -> bool:
        """Check if we can spawn another agent process."""
        return self.active_count < self._max_agents

    def get_agent_current_task(self, agent_name: str) -> str | None:
        """Return the task_id the agent is currently working on, or None."""
        agent = self._agents.get(agent_name)
        if agent:
            return agent.current_task_id
        return None

    def get_agent_state(self, agent_name: str) -> AgentState:
        """Get the current state of a named agent."""
        agent = self._agents.get(agent_name)
        return agent.state if agent else AgentState.IDLE

    def is_agent_busy(self, agent_name: str) -> bool:
        """Check if an agent is in a non-assignable state."""
        state = self.get_agent_state(agent_name)
        return state in (
            AgentState.SPAWNING,
            AgentState.READY,
            AgentState.WORKING,
        )

    def reconcile_stuck_agents(self) -> list[str]:
        """Reset agents stuck in a busy state with NO live process.

        Self-heal for the "reviewer never picks up its task" / "agent shows
        working but does nothing" class. A session cancelled or shut down
        mid-flight (e.g. a worker killed on an old task via cancel_task /
        SIGTERM) can leave the agent at ``WORKING``/``READY``/``SPAWNING``
        without the reader-loop ever reaching its IDLE transition (the
        ``_on_event`` callback can be cancelled before the state flip, and
        ``_kill_process`` historically didn't reset state). ``is_agent_busy``
        then short-circuits EVERY ``dispatch_agent`` call, so the agent's queue
        (its assigned review/work task) never drains — permanently.

        For each agent in a busy state whose process is gone (``None`` or
        already exited), reset it to IDLE and clear its current-task pointer so
        the next dispatch cycle can assign its queued task. A busy agent WITH a
        live process is left alone (it's genuinely working). Returns the names
        reset, for logging. Cheap (in-memory) — safe to call every loop.
        """
        reset: list[str] = []
        for name, agent in self._agents.items():
            if agent.state not in (
                AgentState.SPAWNING,
                AgentState.READY,
                AgentState.WORKING,
            ):
                continue
            proc = agent.process
            process_alive = proc is not None and proc.returncode is None
            if process_alive:
                continue  # genuinely busy — leave it
            logger.warning(
                "Self-heal: agent %s stuck in %s with no live process "
                "(current_task=%s) — resetting to IDLE so its queue can "
                "dispatch.",
                name,
                agent.state.value,
                agent.current_task_id,
            )
            agent.state = AgentState.IDLE
            agent.current_task_id = None
            agent.current_readable_id = None
            agent.pid = None
            agent.process = None
            reset.append(name)
        return reset

    def get_all_statuses(self) -> dict[str, dict]:
        """Get status summary for all tracked agents (for health reports).

        Returns:
            Dict of agent_name -> {status, pid, current_task, uptime}.
        """
        result = {}
        for name, agent in self._agents.items():
            result[name] = {
                "status": agent.state.value,
                "pid": agent.pid,
                "current_task": agent.current_task_id,
                "uptime": (
                    time.monotonic() - agent.started_at
                    if agent.started_at
                    else 0
                ),
            }
        return result

    # -----------------------------------------------------------------
    # Public: spawn worker
    # -----------------------------------------------------------------

    async def spawn_worker(
        self,
        agent_name: str,
        agent_config: dict[str, Any],
        task_data: dict[str, Any],
    ) -> bool:
        """Spawn a worker process and assign it a task.

        Creates a new subprocess running agent_worker.py with --role worker.
        Waits for the "ready" message, then sends assign_task with the full
        task data. Starts background tasks for reading stdout, monitoring
        process exit, and heartbeat pinging.

        Args:
            agent_name: The agent's name (e.g., "analyst").
            agent_config: The agent's configuration dict.
            task_data: The full task data including brief, workspace, etc.

        Returns:
            True if the process was spawned and the task was assigned.
            False if the agent is already busy or the limit is reached.
        """
        async with self._get_lock(agent_name):
            if self.is_agent_busy(agent_name):
                logger.warning(
                    "Cannot spawn %s: already busy (state=%s)",
                    agent_name,
                    self.get_agent_state(agent_name).value,
                )
                return False

            if not self.can_spawn():
                logger.warning(
                    "Cannot spawn %s: max agents reached (%d)",
                    agent_name,
                    self._max_agents,
                )
                return False

            # Kill any previous process for this agent (workers are
            # not long-lived — each task gets a fresh process).
            old = self._agents.get(agent_name)
            if old and old.process and old.process.returncode is None:
                try:
                    old.process.terminate()
                    await asyncio.wait_for(old.process.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        old.process.kill()
                    except ProcessLookupError:
                        pass
                # Cancel old background tasks
                for task in (old.reader_task, old.monitor_task, old.heartbeat_task):
                    if task and not task.done():
                        task.cancel()
                logger.debug("Cleaned up old %s process (PID %s)", agent_name, old.pid)

            now = time.monotonic()
            agent = AgentProcess(
                agent_name=agent_name,
                role="worker",
                state=AgentState.SPAWNING,
                started_at=now,
                last_message_at=now,
            )
            self._agents[agent_name] = agent

            try:
                cmd = self._resolve_agent_argv()
                worker_env = self._build_subprocess_env()
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    "--role",
                    "worker",
                    "--agent-name",
                    agent_name,
                    "--workspace-path",
                    self._workspace,
                    "--office-id",
                    self._office_id,
                    "--backend-url",
                    self._backend_url,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_STREAM_LIMIT,
                    env=worker_env,
                    cwd=os.path.dirname(
                        os.path.dirname(__file__)
                    ),  # communicator/
                )
            except Exception as exc:
                logger.error(
                    "Failed to spawn process for %s: %s", agent_name, exc
                )
                agent.state = AgentState.CRASHED
                return False

            agent.process = process
            agent.pid = process.pid
            logger.info(
                "Spawned worker process for %s (PID %d)",
                agent_name,
                process.pid,
            )

            # Amendment C-2: Start dedicated reader loop for this process.
            # This continuously drains stdout so the pipe buffer never fills.
            agent.reader_task = asyncio.create_task(
                self._reader_loop(agent_name, process.stdout)
            )

            # Wait for "ready" message
            try:
                await asyncio.wait_for(
                    self._wait_for_ready(agent_name),
                    timeout=SPAWN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Agent %s did not become ready within %ds",
                    agent_name,
                    SPAWN_TIMEOUT_SECONDS,
                )
                await self._kill_process(agent_name)
                agent.state = AgentState.CRASHED
                return False
            except RuntimeError as exc:
                # P2.5-B: ``_wait_for_ready`` (P2-C) early-exits with
                # RuntimeError when the agent already crashed during
                # boot OR the underlying process exited. Without this
                # branch, the RuntimeError propagated out of
                # ``spawn_worker`` and broke the documented contract
                # ("returns False on failure"). It also bypassed the
                # ``_kill_process`` cleanup path, leaving zombie
                # subprocess records and breaking Manager auto-restart.
                logger.error(
                    "Agent %s failed during spawn: %s", agent_name, exc,
                )
                await self._kill_process(agent_name)
                agent.state = AgentState.CRASHED
                return False

            # Agent is ready -- assign the task
            agent.state = AgentState.WORKING
            agent.current_task_id = task_data.get("task_id", "")
            agent.current_readable_id = task_data.get("readable_id", "")

            # Inject container_name into agent_config so the subprocess can
            # invoke Claude CLI via docker exec
            config_with_url = {
                **agent_config,
                "_container_name": self._container_name,
            }

            assign_msg = {
                "type": "assign_task",
                **task_data,
                "agent_config": config_with_url,
                "workspace_path": self._workspace,
                "backend_url": self._backend_url,
                "office_id": self._office_id,
            }
            await self._send_to_agent(agent_name, assign_msg)

            # Monitor process exit in background. Pass OUR AgentProcess
            # record explicitly (Issue 3) — a later spawn can replace
            # ``self._agents[agent_name]`` before/while the monitor runs,
            # and a name-based lookup would mutate the REPLACEMENT.
            agent.monitor_task = asyncio.create_task(
                self._monitor_exit(agent_name, agent)
            )

            # Amendment A4: Start heartbeat monitoring
            agent.heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(agent_name)
            )

            return True

    # -----------------------------------------------------------------
    # Public: spawn Manager
    # -----------------------------------------------------------------

    async def spawn_manager(self, agent_config: dict[str, Any]) -> bool:
        """Spawn the Manager agent process (long-lived).

        The Manager process stays alive across multiple chat messages.
        It is spawned once and receives chat_message commands via stdin.
        Unlike workers, it does NOT exit after each query.

        Args:
            agent_config: The Manager's configuration dict.

        Returns:
            True if spawned (or already running). False on failure.
        """
        agent_name = "manager"
        async with self._get_lock(agent_name):
            if self.is_agent_busy(agent_name):
                return True  # Already running

            now = time.monotonic()
            agent = AgentProcess(
                agent_name=agent_name,
                role="manager",
                state=AgentState.SPAWNING,
                started_at=now,
                last_message_at=now,
            )
            self._agents[agent_name] = agent

            try:
                cmd = self._resolve_agent_argv()
                manager_env = self._build_subprocess_env()
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    "--role",
                    "manager",
                    "--agent-name",
                    "manager",
                    "--workspace-path",
                    self._workspace,
                    "--office-id",
                    self._office_id,
                    "--backend-url",
                    self._backend_url,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_STREAM_LIMIT,
                    env=manager_env,
                    cwd=os.path.dirname(os.path.dirname(__file__)),
                )
            except Exception as exc:
                logger.error("Failed to spawn Manager process: %s", exc)
                agent.state = AgentState.CRASHED
                return False

            agent.process = process
            agent.pid = process.pid

            # Amendment C-2: Dedicated reader loop for Manager
            agent.reader_task = asyncio.create_task(
                self._reader_loop(agent_name, process.stdout)
            )

            try:
                await asyncio.wait_for(
                    self._wait_for_ready(agent_name),
                    timeout=SPAWN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Manager did not become ready within %ds",
                    SPAWN_TIMEOUT_SECONDS,
                )
                await self._kill_process(agent_name)
                agent.state = AgentState.CRASHED
                return False
            except RuntimeError as exc:
                # P2.5-B: same fix as spawn_worker. Without it, a
                # Manager that crashes mid-boot bypasses the
                # auto-restart path in ManagerController._spawn_manager.
                logger.error("Manager failed during spawn: %s", exc)
                await self._kill_process(agent_name)
                agent.state = AgentState.CRASHED
                return False

            agent.state = AgentState.READY
            logger.info("Manager process ready (PID %d)", process.pid)

            # Pass OUR record explicitly — see spawn_worker (Issue 3).
            agent.monitor_task = asyncio.create_task(
                self._monitor_exit(agent_name, agent)
            )

            # Amendment A4: Heartbeat for Manager too
            agent.heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(agent_name)
            )

            return True

    # -----------------------------------------------------------------
    # Public: send chat to Manager
    # -----------------------------------------------------------------

    async def send_chat_to_manager(self, msg: dict) -> None:
        """Forward a chat message to the Manager process.

        The Manager must be in READY or WORKING state. Transitions the
        Manager to WORKING state while processing the query.

        Args:
            msg: The chat_message dict (context_key, content, etc.).

        Raises:
            RuntimeError: If the Manager process is not running.
        """
        agent = self._agents.get("manager")
        if not agent or agent.state not in (
            AgentState.READY,
            AgentState.WORKING,
        ):
            raise RuntimeError("Manager process is not running")
        agent.state = AgentState.WORKING
        # Inject container_name so the subprocess can invoke Claude CLI
        enriched = {
            "type": "chat_message",
            **msg,
        }
        if "agent_config" not in enriched:
            enriched["agent_config"] = {}
        enriched["agent_config"]["_container_name"] = self._container_name
        await self._send_to_agent("manager", enriched)

    # -----------------------------------------------------------------
    # Internal: IPC write
    # -----------------------------------------------------------------

    async def _send_to_agent(self, agent_name: str, msg: dict) -> None:
        """Write an NDJSON message to an agent's stdin.

        Serializes the message as compact JSON, writes it as a single
        line to the process's stdin pipe, and drains the buffer.

        P2-B: Holds a per-agent write lock for the duration of the
        write+drain. Without the lock, two coroutines (e.g. the
        heartbeat loop and a chat dispatcher) writing to the same
        stdin can interleave bytes — the receiving agent's NDJSON
        parser then sees a malformed line and raises.

        P2.5-A: ``drain()`` blocks until the kernel pipe buffer is
        drained, which depends on the agent process actually reading.
        A hung-but-alive agent would otherwise hold the write lock
        forever, starving the heartbeat (the only mechanism that
        detects "agent silent for 90s") and serial shutdown sends.
        We bound the drain at 5 s; on timeout we mark the agent
        CRASHED so the spawn/dispatch path can recover. The 5-s
        budget is plenty for a healthy reader (microseconds) and
        catches a wedged stdin promptly.

        Args:
            agent_name: The target agent's name.
            msg: The message dict to send.

        Raises:
            RuntimeError: If the agent has no active process / stdin
                OR the drain timed out (treated as a crashed agent).
        """
        agent = self._agents.get(agent_name)
        if not agent or not agent.process or not agent.process.stdin:
            raise RuntimeError(
                f"Agent {agent_name} has no active process"
            )
        line = json.dumps(msg, separators=(",", ":"), default=str) + "\n"
        async with self._get_write_lock(agent_name):
            agent.process.stdin.write(line.encode())
            try:
                await asyncio.wait_for(
                    agent.process.stdin.drain(), timeout=5,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "stdin drain to %s timed out — marking CRASHED so "
                    "the supervisor can recover the slot",
                    agent_name,
                )
                agent.state = AgentState.CRASHED
                raise RuntimeError(
                    f"Agent {agent_name} stdin drain timeout (hung reader)",
                )

    # -----------------------------------------------------------------
    # Internal: IPC read (Amendment C-2: dedicated reader per process)
    # -----------------------------------------------------------------

    async def _reader_loop(self, agent_name: str, stdout) -> None:
        """Dedicated reader loop for one agent process's stdout.

        Amendment C-2: Each agent process has its own reader task that
        continuously drains stdout. This prevents pipe buffer deadlock
        that would occur if the Orchestrator blocked on one agent's
        stdout while another agent's pipe buffer filled up.

        The reader loop:
        1. Reads one line at a time from stdout.
        2. Parses it as JSON (skips non-JSON lines).
        3. Updates the agent's last_message_at timestamp.
        4. Handles internal state transitions (ready, task_complete, response_final).
        5. Forwards all events to the on_event callback.

        Args:
            agent_name: The agent whose stdout we are reading.
            stdout: The asyncio StreamReader for the process's stdout.
        """
        while True:
            try:
                line = await stdout.readline()
            except ValueError as exc:
                # T8.1.2 (03/#7): an oversized NDJSON line. CPython's
                # ``readline()`` CLEARS the buffer and raises ``ValueError``
                # (not ``LimitOverrunError`` — that never reaches the caller).
                # The old ``except asyncio.LimitOverrunError`` branch was dead;
                # the ValueError fell into the generic ``except Exception``
                # below → the reader loop BROKE while the process was alive →
                # pongs stopped → the heartbeat killed a HEALTHY agent ~90s
                # later as "wedged". The buffer is already cleared, so skip the
                # oversized line and keep reading (keeps pongs flowing).
                logger.warning(
                    "Agent %s emitted a line exceeding the %d-byte limit; "
                    "skipping it (%s)", agent_name, _STREAM_LIMIT, exc,
                )
                continue
            except Exception as exc:
                logger.debug("Reader loop for %s exited: %s", agent_name, exc)
                break
            if not line:
                break  # EOF -- process exited
            # W5-P2-H1: ``errors="replace"`` so a malformed UTF-8 byte
            # (buggy agent output, accidental binary blob in stdout)
            # substitutes U+FFFD instead of raising UnicodeDecodeError.
            # The strict-mode decode used to kill the reader loop on
            # a single bad byte, which in turn killed the agent's
            # heartbeat and got it reaped by the supervisor — a DoS
            # vector. With ``replace`` the JSON parse below will
            # fail cleanly and the loop continues.
            decoded = line.decode(errors="replace").strip()
            if not decoded:
                continue
            try:
                msg = json.loads(decoded)
            except json.JSONDecodeError:
                logger.debug(
                    "Non-JSON output from %s: %s",
                    agent_name,
                    decoded[:200],
                )
                continue

            agent = self._agents.get(agent_name)
            if agent:
                agent.last_message_at = time.monotonic()

            msg_type = msg.get("type", "")

            # P6.10 v2: dedicated PONG → last_pong_at update so the
            # heartbeat can distinguish "agent ack'd our PING" from
            # "agent emitted some other message". Pongs aren't
            # forwarded to the on_event callback.
            if msg_type == "pong":
                if agent:
                    agent.last_pong_at = time.monotonic()
                continue

            # Handle "ready" internally -- transitions SPAWNING -> READY
            if msg_type == "ready":
                if agent:
                    agent.state = AgentState.READY
                    # Treat READY as the initial PONG so the first
                    # heartbeat tick has a baseline.
                    agent.last_pong_at = time.monotonic()
                continue

            # Handle "task_complete" internally -- worker finished task.
            # Clear task tracking but keep state as WORKING until after
            # the _on_event callback completes.  This prevents a race
            # where the dispatcher sees IDLE, calls spawn_worker(),
            # cancels the reader_loop, and kills the _on_event callback
            # (which does the HTTP calls to unassign the task) mid-flight.
            if msg_type == "task_complete":
                if agent:
                    agent.current_task_id = None
                    agent.current_readable_id = None
                    # NOTE: state stays WORKING — set to IDLE after _on_event

            # Handle "response_final" -- Manager done with query
            if msg_type == "response_final":
                if agent:
                    agent.state = AgentState.READY

            # Forward ALL events (including task_complete, response_final)
            # to the callback for external handling.
            #
            # P2-G + P2.5-D: every callback is bounded so a slow
            # backend / Redis / WS broadcast can't pin the reader
            # loop indefinitely. A pinned reader stops draining the
            # agent's stdout pipe, which eventually blocks the
            # agent's writes — the whole IPC channel wedges. The
            # timeouts differ by event type:
            #
            # - task_complete: 30 s. Triggers unassign + broadcast +
            #   audit; these can be slow but a 30-s ceiling keeps
            #   the queue moving.
            # - response_chunk / progress: 10 s. Streaming events
            #   should be near-instant Redis XADDs; 10 s catches
            #   genuine wedges without truncating healthy bursts.
            # - everything else: 30 s as a safety net.
            if self._on_event:
                if msg_type == "task_complete":
                    timeout = 30
                elif msg_type in ("response_chunk", "progress"):
                    timeout = 10
                else:
                    timeout = 30
                try:
                    await asyncio.wait_for(
                        self._on_event(agent_name, msg), timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "%s callback for %s exceeded %ds; reader loop "
                        "moves on so stdout pipe stays drained",
                        msg_type, agent_name, timeout,
                    )
                except Exception as exc:
                    logger.exception(
                        "Error in event callback for %s: %s",
                        agent_name,
                        exc,
                    )

            # NOW transition worker to IDLE — after _on_event has finished
            # the unassign/cleanup HTTP calls.  The dispatcher can only
            # see this agent as available from this point onward.
            if msg_type == "task_complete":
                if agent:
                    agent.state = AgentState.IDLE

    # -----------------------------------------------------------------
    # Internal: wait for ready
    # -----------------------------------------------------------------

    async def _wait_for_ready(self, agent_name: str) -> None:
        """Wait until the agent transitions out of SPAWNING state.

        Polls the agent's state every 100ms. Returns as soon as the
        state is no longer SPAWNING.

        P2-C: also returns early with ``RuntimeError`` if the agent
        has already crashed or its underlying process has exited.
        Without this check the wait would hang for the full caller
        timeout (typically 30 s) on a process that died during boot,
        e.g. a missing claude binary or a permission failure inside
        the container — observed in production as "spawn timeout"
        even though the process exited within milliseconds.
        """
        while True:
            agent = self._agents.get(agent_name)
            if not agent:
                # The agent was GC'd while we were waiting — let the
                # caller treat this as a timeout-equivalent.
                return
            if agent.state == AgentState.CRASHED:
                raise RuntimeError(
                    f"Agent {agent_name} crashed during spawn",
                )
            if agent.process is not None and agent.process.returncode is not None:
                raise RuntimeError(
                    f"Agent {agent_name} process exited "
                    f"(rc={agent.process.returncode}) before becoming ready",
                )
            if agent.state != AgentState.SPAWNING:
                return
            await asyncio.sleep(0.1)

    # -----------------------------------------------------------------
    # Internal: process exit monitoring
    # -----------------------------------------------------------------

    async def _monitor_exit(
        self, agent_name: str, agent: AgentProcess | None = None,
    ) -> None:
        """Wait for an agent process to exit and handle cleanup.

        This runs as a background asyncio task for each spawned process.
        It waits for the process to exit (via process.wait()), then:
        - On clean exit (code 0): transitions to IDLE.
        - On crash (code != 0): transitions to CRASHED, notifies via callback.
        - Cleans up: nullifies process references, cancels reader/heartbeat tasks.

        Event-hygiene (Issue 3): the spawn sites pass THEIR AgentProcess
        record explicitly. A name-based lookup can resolve to a
        REPLACEMENT record (a fresh spawn can swap
        ``self._agents[agent_name]`` between this task's creation and its
        first step, or while it is parked on ``process.wait()``), and
        mutating that would flip the NEW process's state / clear its task
        pointers. After the wait, shared state is only mutated when our
        record is still the registered one; the exit/error event is still
        emitted for OUR process either way (with OUR task snapshot).

        Args:
            agent_name: The agent whose process we are monitoring.
            agent: The AgentProcess record this monitor was started for.
                ``None`` falls back to a registry lookup (back-compat for
                direct callers/tests that registered the record first).
        """
        if agent is None:
            agent = self._agents.get(agent_name)
        if not agent or not agent.process:
            return

        # Capture the process handle — ``_kill_process`` nulls
        # ``agent.process`` and must not break the in-flight wait.
        process = agent.process
        exit_code = await process.wait()
        agent.exit_code = exit_code
        # T1.1.8 (G19): if _kill_process's continuation won the race and
        # already nulled ``current_task_id``, fall back to the frozen
        # ``killed_task_id`` snapshot so the fatal error event below
        # still carries the task and crash-recovery routing fires.
        task_id = agent.current_task_id or agent.killed_task_id

        # Issue 3: only flip dict-visible state if our record is still
        # the registered one for this agent name.
        is_registered = self._agents.get(agent_name) is agent
        if not is_registered:
            logger.debug(
                "Agent %s record was replaced while monitoring PID %s — "
                "skipping state mutation for the stale record.",
                agent_name, agent.pid,
            )

        if exit_code == 0:
            logger.info(
                "Agent %s exited cleanly (PID %d)",
                agent_name,
                agent.pid or 0,
            )
            if is_registered:
                agent.state = AgentState.IDLE
        else:
            logger.error(
                "Agent %s crashed (PID %d, exit_code=%d, task=%s)",
                agent_name,
                agent.pid or 0,
                exit_code,
                task_id,
            )
            if is_registered and not agent.kill_initiated:
                agent.state = AgentState.CRASHED
            elif is_registered:
                # Issue 4: the exit was killer-initiated — _kill_process
                # already reset the record to IDLE; flipping it to
                # CRASHED afterwards is a misleading state read.
                logger.debug(
                    "Agent %s exit was killer-initiated — keeping the "
                    "state set by _kill_process instead of CRASHED.",
                    agent_name,
                )

            # Notify via event callback so the dispatcher can handle
            # recovery. Issue 4: skip when the heartbeat loop already
            # emitted the fatal error for this same process+task (it
            # snapshots the task_id and emits BEFORE killing) — a killed
            # WORKING agent used to produce TWO fatal error events.
            if self._on_event and task_id and not agent.fatal_error_emitted:
                await self._on_event(
                    agent_name,
                    {
                        "type": "error",
                        "message": (
                            f"Agent process exited with code {exit_code}"
                        ),
                        "task_id": task_id,
                        "fatal": True,
                    },
                )

        # Cancel background tasks BEFORE clearing process references
        # to avoid reader_loop accessing a nullified process.
        if agent.reader_task and not agent.reader_task.done():
            agent.reader_task.cancel()
        if agent.heartbeat_task and not agent.heartbeat_task.done():
            agent.heartbeat_task.cancel()

        # Cleanup
        agent.process = None
        agent.pid = None
        agent.current_task_id = None
        agent.current_readable_id = None

    # -----------------------------------------------------------------
    # Internal: heartbeat (Amendment A4)
    # -----------------------------------------------------------------

    async def _heartbeat_loop(self, agent_name: str) -> None:
        """Monitor agent process liveness via PING/PONG round-trip.

        P6.10 v2 (review): the previous version relied on
        ``last_message_at`` (updated on EVERY incoming message
        including progress events), which seemed reasonable but broke
        on real agents — the worker's read loop is single-threaded
        and stops reading stdin while it's inside
        ``stream_cli_session``. A legitimate slow Claude call without
        progress events for >90s got killed as "wedged" even though
        it was healthy. The fix: track PING / PONG explicitly.

        Algorithm per tick:

        1. Sleep HEARTBEAT_INTERVAL_SECONDS.
        2. If the process has exited, stop — _monitor_exit handles it.
        3. If `now - last_pong_at` exceeds HEARTBEAT_TIMEOUT_SECONDS
           the agent has wedged. Kill it.
        4. Otherwise, send a fresh PING. The reader loop sets
           ``last_pong_at`` when it sees the response.

        Pipe-break is detected by the PING send raising
        OSError/RuntimeError — handled separately.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

            agent = self._agents.get(agent_name)
            if not agent or not agent.process:
                break
            if agent.process.returncode is not None:
                break  # Process already exited — _reader_loop handles this

            # Liveness check: time since the agent's last PONG (or
            # the "ready" message that we treat as the initial PONG).
            # Independent of whatever else the agent is doing — a
            # healthy agent's reader loop responds to PINGs even
            # during a long Claude call.
            if (
                agent.last_pong_at > 0
                and time.monotonic() - agent.last_pong_at
                > HEARTBEAT_TIMEOUT_SECONDS
            ):
                outstanding = time.monotonic() - agent.last_pong_at
                logger.warning(
                    "Agent %s did not PONG within %.1fs (>%ds) — "
                    "killing wedged process.",
                    agent_name, outstanding, HEARTBEAT_TIMEOUT_SECONDS,
                )
                # T1.1.8 (G19): snapshot the in-flight task BEFORE the
                # kill below resets it — without ``task_id`` on this
                # event, handlers.py's crash-recovery routing (gated on
                # ``if task_id:``) skips entirely and the killed task
                # waits on the 60s reconciler instead of an immediate
                # re-queue.
                task_id = agent.current_task_id
                if self._on_event:
                    try:
                        await self._on_event(agent_name, {
                            "type": "error",
                            "agent_name": agent_name,
                            "fatal": True,
                            "reason": "heartbeat_timeout",
                            "task_id": task_id,
                            "elapsed_seconds": outstanding,
                        })
                        # Issue 4: dedupe — _monitor_exit must not emit a
                        # SECOND fatal error for this same process+task
                        # when the kill below makes the process exit.
                        agent.fatal_error_emitted = True
                    except Exception:
                        logger.exception(
                            "on_event callback raised while emitting "
                            "heartbeat_timeout for %s — proceeding with kill",
                            agent_name,
                        )
                await self._kill_process(agent_name)
                break

            # Send PING. Pipe-break detected here.
            try:
                await self._send_to_agent(
                    agent_name, {"type": "ping"}
                )
            except (RuntimeError, OSError):
                logger.warning(
                    "Failed to send PING to %s -- process is dead.",
                    agent_name,
                )
                break

    # -----------------------------------------------------------------
    # Internal: process termination
    # -----------------------------------------------------------------

    async def _kill_process(self, agent_name: str) -> None:
        """Forcefully terminate an agent process.

        First sends SIGTERM and waits 5 seconds. If the process does not
        exit, sends SIGKILL. Handles ProcessLookupError (process already
        gone).

        Args:
            agent_name: The agent whose process to kill.
        """
        agent = self._agents.get(agent_name)
        if not agent or not agent.process:
            return

        try:
            agent.process.terminate()  # SIGTERM
            try:
                await asyncio.wait_for(agent.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                agent.process.kill()  # SIGKILL
                await agent.process.wait()
        except ProcessLookupError:
            pass  # Process already gone

        logger.info(
            "Killed agent process %s (PID %s)", agent_name, agent.pid
        )
        # Reset state directly rather than relying solely on _monitor_exit /
        # the reader loop to observe the exit — those can be cancelled in the
        # teardown race, stranding the agent at WORKING and short-circuiting
        # all future dispatch. A spawn-replace caller sets SPAWNING again right
        # after this returns, so the brief IDLE is harmless.
        #
        # T1.1.8 (G19): freeze the in-flight task id BEFORE nulling it so
        # a _monitor_exit continuation that loses the race against this
        # reset can still attach the task_id to its fatal error event
        # (handlers.py skips ALL crash-recovery routing without it).
        if agent.current_task_id:
            agent.killed_task_id = agent.current_task_id
        # Issue 4: signal _monitor_exit that this exit was killer-
        # initiated so it doesn't overwrite the IDLE reset below with a
        # misleading CRASHED.
        agent.kill_initiated = True
        agent.state = AgentState.IDLE
        agent.current_task_id = None
        agent.current_readable_id = None
        agent.pid = None
        agent.process = None

    # -----------------------------------------------------------------
    # Public: graceful shutdown
    # -----------------------------------------------------------------

    async def shutdown(self, timeout: int = SHUTDOWN_GRACE_SECONDS) -> None:
        """Shut down all agent processes gracefully.

        1. Send shutdown message to all running processes.
        2. Wait up to `timeout` seconds for all to exit.
        3. Kill any that did not exit in time.
        4. Cancel all background tasks (readers, monitors, heartbeats).
        5. Clear the agents registry.

        Args:
            timeout: Maximum seconds to wait for graceful exit.
        """
        logger.info(
            "Shutting down all agent processes (timeout=%ds)", timeout
        )

        # Send shutdown to all running processes. Narrow the catch
        # to IPC failure modes — a swallow-everything would mask a
        # CancelledError from the parent shutdown signal and leave
        # the loop unrecoverable.
        for agent_name, agent in self._agents.items():
            if agent.process and agent.process.returncode is None:
                try:
                    await self._send_to_agent(
                        agent_name,
                        {
                            "type": "shutdown",
                            "grace_period_seconds": timeout,
                        },
                    )
                except (RuntimeError, OSError):
                    # BrokenPipeError is an OSError subclass — no
                    # need to list it separately.
                    pass

        # Wait for all processes to exit
        processes = [
            a.process
            for a in self._agents.values()
            if a.process and a.process.returncode is None
        ]
        if processes:
            wait_tasks = [
                asyncio.create_task(p.wait()) for p in processes
            ]
            done, pending = await asyncio.wait(
                wait_tasks, timeout=timeout
            )
            # Kill any that did not exit in time
            for task in pending:
                task.cancel()
            for agent in self._agents.values():
                if (
                    agent.process
                    and agent.process.returncode is None
                ):
                    try:
                        agent.process.kill()
                    except ProcessLookupError:
                        pass

        # Cancel all background tasks
        for agent in self._agents.values():
            for task_attr in (
                "reader_task",
                "monitor_task",
                "heartbeat_task",
            ):
                task = getattr(agent, task_attr, None)
                if task and not task.done():
                    task.cancel()

        self._agents.clear()
        logger.info("All agent processes shut down")
