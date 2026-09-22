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
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from src.tool_proxy_identity import ProxyCredentials, ProxySessionRegistry

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


class ManagerTurnBusy(RuntimeError):
    """A previous Manager CLI turn has not confirmed completion yet."""


@dataclass
class AgentProcess:
    """Tracks one agent subprocess.

    Contains all state needed to manage the subprocess lifecycle:
    process handle, state, current task, timing, and background tasks.
    """

    agent_name: str
    role: str  # "manager" or "worker"
    agent_instance_id: str = ""
    profile_id: str = ""
    runtime_release_required: bool = False
    runtime_released: bool = False
    execution_resources: list[str] = field(default_factory=list)
    state: AgentState = AgentState.IDLE
    process: asyncio.subprocess.Process | None = None
    spawn_task: asyncio.Task | None = None
    pid: int | None = None
    current_task_id: str | None = None
    current_readable_id: str | None = None
    manager_turn: tuple[str, str] | None = None
    execution_marker: str = ""
    execution_container_id: str = ""
    execution_container_managed: bool = False
    execution_task_id: str = ""
    execution_mode: str = ""
    execution_cycle: int = 0
    execution_generation: int = 0
    review_retry_epoch: int = 0
    execution_assignee: str = ""
    execution_attempt_id: str = field(default_factory=lambda: secrets.token_hex(16))
    failure_recorded: bool = False
    cleanup_pending: bool = False
    cleanup_failed: bool = False
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_completion: dict[str, Any] | None = None
    outcome_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    completion_delivered: bool = False
    completion_failed: bool = False
    pending_failure: dict[str, Any] | None = None
    stop_requested: bool = False
    proxy_credentials: ProxyCredentials | None = field(default=None, repr=False)
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
    observed_at: float = field(default_factory=time.time)
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
    exit_handled: bool = False

    @property
    def runtime_key(self) -> str:
        return self.agent_instance_id or self.agent_name


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
        self._failure_observer: Callable[[str, str], None] | None = None
        self._runtime_state = None
        self._execution_claimer = None
        self._execution_releaser = None
        self._execution_recoverer = None
        self._deferred_executions: set[str] = set()
        self._script_resource_provider = None
        self._config_reconciler = None
        self.config_sync_error: str | None = None
        self._execution_containers = None
        self._execution_policy = {
            "enabled": False,
            "max_workers": 4,
            "max_workers_per_profile": 2,
        }
        self._config_ready = True
        self._execution_policy_initialized = False
        self._policy_disable_pending = False
        # Admission covers capacity checks, board transition and process startup.
        # Workers execute concurrently after this short control-plane section.
        self.admission_lock = asyncio.Lock()

        # Per-office tool-proxy URL + bearer token. Set via
        # set_tool_proxy() once the ToolProxyServer has started (it
        # binds to a random OS-assigned port and mints a random
        # token). Each office has its OWN proxy — passed explicitly
        # to spawned workers rather than via shared os.environ vars
        # (which would get overwritten when a second office starts,
        # cross-wiring tool calls to the wrong office's WS).
        self._tool_proxy_url: str = ""
        self._tool_proxy_token: str = ""
        self._proxy_sessions: ProxySessionRegistry | None = None
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
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._suppressed_tasks: set[str] = set()

    def suppress_task(self, task_id: str) -> None:
        if task_id:
            self._suppressed_tasks.add(task_id)
            self._deferred_executions.discard(task_id)

    def set_failure_observer(self, observer: Callable[[str, str], None]) -> None:
        self._failure_observer = observer

    def set_runtime_state(self, runtime_state) -> None:
        self._runtime_state = runtime_state
        restored = {}
        stop_attempts = {
            record["attempt_id"]
            for record in runtime_state.pending_worker_executions()
            if record["stop_requested"]
        }
        for receipt in runtime_state.pending_completions():
            name = receipt["agent_name"]
            event = receipt["payload"]
            caller = event.get("_caller") or {}
            runtime_key = caller.get("agent_instance_id") or name
            if runtime_key in restored or runtime_key in self._agents:
                raise RuntimeError(
                    "Multiple retained attempts share one Agent; reconcile before office admission"
                )
            fatal = event.get("type") == "error" and event.get("fatal") is True
            if event.get("task_id") != receipt["task_id"] or (
                caller and (
                    caller.get("task_id") != receipt["task_id"]
                    or caller.get("role") != "worker"
                    or caller.get("agent_name") != name
                    or caller.get("attempt_id") != receipt["attempt_id"]
                )
            ):
                raise RuntimeError("Retained completion ownership could not be verified")
            restored[runtime_key] = AgentProcess(
                name,
                "worker",
                state=AgentState.WORKING,
                agent_instance_id=caller.get("agent_instance_id", ""),
                profile_id=caller.get("profile_id", ""),
                observed_at=event.get("observed_at", 0.0),
                runtime_release_required=bool(caller.get("runtime_release_required")),
                execution_resources=list(caller.get("execution_resources") or []),
                stop_requested=receipt["attempt_id"] in stop_attempts,
                current_task_id=receipt["task_id"],
                execution_task_id=receipt["task_id"],
                execution_attempt_id=receipt["attempt_id"],
                execution_cycle=caller.get("execution_cycle", 0),
                execution_generation=caller.get("execution_generation", 0),
                review_retry_epoch=caller.get("review_retry_epoch", 0),
                execution_assignee=caller.get("expected_assigned_agent", ""),
                execution_mode=caller.get("task_mode", "execute"),
                execution_marker=receipt.get("execution_marker") or "",
                execution_container_managed=bool(receipt.get("container_managed")),
                cleanup_pending=bool(receipt.get("execution_marker")),
                pending_failure=event if fatal else None,
                pending_completion=None if fatal else event,
                completion_failed=not fatal,
            )
        self._agents.update(restored)

    def set_execution_claimer(self, claimer) -> None:
        self._execution_claimer = claimer

    def set_execution_releaser(self, releaser) -> None:
        self._execution_releaser = releaser

    def set_execution_recoverer(self, recoverer) -> None:
        self._execution_recoverer = recoverer

    def set_script_resource_provider(self, provider) -> None:
        self._script_resource_provider = provider

    def set_config_reconciler(self, reconciler) -> None:
        self._config_reconciler = reconciler

    def _untracked_executions(self) -> list[dict]:
        if self._runtime_state is None:
            return []
        tracked = {agent.execution_attempt_id for agent in self._agents.values()}
        return [
            record
            for record in self._runtime_state.pending_worker_executions()
            if record["attempt_id"] not in tracked
        ]

    async def _recover_worker_executions(self) -> None:
        for record in self._untracked_executions():
            try:
                claim = record["receipt"]
                if claim is None:
                    if self._execution_recoverer is None:
                        continue
                    claim = await self._execution_recoverer(record)
                    if claim is None:
                        self._runtime_state.forget_worker_execution(
                            record["attempt_id"]
                        )
                        continue
                    self._runtime_state.record_worker_claim(record["attempt_id"], claim)
                agent = AgentProcess(
                    agent_name=record["agent_name"],
                    role="worker",
                    state=AgentState.WORKING,
                    agent_instance_id=claim["agent_instance_id"],
                    profile_id=claim["profile_id"],
                    runtime_release_required=bool(claim["runtime_release_required"]),
                    execution_resources=list(claim.get("execution_resources") or []),
                    current_task_id=record["task_id"],
                    execution_task_id=record["task_id"],
                    execution_attempt_id=record["attempt_id"],
                    execution_cycle=claim["execution_cycle"],
                    execution_generation=claim["execution_generation"],
                    execution_assignee=claim.get(
                        "expected_assigned_agent",
                        record["request"]["expected_assigned_agent"],
                    ),
                    execution_mode=claim.get(
                        "execution_mode", record["request"]["execution_mode"]
                    ),
                    review_retry_epoch=claim.get("review_retry_epoch", 0),
                    execution_marker=record["execution_marker"],
                    execution_container_managed=bool(record["container_managed"]),
                    cleanup_pending=True,
                    stop_requested=bool(record["stop_requested"]),
                )
                if agent.runtime_key in self._agents:
                    raise RuntimeError(
                        "Conflicting unfinished attempts share a task agent"
                    )
                if not agent.stop_requested:
                    if record["execution_marker"]:
                        agent.pending_failure = self._execution_event(
                            agent,
                            {
                                "type": "error",
                                "fatal": True,
                                "task_id": record["task_id"],
                                "reason": "daemon_restart",
                                "message": "Worker execution interrupted by daemon restart",
                            },
                        )
                    else:
                        agent.pending_completion = self._execution_event(
                            agent,
                            {
                                "type": "task_complete",
                                "task_id": record["task_id"],
                                "status": {
                                    "execute": "in_progress",
                                    "review": "review",
                                    "triage": "blocked",
                                }[agent.execution_mode],
                                "execution_deferred": True,
                            },
                        )
                self._agents[agent.runtime_key] = agent
            except Exception:
                logger.exception(
                    "Retaining unresolved execution claim %s for recovery",
                    record["attempt_id"],
                )

    def set_execution_policy(self, policy: dict, *, ready: bool = True) -> bool:
        from src.agent_execution_policy import (
            POLICY_DRAIN_MESSAGE,
            normalize_execution_policy,
        )

        self._config_ready = False
        self._policy_disable_pending = False
        requested = normalize_execution_policy(policy)
        if not requested["enabled"] and self._policy_disable_needs_cleanup():
            # Keep the resource-aware script path active, including during a
            # restart whose desired policy is already off. Cleanup must be able
            # to initialize, so signal deferral without aborting office startup.
            self._execution_policy = {**requested, "enabled": True}
            self._execution_policy_initialized = True
            self._policy_disable_pending = True
            self.config_sync_error = POLICY_DRAIN_MESSAGE
            return False
        self._execution_policy = requested
        self._execution_policy_initialized = True
        self._policy_disable_pending = False
        self._config_ready = ready
        return True

    def pause_configuration(self) -> None:
        self._config_ready = False
        self._policy_disable_pending = False

    def can_continue_script_during_policy_drain(
        self, caller: dict | None, task_id: str | None,
    ) -> bool:
        """Let an exact live parent finish while new execution remains paused."""
        if (
            not self._policy_disable_pending
            or not isinstance(caller, dict)
            or caller.get("role") != "worker"
            or not caller.get("agent_instance_id")
            or not self.execution_is_current(caller, task_id)
        ):
            return False
        agent = self._agents[caller["agent_instance_id"]]
        return (
            agent.state == AgentState.WORKING
            and caller.get("task_mode") == agent.execution_mode
            and agent.execution_mode in {"execute", "review", "triage"}
        )

    def _policy_disable_needs_cleanup(self) -> bool:
        state = self._runtime_state
        # These leases are created only by resource-aware script admission and
        # remain authoritative even if the requested policy changed offline.
        if state is not None and state.active_script_resources():
            return True
        if self._execution_policy["enabled"] or not self._execution_policy_initialized:
            if any(
                agent.role == "worker" and self._agent_busy(agent)
                for agent in self._agents.values()
            ):
                return True
            if state is not None and state.pending_worker_executions():
                return True
            if (
                self._script_resource_provider is not None
                and self._script_resource_provider()
            ):
                return True
        return False

    @property
    def execution_policy(self) -> dict:
        return dict(self._execution_policy)

    @property
    def config_ready(self) -> bool:
        return self._config_ready

    def profile_can_spawn(
        self, profile_name: str, *, excluded_attempt_id: str = ""
    ) -> bool:
        if not self._config_ready or not self.can_spawn():
            return False
        workers = [
            agent
            for agent in self._agents.values()
            if agent.role == "worker" and self._agent_busy(agent)
        ]
        unresolved = [
            record
            for record in self._untracked_executions()
            if record["attempt_id"] != excluded_attempt_id
        ]
        if not self._execution_policy["enabled"]:
            return not any(
                agent.agent_name == profile_name for agent in workers
            ) and not any(record["agent_name"] == profile_name for record in unresolved)
        return (
            len(workers) + len(unresolved) < self._execution_policy["max_workers"]
            and sum(agent.agent_name == profile_name for agent in workers)
            + sum(record["agent_name"] == profile_name for record in unresolved)
            < self._execution_policy["max_workers_per_profile"]
        )

    def resources_available(
        self, profile: dict, task: dict, *, parent_attempt_id: str = ""
    ) -> bool:
        if not self._execution_policy["enabled"]:
            return True
        from src.agent_execution_policy import execution_resources

        requested = set(execution_resources(task))
        held = {
            resource
            for agent in self._agents.values()
            if agent.role == "worker"
            and self._agent_busy(agent)
            and (
                not parent_attempt_id or agent.execution_attempt_id != parent_attempt_id
            )
            for resource in (
                agent.execution_resources
                if agent.agent_instance_id or agent.execution_resources
                else ["shared-workspace"]
            )
        }
        for record in self._untracked_executions():
            if parent_attempt_id and record["attempt_id"] == parent_attempt_id:
                continue
            held.update(
                (record.get("receipt") or {}).get(
                    "execution_resources",
                    record["request"].get(
                        "expected_execution_resources", ["shared-workspace"]
                    ),
                )
            )
        if self._runtime_state is not None:
            for lease in self._runtime_state.active_script_resources():
                held.update(lease["resources"])
        if self._script_resource_provider is not None:
            held.update(self._script_resource_provider())
        return not requested.intersection(held)

    def get_task_agent(self, profile_name: str, task_id: str) -> AgentProcess | None:
        return next(
            (
                agent
                for agent in self._agents.values()
                if agent.agent_name == profile_name
                and task_id == (agent.execution_task_id or agent.current_task_id)
                and (self._agent_busy(agent) or agent.execution_marker)
            ),
            None,
        )

    def is_task_busy(self, profile_name: str, task_id: str) -> bool:
        return self.get_task_agent(profile_name, task_id) is not None or any(
            record["task_id"] == task_id for record in self._untracked_executions()
        )

    def execution_is_deferred(self, task_id: str) -> bool:
        """An execution capacity wait needs retries, not crash escalation."""
        return task_id in self._deferred_executions

    @classmethod
    def _agent_busy(cls, agent: AgentProcess) -> bool:
        return cls._has_pending_lifecycle(agent) or agent.state in (
            AgentState.SPAWNING,
            AgentState.READY,
            AgentState.WORKING,
        )

    def _retain_failure(self, agent: AgentProcess, event: dict[str, Any]) -> dict[str, Any] | None:
        """Persist worker failure identity before any destructive cleanup."""
        if agent.pending_completion is not None or agent.completion_delivered:
            return None
        event = self._execution_event(agent, agent.pending_failure or event)
        agent.pending_failure = event
        if (
            self._runtime_state is not None and agent.role == "worker"
            and agent.execution_task_id
            and not agent.execution_task_id.startswith(("planner-", "flow-consult-"))
            and not agent.stop_requested
        ):
            self._runtime_state.retain_completion(
                agent.agent_name, agent.execution_attempt_id, agent.execution_task_id, event,
                cleanup={"execution_marker": agent.execution_marker,
                         "container_managed": agent.execution_container_managed},
            )
        return event

    def _retain_boot_failure(self, agent: AgentProcess, reason: str) -> dict[str, Any] | None:
        return self._retain_failure(agent, {
            "type": "error", "fatal": True, "reason": reason,
            "message": "Worker process failed before task assignment completed",
            "task_id": agent.execution_task_id,
        })

    def set_execution_containers(self, manager) -> None:
        self._execution_containers = manager

    async def stop_retained_task_executions(self, task_id: str) -> bool:
        if self._execution_containers is None:
            return False
        return bool(await self._execution_containers.stop_task(task_id))

    def _execution_event(self, agent: AgentProcess, message: dict) -> dict:
        if agent.execution_generation <= 0 or not agent.execution_task_id:
            return dict(message)
        return {
            **message,
            **(
                {"observed_at": message.get("observed_at", agent.observed_at)}
                if agent.agent_instance_id
                else {}
            ),
            "_caller": {
                "agent_name": agent.agent_name,
                "role": agent.role,
                "task_id": agent.execution_task_id,
                "attempt_id": agent.execution_attempt_id,
                "execution_cycle": agent.execution_cycle,
                "execution_generation": agent.execution_generation,
                "expected_assigned_agent": agent.execution_assignee,
                "task_mode": agent.execution_mode,
                **(
                    {
                        "agent_instance_id": agent.agent_instance_id,
                        "profile_id": agent.profile_id,
                    }
                    if agent.agent_instance_id
                    else {}
                ),
                **(
                    {"runtime_release_required": True}
                    if agent.runtime_release_required
                    else {}
                ),
                **(
                    {"execution_resources": agent.execution_resources}
                    if agent.agent_instance_id
                    else {}
                ),
                **(
                    {"review_retry_epoch": agent.review_retry_epoch}
                    if agent.review_retry_epoch
                    else {}
                ),
            },
        }

    def execution_is_current(self, caller: dict, task_id: str | None) -> bool:
        if not isinstance(caller, dict):
            return False
        agent = self._agents.get(
            caller.get("agent_instance_id") or caller.get("agent_name")
        )
        if agent is None or agent.process is None or agent.process.returncode is not None:
            return False
        if agent.stop_requested or self._has_pending_lifecycle(agent):
            return False
        if agent.role == "manager":
            return caller.get("role") == "manager" and agent.state == AgentState.WORKING
        return bool(
            caller.get("role") == "worker"
            and caller.get("agent_name") == agent.agent_name
            and caller.get("agent_instance_id", "") == agent.agent_instance_id
            and caller.get("profile_id", "") == agent.profile_id
            and task_id
            and task_id not in self._suppressed_tasks
            and task_id == agent.current_task_id == caller.get("task_id")
            and agent.execution_generation > 0
            and caller.get("attempt_id") == agent.execution_attempt_id
            and caller.get("execution_cycle") == agent.execution_cycle
            and caller.get("execution_generation") == agent.execution_generation
            and caller.get("review_retry_epoch", 0) == agent.review_retry_epoch
        )

    def _record_failure(self, agent: AgentProcess) -> None:
        task_id = agent.execution_task_id or agent.current_task_id or agent.killed_task_id
        if (
            self._failure_observer is None
            or agent.failure_recorded
            or agent.stop_requested
            or agent.cleanup_pending
            or agent.execution_marker
            or agent.execution_mode not in {"execute", "review"}
            or not task_id
            or task_id in self._suppressed_tasks
            or task_id.startswith(("planner-", "flow-consult-"))
        ):
            return
        if self._runtime_state is not None and agent.execution_mode == "review":
            self._runtime_state.record_review_attempt(
                task_id, agent.execution_cycle, agent.agent_name, agent.execution_attempt_id,
                epoch=agent.review_retry_epoch,
            )
        elif self._runtime_state is not None:
            self._failure_observer(task_id, agent.execution_attempt_id, agent.execution_cycle)
        elif agent.execution_mode == "execute":
            self._failure_observer(task_id, agent.execution_attempt_id)
        agent.failure_recorded = True

    def set_tool_proxy(
        self, url: str, token: str, collections_token: str = "", *,
        sessions: ProxySessionRegistry | None = None,
    ) -> None:
        """Configure the office proxy and its host-owned session registry.

        Production wiring supplies ``sessions``: each spawned worker/Manager
        gets its own revocable tool and collections-only credentials. The
        shared tokens are retained only for legacy callers without a registry.
        """
        self._tool_proxy_url = url or ""
        self._tool_proxy_token = token or ""
        self._collections_token = collections_token or ""
        self._proxy_sessions = sessions

    def _proxy_identity(self, agent: AgentProcess, task_data: dict) -> dict:
        caller = {
            "agent_name": agent.agent_name,
            "role": agent.role,
            "task_mode": "manager" if agent.role == "manager" else agent.execution_mode,
        }
        if agent.execution_task_id:
            caller["task_id"] = agent.execution_task_id
        if agent.execution_generation > 0:
            caller.update(self._execution_event(agent, {})["_caller"])
        if agent.agent_instance_id and task_data.get("output_dir"):
            caller["output_dir"] = task_data["output_dir"]
        consult = task_data.get("planner_consult")
        if (
            agent.role == "worker" and agent.agent_name == "planner"
            and agent.execution_task_id.startswith("planner-")
            and isinstance(consult, dict)
        ):
            if consult.get("_infra_refire") or consult.get("_verdictless_refire"):
                caller["consult_refire"] = True
            # The daemon's consult marker is the authority for draft-task
            # corrections. Tool arguments cannot widen this workstream/scope.
            # Malformed scope metadata must not degrade into workstream-wide
            # permission, so issue none of these fields unless all IDs parse.
            mode = consult.get("mode")
            if isinstance(mode, str) and mode.strip():
                try:
                    workstream_id = str(uuid.UUID(str(consult.get("workstream_id"))))
                    scope = consult.get("scope_id")
                    if mode.strip() in ("scope_plan", "materialize", "verify") and scope in (None, ""):
                        raise ValueError("This consult requires a concrete scope")
                    scope_id = str(uuid.UUID(str(scope))) if scope not in (None, "") else None
                except (ValueError, TypeError, AttributeError):
                    logger.warning("Planner consult has invalid scope identity; draft corrections are unavailable")
                else:
                    caller["consult_mode"] = mode.strip()
                    caller["consult_workstream_id"] = workstream_id
                    if scope_id:
                        caller["consult_scope_id"] = scope_id
        return caller

    def _proxy_session_live(self, agent: AgentProcess) -> bool:
        return bool(
            self._agents.get(agent.runtime_key) is agent
            and agent.process is not None
            and agent.process.returncode is None
            and agent.state == AgentState.WORKING
            and not agent.stop_requested
            and not agent.kill_initiated
            and not self._has_pending_lifecycle(agent)
            and agent.execution_task_id not in self._suppressed_tasks
        )

    def _revoke_proxy_session(self, agent: AgentProcess) -> None:
        if self._proxy_sessions is not None:
            self._proxy_sessions.revoke(agent.proxy_credentials)
        agent.proxy_credentials = None

    def _bind_proxy_session(self, agent: AgentProcess, task_data: dict, env: dict) -> None:
        if self._proxy_sessions is None:
            return
        agent.proxy_credentials = self._proxy_sessions.issue(
            self._proxy_identity(agent, task_data), lambda: self._proxy_session_live(agent),
        )
        env["CUBICLE_TOOL_PROXY_TOKEN"] = agent.proxy_credentials.tool_token
        env["CUBICLE_COLLECTIONS_TOKEN"] = agent.proxy_credentials.collections_token

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
        from src.docker.task_process_cleanup import WORKER_EXECUTION_ENV

        env.pop(WORKER_EXECUTION_ENV, None)
        for key in (
            "CUBICLE_AGENT_INSTANCE_ID",
            "CUBICLE_PROFILE_ID",
            "CUBICLE_EXECUTION_ATTEMPT_ID",
            "CUBICLE_EXECUTION_CYCLE",
            "CUBICLE_EXECUTION_GENERATION",
            "CUBICLE_EXECUTION_ASSIGNEE",
            "CUBICLE_REVIEW_RETRY_EPOCH",
            "CUBICLE_TOOL_PROXY_URL",
            "CUBICLE_TOOL_PROXY_TOKEN",
            "CUBICLE_COLLECTIONS_TOKEN",
            "CUBICLE_OFFICE_TOOL_SECRET",
        ):
            env.pop(key, None)
        if self._tool_proxy_url:
            env["CUBICLE_TOOL_PROXY_URL"] = self._tool_proxy_url
        if self._tool_proxy_token and self._proxy_sessions is None:
            env["CUBICLE_TOOL_PROXY_TOKEN"] = self._tool_proxy_token
        # Narrow collections token for the in-container script-exec
        # path — scripts get THIS one, never the main proxy token
        # (spec ui-ux-aug19 D4.2/D4.3).
        if self._collections_token and self._proxy_sessions is None:
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
            for agent in self._agents.values()
            if self._has_pending_lifecycle(agent)
            or agent.state not in (AgentState.IDLE, AgentState.CRASHED)
        )

    @staticmethod
    def _has_pending_lifecycle(agent: AgentProcess) -> bool:
        return (
            agent.cleanup_pending
            or agent.pending_completion is not None
            or agent.pending_failure is not None
            or agent.outcome_lock.locked()
        )

    def can_spawn(self) -> bool:
        """Check if we can spawn another agent process."""
        return self.active_count < self._max_agents

    def get_agent_current_task(self, agent_name: str) -> str | None:
        """Return the task_id the agent is currently working on, or None."""
        agent = self._agents.get(agent_name)
        if agent:
            return agent.current_task_id
        tasks = {
            item.current_task_id
            for item in self._agents.values()
            if item.agent_name == agent_name
            and self._agent_busy(item)
            and item.current_task_id
        }
        return next(iter(tasks)) if len(tasks) == 1 else None

    def get_task_execution_marker(self, agent_name: str, task_id: str) -> str | None:
        agent = self.get_task_agent(agent_name, task_id)
        if agent and task_id == (agent.execution_task_id or agent.current_task_id):
            return agent.execution_marker or None
        return None

    def get_agent_state(self, agent_name: str) -> AgentState:
        """Get the current state of a named agent."""
        agent = self._agents.get(agent_name)
        if agent is None:
            agent = next(
                (
                    item
                    for item in self._agents.values()
                    if item.agent_name == agent_name and self._agent_busy(item)
                ),
                None,
            )
        return agent.state if agent else AgentState.IDLE

    def is_agent_busy(self, agent_name: str) -> bool:
        """Check if an agent is in a non-assignable state."""
        return any(
            self._agent_busy(agent)
            for key, agent in self._agents.items()
            if key == agent_name or agent.agent_name == agent_name
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
        # Retained logical Agents live in PostgreSQL. Their finished process
        # objects are only a short-lived telemetry cache, not permanent memory.
        for key, agent in list(self._agents.items()):
            if (
                agent.agent_instance_id
                and not self._agent_busy(agent)
                and agent.process is None
                and not agent.execution_marker
                and time.time() - agent.observed_at > 300
            ):
                self._agents.pop(key, None)
                self._write_locks.pop(key, None)
                self._locks.pop(key, None)
        reset: list[str] = []
        for name, agent in self._agents.items():
            if self._has_pending_lifecycle(agent) or agent.execution_marker:
                continue
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

    def _agent_status(self, agent: AgentProcess) -> dict:
        return {
            "status": (
                AgentState.WORKING.value
                if self._has_pending_lifecycle(agent)
                else agent.state.value
            ),
            "pid": agent.pid,
            "current_task": agent.current_task_id
            or (
                (agent.killed_task_id or agent.execution_task_id)
                if self._has_pending_lifecycle(agent)
                else None
            ),
            "execution_cleanup_pending": agent.cleanup_pending,
            "execution_cleanup_failed": agent.cleanup_failed,
            "execution_finalization_pending": agent.pending_failure is not None
            or agent.completion_failed,
            "execution_isolation": (
                "task_container"
                if agent.execution_container_managed
                else "office_container"
            ),
            "execution_container_id": agent.execution_container_id or None,
            "uptime": time.monotonic() - agent.started_at if agent.started_at else 0,
        }

    def get_instance_statuses(self) -> dict[str, dict]:
        return {
            agent.agent_instance_id: {
                **self._agent_status(agent),
                "agent_instance_id": agent.agent_instance_id,
                "profile_id": agent.profile_id,
                "agent_name": agent.agent_name,
                "task_id": agent.execution_task_id,
                "attempt_id": agent.execution_attempt_id,
                "execution_generation": agent.execution_generation,
                "execution_mode": agent.execution_mode,
                "observed_at": agent.observed_at,
            }
            for agent in self._agents.values()
            if agent.agent_instance_id
        }

    def get_all_statuses(self) -> dict[str, dict]:
        """Legacy profile summary; a sibling's completion cannot mark it idle."""
        groups: dict[str, list[AgentProcess]] = {}
        for agent in self._agents.values():
            groups.setdefault(agent.agent_name, []).append(agent)
        result = {}
        for name, agents in groups.items():
            busy = [agent for agent in agents if self._agent_busy(agent)]
            status = self._agent_status((busy or agents)[-1])
            if len(busy) > 1:
                status["current_task"] = None
                status["pid"] = None
            if any(agent.agent_instance_id for agent in agents):
                status["running_count"] = len(busy)
                status["retained_count"] = len(agents)
            result[name] = status
        return result

    def active_execution_count(self) -> int:
        return sum(
            self._has_pending_lifecycle(agent)
            or agent.state in (AgentState.SPAWNING, AgentState.WORKING)
            or (agent.role == "worker" and bool(agent.current_task_id or agent.execution_marker))
            for agent in self._agents.values()
        )

    # -----------------------------------------------------------------
    # Public: spawn worker
    # -----------------------------------------------------------------

    async def spawn_worker(
        self,
        agent_name: str,
        agent_config: dict[str, Any],
        task_data: dict[str, Any],
        *,
        admission_token: str | None = None,
        admission_locked: bool = False,
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
        task_id = str(task_data.get("task_id") or "")
        reservation = None
        if self._runtime_state is not None:
            from src.runtime_state import AdmissionPaused

            try:
                if admission_token is not None:
                    if not self._runtime_state.owns_reservation(admission_token):
                        return False
                else:
                    reservation = self._runtime_state.reserve("worker", task_id)
            except AdmissionPaused:
                return False
        try:

            async def admitted_spawn() -> bool:
                async with self._task_locks.setdefault(task_id, asyncio.Lock()):
                    return await self._spawn_worker(agent_name, agent_config, task_data)

            if admission_locked:
                return await admitted_spawn()
            async with self.admission_lock:
                return await admitted_spawn()
        finally:
            if reservation is not None:
                self._runtime_state.release(reservation)

    async def _spawn_worker(
        self,
        agent_name: str,
        agent_config: dict[str, Any],
        task_data: dict[str, Any],
    ) -> bool:
        current_profile_skills = agent_config.get("skills") or []
        task_data = {**task_data, "agent_execution_policy": self.execution_policy}
        task_id = str(task_data.get("task_id") or "")
        async with self._get_lock(agent_name):
            if self._runtime_state is not None and self._runtime_state.quota_status()["state"] != "running":
                return False
            if task_id in self._suppressed_tasks:
                return False
            for other_name, other in self._agents.items():
                if (
                    other_name != agent_name
                    and task_id
                    and task_id == (other.execution_task_id or other.current_task_id)
                    and (self.is_agent_busy(other_name) or other.execution_marker)
                ):
                    return False
            if not self.profile_can_spawn(agent_name):
                logger.warning(
                    "Cannot spawn %s: already busy (state=%s)",
                    agent_name,
                    self.get_agent_state(agent_name).value,
                )
                return False
            if not self.resources_available(agent_config, task_data):
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
            if old:
                await self._kill_process(agent_name, expected=old)
                for task in (old.reader_task, old.monitor_task, old.heartbeat_task):
                    if task and not task.done():
                        task.cancel()
                logger.debug("Cleaned up old %s process (PID %s)", agent_name, old.pid)

            if not self.can_spawn():
                return False

            self._deferred_executions.discard(task_id)
            isolated = self._execution_containers is not None and not task_id.startswith(("planner-", "flow-consult-"))
            if isolated and (
                not await self._execution_containers.available()
                or not await self._execution_containers.task_available(task_id)
            ):
                self._deferred_executions.add(task_id)
                logger.warning("Task %s has retained isolated execution; reconciliation is required", task_id)
                return False

            attempt_id = str(uuid.uuid4())
            claim = {}
            if self._execution_claimer is not None and not task_id.startswith(("planner-", "flow-consult-")):
                from src.execution_claim import ExecutionClaimDeferred

                try:
                    claim = await self._execution_claimer(agent_name, task_data, attempt_id)
                except ExecutionClaimDeferred as exc:
                    self._deferred_executions.add(task_id)
                    logger.info("Execution pickup deferred for %s (%s)", task_id, exc)
                    return False
                except Exception:
                    self._deferred_executions.discard(task_id)
                    logger.exception("Execution claim unavailable for %s; no worker spawned", task_id)
                    return False
                self._deferred_executions.discard(task_id)
                task_data = {
                    **task_data,
                    "agent_instance_id": claim.get("agent_instance_id", ""),
                    "profile_id": claim.get("profile_id", ""),
                    "profile_revision": claim.get("profile_revision", ""),
                    "agent_execution_policy": self.execution_policy,
                    "execution_attempt_id": claim["attempt_id"],
                    "execution_cycle": claim["execution_cycle"],
                    "execution_generation": claim["execution_generation"],
                    "execution_assignee": claim["expected_assigned_agent"],
                    "review_retry_epoch": claim.get("review_retry_epoch", 0),
                }
                if claim.get("effective_agent_config"):
                    current_config = agent_config
                    agent_config = dict(claim["effective_agent_config"])
                    # A retained playbook never restores a revoked credential.
                    enabled_connectors = {
                        connector.get("id")
                        for connector in current_config.get("connectors", [])
                        if connector.get("is_enabled", True)
                    }
                    agent_config["connectors"] = [
                        connector
                        for connector in agent_config.get("connectors", [])
                        if connector.get("id") in enabled_connectors
                    ]
                    pinned_secrets = agent_config.get("secret_env_allowlist")
                    current_secrets = current_config.get("secret_env_allowlist")
                    agent_config["secret_env_allowlist"] = (
                        current_secrets
                        if pinned_secrets is None
                        else (
                            pinned_secrets
                            if current_secrets is None
                            else [
                                name
                                for name in pinned_secrets
                                if name in current_secrets
                            ]
                        )
                    )
                    task_data["prior_session_id"] = claim.get("prior_session_id") or ""
                if self._runtime_state is not None:
                    previous_task = {
                        **task_data,
                        "execution_generation": claim["execution_generation"] - 1,
                        "assigned_agent": claim["expected_assigned_agent"],
                        "reviewer": agent_name if task_data.get("status") == "review" else task_data.get("reviewer"),
                    }
                    quota_session = self._runtime_state.quota_session(previous_task)
                    if quota_session:
                        task_data["prior_session_id"] = quota_session

            admission_closed = (
                task_id in self._suppressed_tasks
                or not self.profile_can_spawn(
                    agent_name, excluded_attempt_id=attempt_id
                )
                or (
                    self._runtime_state is not None
                    and self._runtime_state.quota_status()["state"] != "running"
                )
            )
            if admission_closed and not claim.get("runtime_release_required"):
                return False
            now = time.monotonic()
            execution_cycle = task_data.get("execution_cycle", 0)
            if self._runtime_state is not None:
                self._runtime_state.observe_cycle(task_id, execution_cycle)
            agent = AgentProcess(
                agent_name=agent_name,
                role="worker",
                agent_instance_id=task_data.get("agent_instance_id", ""),
                profile_id=task_data.get("profile_id", ""),
                runtime_release_required=(
                    bool(claim.get("runtime_release_required"))
                    if self._execution_claimer is not None
                    and not task_id.startswith(("planner-", "flow-consult-"))
                    else False
                ),
                execution_resources=(
                    list(claim.get("execution_resources") or [])
                    if self._execution_claimer is not None
                    and not task_id.startswith(("planner-", "flow-consult-"))
                    else []
                ),
                execution_marker="",
                execution_container_managed=False,
                execution_task_id=task_id,
                execution_cycle=execution_cycle,
                execution_generation=task_data.get("execution_generation", 0),
                review_retry_epoch=task_data.get("review_retry_epoch", 0),
                execution_assignee=task_data.get("execution_assignee", ""),
                execution_attempt_id=attempt_id,
                execution_mode=(
                    "review"
                    if task_data.get("status") == "review"
                    else "triage" if task_data.get("status") == "blocked" else "execute"
                ),
                current_task_id=task_id,
                state=AgentState.SPAWNING,
                started_at=now,
                last_message_at=now,
            )
            runtime_key = agent.runtime_key
            previous = self._agents.get(runtime_key)
            if previous is not None:
                await self._kill_process(runtime_key, expected=previous)
            self._agents[runtime_key] = agent

            try:
                if admission_closed:
                    await self._abort_worker_admission(agent)
                    self._agents.pop(runtime_key, None)
                    return False
                if task_id in self._suppressed_tasks or (
                    self._runtime_state is not None
                    and self._runtime_state.quota_status()["state"] != "running"
                ):
                    await self._abort_worker_admission(agent)
                    return False
                if agent.agent_instance_id:
                    from pathlib import Path
                    from src.agent_instance_workspace import prepare_instance_workspace
                    from src.orchestrator.worker_prompt import task_output_dir

                    if self._runtime_state is None:
                        raise RuntimeError("Task agents require durable runtime state")
                    task_data["output_dir"] = task_output_dir(task_data)
                    archive_root = (
                        Path(self._runtime_state.database_path).parent
                        / "agent_snapshots"
                        / self._office_id
                    )
                    task_data["agent_workspace"] = await asyncio.to_thread(
                        prepare_instance_workspace,
                        self._workspace,
                        archive_root,
                        agent_config,
                        task_data,
                        current_skills=current_profile_skills,
                    )
                # Snapshot preparation cannot launch a container or worker.
                # Only mark pool ownership immediately before prepare creates
                # its durable reservation. Otherwise a failed snapshot asks
                # cleanup to stop a nonexistent pool attempt forever.
                agent.execution_marker = secrets.token_hex(32)
                if self._runtime_state is not None and agent.runtime_release_required:
                    self._runtime_state.record_worker_launch(
                        agent.execution_attempt_id, agent.execution_marker, isolated
                    )
                agent.execution_container_managed = isolated
                if isolated:
                    identity = await self._execution_containers.prepare(task_id, agent.execution_attempt_id)
                    agent.execution_container_id = identity.container_id
                    if task_id in self._suppressed_tasks or (
                        self._runtime_state is not None and self._runtime_state.quota_status()["state"] != "running"
                    ):
                        await self._abort_worker_admission(agent)
                        return False
                cmd = self._resolve_agent_argv()
                worker_env = self._build_subprocess_env()
                self._bind_proxy_session(agent, task_data, worker_env)
                from src.docker.task_process_cleanup import WORKER_EXECUTION_ENV

                worker_env[WORKER_EXECUTION_ENV] = agent.execution_marker
                worker_env["CUBICLE_AGENT_INSTANCE_ID"] = agent.agent_instance_id
                worker_env["CUBICLE_PROFILE_ID"] = agent.profile_id
                worker_env["CUBICLE_EXECUTION_ATTEMPT_ID"] = agent.execution_attempt_id
                worker_env["CUBICLE_EXECUTION_CYCLE"] = str(agent.execution_cycle)
                worker_env["CUBICLE_EXECUTION_GENERATION"] = str(agent.execution_generation)
                worker_env["CUBICLE_REVIEW_RETRY_EPOCH"] = str(agent.review_retry_epoch)
                worker_env["CUBICLE_EXECUTION_ASSIGNEE"] = agent.execution_assignee
                agent.spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
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
                    cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                ))
                process = await asyncio.shield(agent.spawn_task)
            except asyncio.CancelledError:
                await self._abort_worker_admission(agent)
                raise
            except Exception as exc:
                logger.error(
                    "Failed to spawn process for %s: %s", agent_name, exc
                )
                from src.docker.execution_ledger import ExecutionCapacityUnavailable

                if isinstance(exc, ExecutionCapacityUnavailable):
                    self._deferred_executions.add(task_id)
                    await self._abort_worker_admission(agent)
                    return False
                agent.state = AgentState.CRASHED
                self._revoke_proxy_session(agent)
                self._retain_failure(agent, {
                    "type": "error", "fatal": True, "reason": "spawn_failed",
                    "message": "Worker process could not be launched", "task_id": task_id,
                })
                if agent.execution_container_managed:
                    await self._kill_process(runtime_key, expected=agent)
                else:
                    agent.execution_marker = ""
                    await self._cleanup_execution(agent)
                await self._report_failure(agent, agent.pending_failure)
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
                self._reader_loop(runtime_key, process.stdout, agent)
            )

            # Wait for "ready" message
            try:
                await asyncio.wait_for(
                    self._wait_for_ready(runtime_key),
                    timeout=SPAWN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Agent %s did not become ready within %ds",
                    agent_name,
                    SPAWN_TIMEOUT_SECONDS,
                )
                failure = self._retain_boot_failure(agent, "ready_timeout")
                await self._kill_process(runtime_key)
                agent.state = AgentState.CRASHED
                await self._report_failure(agent, failure)
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
                failure = self._retain_boot_failure(agent, "ready_failed")
                await self._kill_process(runtime_key)
                agent.state = AgentState.CRASHED
                await self._report_failure(agent, failure)
                return False
            except asyncio.CancelledError:
                await self._abort_worker_admission(agent)
                raise

            if task_id in self._suppressed_tasks or (
                self._runtime_state is not None and self._runtime_state.quota_status()["state"] != "running"
            ):
                await self._abort_worker_admission(agent)
                return False

            # Agent is ready -- assign the task
            agent.state = AgentState.WORKING
            agent.current_task_id = task_data.get("task_id", "")
            agent.current_readable_id = task_data.get("readable_id", "")

            # Inject container_name into agent_config so the subprocess can
            # invoke Claude CLI via docker exec
            config_with_url = {
                **agent_config,
                "_container_name": agent.execution_container_id or self._container_name,
            }

            assign_msg = {
                "type": "assign_task",
                **task_data,
                "agent_config": config_with_url,
                "workspace_path": self._workspace,
                "backend_url": self._backend_url,
                "office_id": self._office_id,
            }
            try:
                await self._send_to_agent(runtime_key, assign_msg)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    await self._abort_worker_admission(agent)
                else:
                    self._retain_boot_failure(agent, "assignment_failed")
                    await self._kill_process(runtime_key, expected=agent)
                    await self._report_failure(agent, agent.pending_failure)
                raise

            # Monitor process exit in background. Pass OUR AgentProcess
            # record explicitly (Issue 3) — a later spawn can replace
            # ``self._agents[agent_name]`` before/while the monitor runs,
            # and a name-based lookup would mutate the REPLACEMENT.
            agent.monitor_task = asyncio.create_task(
                self._monitor_exit(runtime_key, agent)
            )

            # Amendment A4: Start heartbeat monitoring
            agent.heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(runtime_key, agent)
            )

            if self._runtime_state is not None and task_data.get("script_handoff_results"):
                self._runtime_state.resume_script_handoff(task_id)

            if self._runtime_state is not None and task_data.get("capacity_wait_resume"):
                self._runtime_state.complete_capacity_resume(task_id, attempt_id)

            return True

    async def _abort_worker_admission(self, agent: AgentProcess) -> None:
        """Quiesce an interrupted pickup and acknowledge its durable claim.

        Capacity deferral or cancellation is not a crash or a task status
        change. Retain its exact Stop intent if cleanup/release fails, then
        forget the journal only after physical quiescence is confirmed.
        """
        agent.stop_requested = True
        agent.cleanup_pending = True
        if self._runtime_state is not None:
            self._runtime_state.record_worker_stop(agent.execution_attempt_id)
        await self._kill_process(agent.runtime_key, expected=agent)

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

            old = self._agents.get(agent_name)
            if old is not None:
                await self._kill_process(agent_name, expected=old)

            now = time.monotonic()
            agent = AgentProcess(
                agent_name=agent_name,
                role="manager",
                execution_marker=secrets.token_hex(32),
                state=AgentState.SPAWNING,
                started_at=now,
                last_message_at=now,
            )
            self._agents[agent_name] = agent

            try:
                cmd = self._resolve_agent_argv()
                manager_env = self._build_subprocess_env()
                self._bind_proxy_session(agent, {}, manager_env)
                from src.docker.task_process_cleanup import WORKER_EXECUTION_ENV

                manager_env[WORKER_EXECUTION_ENV] = agent.execution_marker
                agent.spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
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
                    cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                ))
                process = await asyncio.shield(agent.spawn_task)
            except asyncio.CancelledError:
                agent.cleanup_pending = True
                await self._kill_process(agent_name, expected=agent)
                raise
            except Exception as exc:
                logger.error("Failed to spawn Manager process: %s", exc)
                agent.state = AgentState.CRASHED
                agent.execution_marker = ""
                self._revoke_proxy_session(agent)
                return False

            agent.process = process
            agent.pid = process.pid

            # Amendment C-2: Dedicated reader loop for Manager
            agent.reader_task = asyncio.create_task(
                self._reader_loop(agent_name, process.stdout, agent)
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
            except asyncio.CancelledError:
                await self._kill_process(agent_name, expected=agent)
                raise

            agent.state = AgentState.READY
            logger.info("Manager process ready (PID %d)", process.pid)

            # Pass OUR record explicitly — see spawn_worker (Issue 3).
            agent.monitor_task = asyncio.create_task(
                self._monitor_exit(agent_name, agent)
            )

            # Amendment A4: Heartbeat for Manager too
            agent.heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(agent_name, agent)
            )

            return True

    # -----------------------------------------------------------------
    # Public: send chat to Manager
    # -----------------------------------------------------------------

    async def send_chat_to_manager(self, msg: dict) -> None:
        """Forward a chat message to the Manager process.

        The Manager must be READY with no unconfirmed turn. Transitions the
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
        if agent.state == AgentState.WORKING or agent.manager_turn is not None:
            raise ManagerTurnBusy("The previous Manager reply is still stopping")
        if self._proxy_sessions is not None:
            if agent.proxy_credentials is None:
                raise RuntimeError("Manager proxy identity is unavailable")
            self._proxy_sessions.bind_manager_context(
                agent.proxy_credentials, msg.get("context_key", "general_chat"),
            )
        agent.state = AgentState.WORKING
        agent.manager_turn = (
            msg.get("conversation_id", ""), msg.get("context_key", "general_chat"),
        )
        # Inject container_name so the subprocess can invoke Claude CLI
        if self._runtime_state is not None:
            self._runtime_state.invalidate_snapshot()
        enriched = {
            "type": "chat_message",
            **msg,
        }
        enriched["agent_config"] = {
            **(msg.get("agent_config") or {}),
            "_container_name": self._container_name,
        }
        try:
            await self._send_to_agent("manager", enriched)
        except Exception as exc:
            # write/drain failure is ambiguous: the CLI may already have read
            # this turn. Revoke authority and confirm exact execution cleanup;
            # never leave an alive broken-pipe process occupying WORKING forever,
            # and never make this same input eligible for transparent replay.
            try:
                await self._kill_process("manager", expected=agent)
            except Exception:
                logger.exception("Manager IPC failed and execution cleanup remains unconfirmed")
            raise RuntimeError(
                "Manager message delivery could not be confirmed. Earlier actions may have completed."
            ) from exc

    @property
    def manager_turn_active(self) -> bool:
        agent = self._agents.get("manager")
        return bool(agent and agent.state == AgentState.WORKING)

    async def wait_for_manager_turn(self, timeout: float) -> None:
        """Bound the post-timeout drain wait without rebinding a live proxy."""
        async with asyncio.timeout(timeout):
            while self.manager_turn_active:
                await asyncio.sleep(0.1)

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

    async def _reader_loop(
        self, agent_name: str, stdout, expected: AgentProcess | None = None
    ) -> None:
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
        expected = expected or self._agents.get(agent_name)
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
            if expected is not None and agent is not expected:
                continue
            if agent is not None:
                # IPC timestamps are not authority. Capture receipt time once;
                # persisted callbacks preserve it across retry and reconnect.
                if agent.agent_instance_id:
                    msg["observed_at"] = time.time()
                msg = self._execution_event(agent, msg)
            if agent:
                agent.last_message_at = time.monotonic()
                agent.observed_at = time.time()

            msg_type = msg.get("type", "")
            if agent and agent.role == "worker":
                if agent.stop_requested and msg_type not in ("ready", "pong"):
                    continue
                event_task_id = msg.get("task_id")
                if (
                    event_task_id
                    and agent.execution_task_id
                    and event_task_id != agent.execution_task_id
                ):
                    continue

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
                    if agent.role == "manager" and agent.manager_turn is not None:
                        continue
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
                    try:
                        await self._complete_worker(agent, msg)
                    except Exception:
                        logger.exception(
                            "Task completion withheld until execution cleanup succeeds: %s",
                            agent.execution_task_id or agent.current_task_id,
                        )
                    continue

            # A timeout releases the controller's chat lock before the CLI has
            # necessarily stopped. Only the dispatched turn's final/error can
            # release this process; a late final must not free a newer turn.
            manager_completion = False
            if agent and agent.role == "manager" and (
                msg_type == "response_final"
                or (msg_type == "error" and not msg.get("fatal") and msg.get("conversation_id"))
            ):
                turn = agent.manager_turn
                if (
                    agent.kill_initiated or agent.stop_requested or agent.cleanup_pending
                    or turn is None
                    or msg.get("conversation_id", "") != turn[0]
                    or msg.get("context_key", turn[1]) != turn[1]
                ):
                    continue
                manager_completion = True

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
                        self._on_event(agent.agent_name if agent else agent_name, msg),
                        timeout=timeout,
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

            # Wait for the response/session callback before admitting a queued
            # turn, so its late context hydration observes the completed reply.
            if (
                manager_completion and self._agents.get(agent_name) is agent
                and not (agent.kill_initiated or agent.stop_requested or agent.cleanup_pending)
            ):
                agent.manager_turn = None
                agent.state = AgentState.READY

            # NOW transition worker to IDLE — after _on_event has finished
            # the unassign/cleanup HTTP calls.  The dispatcher can only
            # see this agent as available from this point onward.
            if msg_type == "task_complete":
                if agent:
                    if not agent.cleanup_pending:
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
        - Completed work or explicit Stop transitions to IDLE.
        - Unexpected exits, including code 0 without completion, report failure.
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
        if not agent or (not agent.process and agent.exit_code is None):
            return

        # Capture the process handle — ``_kill_process`` nulls
        # ``agent.process`` and must not break the in-flight wait.
        process = agent.process
        exit_code = await process.wait() if process is not None else agent.exit_code
        self._revoke_proxy_session(agent)
        agent.exit_code = exit_code
        if (
            not agent.kill_initiated
            and agent.reader_task
            and not agent.reader_task.done()
        ):
            try:
                await asyncio.wait_for(asyncio.shield(agent.reader_task), timeout=35)
            except asyncio.TimeoutError:
                logger.warning("Completion drain timed out for %s", agent_name)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                logger.exception("Worker event reader failed while draining %s", agent_name)
        # T1.1.8 (G19): if _kill_process's continuation won the race and
        # already nulled ``current_task_id``, fall back to the frozen
        # ``killed_task_id`` snapshot so the fatal error event below
        # still carries the task and crash-recovery routing fires.
        task_id = agent.current_task_id or agent.killed_task_id

        if agent.pending_completion is not None:
            return
        unfinished_worker_exit = (
            agent.role == "worker" and bool(task_id)
            and not agent.completion_delivered and not agent.stop_requested
        )
        if unfinished_worker_exit and not agent.fatal_error_emitted:
            self._retain_failure(agent, {
                "type": "error", "fatal": True, "task_id": task_id,
                "reason": "missing_completion" if exit_code == 0 else "process_exit",
                "message": (
                    "Agent process exited without reporting task completion"
                    if exit_code == 0 else f"Agent process exited with code {exit_code}"
                ),
            })
        if agent.execution_marker:
            try:
                await self._cleanup_execution(agent)
            except Exception:
                logger.exception(
                    "Worker %s exited but container cleanup is unconfirmed", agent_name
                )
                return
        if agent.exit_handled:
            return
        agent.exit_handled = True

        # Issue 3: only flip dict-visible state if our record is still
        # the registered one for this agent name.
        is_registered = self._agents.get(agent_name) is agent
        if not is_registered:
            logger.debug(
                "Agent %s record was replaced while monitoring PID %s — "
                "skipping state mutation for the stale record.",
                agent_name, agent.pid,
            )

        unfinished_manager_exit = (
            agent.role == "manager"
            and agent.manager_turn is not None
            and is_registered
            and not agent.kill_initiated
            and not agent.stop_requested
        )
        unfinished_exit = unfinished_manager_exit or (
            agent.role == "worker"
            and bool(task_id)
            and not agent.completion_delivered
            and not agent.stop_requested
        )
        if exit_code == 0 and not unfinished_exit:
            logger.info(
                "Agent %s exited cleanly (PID %d)",
                agent_name,
                agent.pid or 0,
            )
            if is_registered and not agent.cleanup_pending:
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
        try:
            if task_id and (exit_code != 0 or unfinished_exit):
                await self._report_failure(agent, {
                    "type": "error",
                    "message": (
                        "Agent process exited without reporting task completion"
                        if exit_code == 0
                        else f"Agent process exited with code {exit_code}"
                    ),
                    "reason": "missing_completion" if exit_code == 0 else "process_exit",
                    "task_id": task_id,
                    "fatal": True,
                })
            elif unfinished_manager_exit:
                conversation_id, context_key = agent.manager_turn
                await self._report_failure(agent, {
                    "type": "error",
                    "message": f"Manager process exited before completing its reply (code {exit_code})",
                    "reason": "process_exit",
                    "conversation_id": conversation_id,
                    "context_key": context_key,
                    "fatal": True,
                })
        finally:
            if agent.reader_task and not agent.reader_task.done():
                agent.reader_task.cancel()
            if agent.heartbeat_task and not agent.heartbeat_task.done():
                agent.heartbeat_task.cancel()
            agent.process = None
            agent.pid = None
            if not agent.cleanup_pending:
                agent.current_task_id = None
                agent.current_readable_id = None

    async def _report_failure(
        self, agent: AgentProcess, event: dict[str, Any] | None,
    ) -> None:
        async with agent.outcome_lock:
            if event is None or agent.pending_completion is not None or agent.completion_delivered:
                return
            if not agent.stop_requested and not agent.fatal_error_emitted:
                event = self._retain_failure(agent, event)
            if agent.cleanup_pending or agent.execution_marker:
                return
            if agent.stop_requested or agent.fatal_error_emitted:
                agent.pending_failure = None
                return
            event = self._execution_event(agent, agent.pending_failure or event)
            agent.pending_failure = event
            self._record_failure(agent)
            try:
                if self._on_event:
                    await asyncio.wait_for(
                        self._on_event(agent.agent_name, event), timeout=30,
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    "Worker failure callback for %s exceeded 30s; retaining finalization for retry",
                    agent.agent_name,
                )
                return
            except Exception:
                logger.exception("Worker failure callback failed for %s", agent.agent_name)
                return
            if self._runtime_state is not None and agent.role == "worker":
                try:
                    self._runtime_state.acknowledge_completion(agent.execution_attempt_id)
                except Exception:
                    logger.exception("Failure acknowledgement remains pending for %s", agent.agent_name)
                    return
            agent.fatal_error_emitted = True
            agent.pending_failure = None
            if self._agents.get(agent.runtime_key) is agent and agent.process is None:
                if agent.state == AgentState.WORKING:
                    agent.state = AgentState.CRASHED
                agent.current_task_id = None

    # -----------------------------------------------------------------
    # Internal: heartbeat (Amendment A4)
    # -----------------------------------------------------------------

    async def _heartbeat_loop(
        self, agent_name: str, expected: AgentProcess | None = None
    ) -> None:
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
        expected = expected or self._agents.get(agent_name)
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

            agent = self._agents.get(agent_name)
            if expected is not None and agent is not expected:
                break
            if not agent or not agent.process:
                break
            if agent.pending_completion is not None or agent.completion_delivered:
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
                failure = {
                    "type": "error",
                    "agent_name": agent_name,
                    "fatal": True,
                    "reason": "heartbeat_timeout",
                    "task_id": task_id,
                    "elapsed_seconds": outstanding,
                }
                if self._retain_failure(agent, failure) is None:
                    break
                try:
                    async with self._get_lock(agent_name):
                        await self._kill_process(agent_name, expected=agent)
                    await self._report_failure(agent, failure)
                except Exception:
                    logger.exception(
                        "Heartbeat cleanup remains unconfirmed for %s; retaining its slot",
                        agent_name,
                    )
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
                if agent.role == "manager":
                    turn = agent.manager_turn
                    failure = {
                        "type": "error", "fatal": True,
                        "reason": "ipc_failure", "message": "Manager control connection failed",
                        **({"conversation_id": turn[0], "context_key": turn[1]} if turn else {}),
                    }
                    if turn:
                        agent.pending_failure = failure
                    try:
                        async with self._get_lock(agent_name):
                            await self._kill_process(agent_name, expected=agent)
                        if turn:
                            await self._report_failure(agent, failure)
                    except Exception:
                        logger.exception("Manager control cleanup remains unconfirmed; retaining its slot")
                break

    # -----------------------------------------------------------------
    # Internal: process termination
    # -----------------------------------------------------------------

    async def stop_task(
        self, agent_name: str, task_id: str, *, expected_mode: str | None = None,
        expected_execution_marker: str | None = None,
    ) -> bool:
        """Stop only the named task, serialized against spawning its successor."""
        if not task_id:
            return False
        async with self._get_lock(agent_name):
            agent = self.get_task_agent(agent_name, task_id)
            current_task = (
                agent.current_task_id or (
                    agent.execution_task_id or agent.killed_task_id
                    if (
                        agent.execution_marker or agent.cleanup_pending
                        or agent.pending_failure is not None or agent.pending_completion is not None
                    ) else None
                )
                if agent is not None else None
            )
            if agent is None or current_task != task_id:
                return False
            if expected_mode and agent.execution_mode != expected_mode:
                return False
            if expected_execution_marker and agent.execution_marker != expected_execution_marker:
                return False
            agent.stop_requested = True
            if self._runtime_state is not None:
                self._runtime_state.record_worker_stop(agent.execution_attempt_id)
            await self._kill_process(agent.runtime_key, expected=agent)
            return True

    async def _cleanup_execution(self, agent: AgentProcess) -> None:
        self._revoke_proxy_session(agent)
        async with agent.cleanup_lock:
            if not agent.execution_marker and (
                not agent.runtime_release_required or agent.runtime_released
            ):
                return
            agent.cleanup_pending = True
            from src.docker.task_process_cleanup import terminate_worker_execution

            try:
                if agent.execution_marker:
                    if agent.execution_container_managed:
                        await self._execution_containers.stop_attempt(
                            agent.execution_task_id, agent.execution_attempt_id
                        )
                    else:
                        await terminate_worker_execution(
                            self._container_name, agent.execution_marker
                        )
                    agent.execution_marker = ""
                if agent.runtime_release_required and not agent.runtime_released:
                    if self._execution_releaser is None:
                        raise RuntimeError(
                            "Execution cleanup acknowledgment is unavailable"
                        )
                    await self._execution_releaser(
                        agent.execution_task_id,
                        agent.execution_attempt_id,
                        agent.agent_instance_id,
                        session_id=(agent.pending_completion or {}).get("session_id")
                        or None,
                    )
                    agent.runtime_released = True
            except BaseException:
                agent.cleanup_failed = True
                raise
            agent.cleanup_pending = False
            agent.cleanup_failed = False
            agent.execution_marker = ""
            agent.observed_at = time.time()

    async def _complete_worker(
        self, agent: AgentProcess, message: dict[str, Any] | None = None,
    ) -> None:
        async with agent.outcome_lock:
            if (
                self._agents.get(agent.runtime_key) is not agent
                or agent.stop_requested
                or agent.completion_delivered
                or agent.pending_failure is not None
                or agent.fatal_error_emitted
            ):
                return
            if message is not None and agent.pending_completion is None:
                agent.pending_completion = self._execution_event(agent, message)
            completion = agent.pending_completion
            if completion is None:
                return
            if self._runtime_state is not None and not agent.execution_task_id.startswith(("planner-", "flow-consult-")):
                try:
                    self._runtime_state.retain_completion(
                        agent.agent_name, agent.execution_attempt_id, agent.execution_task_id, completion,
                        cleanup={"execution_marker": agent.execution_marker,
                                 "container_managed": agent.execution_container_managed},
                    )
                except Exception:
                    agent.completion_failed = True
                    raise
            agent.state = AgentState.WORKING
            await self._cleanup_execution(agent)
            if self._agents.get(agent.runtime_key) is not agent or agent.stop_requested:
                return
            if self._on_event:
                try:
                    await asyncio.wait_for(
                        self._on_event(agent.agent_name, completion), timeout=30,
                    )
                except asyncio.TimeoutError:
                    agent.completion_failed = True
                    logger.warning(
                        "task_complete callback for %s exceeded 30s; retaining completion for reconciliation",
                        agent.agent_name,
                    )
                    return
                except Exception:
                    agent.completion_failed = True
                    logger.exception("Error in task_complete callback for %s", agent.agent_name)
                    return
            if self._runtime_state is not None:
                try:
                    self._runtime_state.acknowledge_completion(agent.execution_attempt_id)
                except Exception:
                    agent.completion_failed = True
                    logger.exception("Completion acknowledgment remains pending for %s", agent.agent_name)
                    return
            agent.current_task_id = None
            agent.current_readable_id = None
            agent.pending_completion = None
            agent.completion_delivered = True
            agent.observed_at = time.time()
            agent.completion_failed = False
            if (
                self._agents.get(agent.runtime_key) is agent
                and not agent.cleanup_pending
            ):
                agent.state = AgentState.IDLE

    async def retry_pending_cleanup(self) -> None:
        async with self.admission_lock:
            await self._recover_worker_executions()
        for agent_name, agent in list(self._agents.items()):
            if not (
                agent.cleanup_pending
                or agent.pending_completion is not None
                or agent.pending_failure is not None
                or (agent.exit_code is not None and not agent.exit_handled)
            ):
                continue
            try:
                if agent.pending_failure is not None and not agent.stop_requested:
                    # A prior SQLite failure kept the outcome in memory. Retry
                    # durable capture before cleanup, not only before delivery.
                    self._retain_failure(agent, agent.pending_failure)
                if agent.stop_requested or agent.kill_initiated:
                    async with self._get_lock(agent_name):
                        if self._agents.get(agent_name) is not agent:
                            continue
                        if agent.cleanup_pending or (
                            agent.stop_requested and (
                                agent.pending_failure is not None
                                or agent.pending_completion is not None
                            )
                        ):
                            # Cleanup may already be confirmed while its Stop
                            # acknowledgement failed in SQLite. Retry that ack
                            # before dropping ownership or releasing the slot.
                            await self._kill_process(agent_name, expected=agent)
                    if agent.pending_failure is not None:
                        await self._report_failure(agent, agent.pending_failure)
                    if agent.exit_code is not None and not agent.exit_handled:
                        await self._monitor_exit(agent_name, agent)
                elif agent.pending_failure is not None:
                    if agent.cleanup_pending or agent.execution_marker:
                        async with self._get_lock(agent_name):
                            if self._agents.get(agent_name) is not agent:
                                continue
                            if agent.process is not None and agent.exit_code is None and agent.process.returncode is None:
                                await self._kill_process(agent_name, expected=agent)
                            else:
                                await self._cleanup_execution(agent)
                    await self._report_failure(agent, agent.pending_failure)
                elif agent.pending_completion is not None:
                    await self._complete_worker(agent)
                elif agent.exit_code is not None:
                    await self._monitor_exit(agent_name, agent)
            except Exception:
                logger.warning(
                    "Execution cleanup still pending for %s; retaining its slot",
                    agent_name, exc_info=True,
                )

    async def _kill_process(
        self, agent_name: str, *, expected: AgentProcess | None = None
    ) -> None:
        """Forcefully terminate an agent process.

        First sends SIGTERM and waits 5 seconds. If the process does not
        exit, sends SIGKILL and waits at most another 5 seconds. Worker
        executions then receive exact-marker container cleanup before the
        slot is released. Cleanup failures retain a busy, retryable slot.
        Handles ProcessLookupError (process already gone).

        Args:
            agent_name: The agent whose process to kill.
        """
        agent = self._agents.get(agent_name)
        if expected is not None and agent is not expected:
            return
        if not agent:
            return
        self._revoke_proxy_session(agent)
        if not agent.process and not agent.cleanup_pending and not agent.execution_marker:
            await self._cleanup_execution(agent)
            if agent.stop_requested:
                if self._runtime_state is not None:
                    self._runtime_state.acknowledge_completion(agent.execution_attempt_id)
                agent.pending_completion = None
                agent.pending_failure = None
                agent.completion_failed = False
                agent.current_task_id = None
                agent.state = AgentState.IDLE
            return
        process = agent.process
        if agent.current_task_id:
            agent.killed_task_id = agent.current_task_id
        agent.kill_initiated = True
        agent.cleanup_pending = bool(agent.execution_marker) or (
            agent.runtime_release_required and not agent.runtime_released
        )
        try:
            if process is None and agent.spawn_task is not None:
                try:
                    process = await asyncio.wait_for(
                        asyncio.shield(agent.spawn_task), timeout=SPAWN_TIMEOUT_SECONDS
                    )
                except Exception:
                    if (
                        not agent.spawn_task.done()
                        or agent.spawn_task.cancelled()
                        or agent.spawn_task.exception() is None
                    ):
                        raise
                agent.process = process
            if process is not None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
        except ProcessLookupError:
            pass  # Process already gone
        except BaseException:
            agent.cleanup_failed = True
            raise

        if agent.cleanup_pending:
            try:
                await self._cleanup_execution(agent)
            except Exception:
                logger.exception(
                    "Task %s host worker exited but container cleanup is unconfirmed; "
                    "agent %s remains unavailable until cancellation is retried",
                    agent.killed_task_id, agent_name,
                )
                raise

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
        if agent.stop_requested and self._runtime_state is not None:
            self._runtime_state.acknowledge_completion(agent.execution_attempt_id)
        agent.kill_initiated = True
        agent.observed_at = time.time()
        agent.state = AgentState.IDLE
        agent.manager_turn = None
        agent.current_task_id = None
        agent.current_readable_id = None
        agent.pending_completion = None
        if agent.stop_requested:
            agent.pending_failure = None
        agent.completion_failed = False
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
        if self._config_reconciler is not None:
            await self._config_reconciler.close()

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

        failures = []
        for agent_name, agent in list(self._agents.items()):
            try:
                await self._kill_process(agent_name, expected=agent)
            except Exception as exc:
                failures.append((agent_name, exc))
                logger.exception("Shutdown cleanup remains unconfirmed for %s", agent_name)
            else:
                if self._agents.get(agent_name) is agent:
                    self._agents.pop(agent_name)
        if self._execution_containers is not None:
            try:
                await self._execution_containers.stop_all()
            except Exception as exc:
                failures.append(("isolated-executions", exc))
                logger.exception("Shutdown retained isolated executions remain unconfirmed")
        if failures:
            raise RuntimeError(
                "Shutdown has unconfirmed worker executions: "
                + ", ".join(name for name, _ in failures)
            )
        if self._execution_containers is not None:
            await self._execution_containers.close()
        logger.info("All agent processes shut down")
