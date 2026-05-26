"""agent_worker.py -- Agent subprocess entry point.

This module is the main entry point for each agent subprocess spawned by the
Orchestrator. It reads NDJSON commands from stdin and writes NDJSON events
to stdout.

Usage (spawned by Orchestrator):
    python -m src.agent_worker --role worker --agent-name analyst
    python -m src.agent_worker --role manager --agent-name manager

The process lifecycle:
1. Parse args and initialize logging (stderr -> log file).
2. Set up signal handlers (SIGTERM, SIGINT -> graceful shutdown).
3. Send "ready" message to Orchestrator via stdout.
4. Enter command loop: read stdin, dispatch to handler.
5. On "assign_task": run Claude SDK session, stream events.
6. On "chat_message": run Manager SDK query, stream response.
7. On "tool_response": resolve pending tool call future.
8. On "cancel_task": cancel current operation.
9. On "shutdown": exit gracefully within grace period.
10. On "ping": respond with "pong".

IPC contract:
- stdout is EXCLUSIVELY for NDJSON messages. No print() calls allowed.
- stderr is redirected to a log file. All logging goes to stderr.
- stdin reads NDJSON commands from the Orchestrator.

Amendment C-1: Tool call futures timeout after 60 seconds. If the
Orchestrator does not respond to a tool_call within 60s, the agent
sends an ERROR message and the tool call fails with a timeout error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Awaitable, Callable

from src.agent_protocol import MessageType, serialize
from src.orchestrator.error_classifier import classify_error

# Maximum number of CLI-session attempts per task before escalating.
# Counts the INITIAL attempt + retries. 3 = one primary try + up to two
# recoveries. Set conservatively — more than 2 retries usually means a
# systemic issue better handled by MA than by automatic retry.
_MAX_SESSION_ATTEMPTS = 3

# P2-E: Wall-clock budget across the entire retry loop. Without this,
# a worker can theoretically burn ``_MAX_SESSION_ATTEMPTS × per-attempt
# CLI timeout`` (~12 hours at default settings) on a single task before
# escalating. The wall-clock cap escalates faster on slow-burn failures
# and gives the Manager Assistant a chance to react.
_MAX_SESSION_WALLCLOCK_SECONDS = 6 * 60 * 60  # 6 hours

# Upper bound on the dynamically-extended system prompt. After each
# retry we append an "AUTOMATIC RECOVERY" guidance block; without a cap,
# a stuck task could bloat the prompt across retries. 200 KB comfortably
# holds the base worker prompt plus several guidance blocks while staying
# well under the model context window.
_MAX_SYSTEM_PROMPT_SIZE = 200_000

# Character lengths for truncating error text in different surfaces.
# These differ because logs can be noisy but blocked-task comments need
# enough context for MA to act. Keep them distinct and centralised.
_ERROR_PREVIEW_LENGTH = 400          # activity content + progress events
_ESCALATION_ORIGINAL_LENGTH = 600    # blocked-task comment to MA


class AgentErrorEscalation(Exception):
    """Raised by _run_sdk_session when retries are exhausted or the error
    is non-retryable.

    The _handle_assign_task handler catches this and emits a task_complete
    message with status=blocked + a structured escalation message, so the
    backend can surface the problem to the Manager Assistant.
    """

    def __init__(
        self,
        *,
        error_class: str,
        original_error: str,
        escalation_message: str,
        session_id: str | None = None,
        total_cost: float | None = None,
    ) -> None:
        super().__init__(
            f"[{error_class}] {escalation_message} "
            f"(original: {original_error[:_ERROR_PREVIEW_LENGTH]})"
        )
        self.error_class = error_class
        self.original_error = original_error
        self.escalation_message = escalation_message
        self.session_id = session_id
        self.total_cost = total_cost

logger = logging.getLogger("cbcl.agent_worker")


# Claude CLI ships a sizeable catalog of built-in tools that overlap
# with — or actively conflict with — Cubicle's domain primitives.
# They are NOT MCP tools: they are baked into the CLI, so the
# ``mcp__cubicle-tools__*`` role filter in ``mcp_tool_server.py``
# cannot exclude them. They appear in the model's tool catalog by
# default.
#
# Leaving any of these visible to Manager + Worker sessions caused
# two production problems users repeatedly reported:
#
# 1. Token-wasting self-talk. Workers seeing both Claude's tool and
#    Cubicle's MCP equivalent kept emitting checkpoint commentary
#    like "Ignoring — this single task uses the Cubicle task
#    lifecycle, not the TaskCreate harness tool. Continuing to poll."
#    Reported on TO-007.T184 — multiple such checkpoints in one
#    session, each burning tokens and cluttering the Activity feed.
#
# 2. Risk of accidental call. A model that decides to use, e.g.,
#    ``TaskCreate`` would hit a CLI built-in that has zero awareness
#    of Cubicle's Brief / lifecycle / scope rules, leaving the
#    platform-side task untouched. Same for ``CronCreate`` (Cubicle
#    has ``schedule_script``) and ``Skill`` (Cubicle has its own
#    skill system).
#
# The list below is split into three intent groups so the rationale
# for each entry is grep-able later. ``--disallowed-tools`` is the
# only mechanism that scrubs them from the model's catalog entirely.
_CLAUDE_CLI_BUILTIN_DISALLOW = [
    # Group A — task / todo / team management. These overlap directly
    # with Cubicle's Kanban + Task Brief lifecycle. Source of the
    # original "TaskCreate harness" noise (TO-007.T184).
    "TaskCreate",
    "TaskList",
    "TaskUpdate",
    "TaskGet",
    "TaskOutput",
    "TaskStop",
    "TodoWrite",
    "TeamCreate",
    "TeamDelete",
    # Group B — domain overlap with Cubicle primitives. Cubicle has
    # its own equivalents (``schedule_script`` for cron, the office
    # Skills surface for skills, the workstream chat for "ask user").
    "CronCreate",
    "CronDelete",
    "CronList",
    "Skill",
    "AskUserQuestion",
    "SendMessage",
    "Brief",
    "SendUserMessage",  # rename of Brief in newer CLI builds — same tool.
    # Group C — agent autonomy risks. These let the model touch
    # config / auth / external triggers / interactive plan-mode
    # affordances Cubicle doesn't use. Disallow defensively even if
    # the model has never tried to call them — a future model
    # behaviour change shouldn't get free reign over CLI internals.
    "Config",
    "MCP",
    "McpAuth",
    "RemoteTrigger",
    "Sleep",
    "REPL",
    "PowerShell",
    "EnterPlanMode",
    "ExitPlanMode",
]


class AgentWorker:
    """Runs inside a subprocess. Manages one agent's SDK sessions.

    The AgentWorker is the main class running inside each agent subprocess.
    It manages the command loop (reading from stdin), dispatches to handlers,
    and runs Claude SDK sessions via the container's agent runner.

    Attributes:
        role: "manager" or "worker".
        agent_name: The agent's name (e.g., "analyst", "python-developer").
        workspace_path: Path to the workspace directory.
        office_id: The office ID this agent belongs to.
        backend_url: URL of the platform backend.
    """

    def __init__(
        self,
        role: str,              # "manager" or "worker"
        agent_name: str,
        workspace_path: str,
        office_id: str,
        backend_url: str = "",
    ) -> None:
        self.role = role
        self.agent_name = agent_name
        self.workspace_path = workspace_path
        self.office_id = office_id
        self.backend_url = backend_url
        self._shutdown_event = asyncio.Event()
        self._current_task_id: str | None = None
        self._agent_config: dict = {}  # Set when task/chat is assigned
        # Chat-v2 (CHAT-005): handle to the asyncio.Task running the
        # current chat/assign work. ``CANCEL_TASK`` from the orchestrator
        # cancels this task, which propagates ``CancelledError`` down
        # into ``stream_cli_session`` and terminates the underlying
        # ``docker exec`` CLI subprocess. None when the worker is idle.
        self._current_session_task: asyncio.Task | None = None
        # Cancellation provenance for the task_errors telemetry row
        # emitted by the ``except CancelledError`` block in
        # ``_handle_assign_task``. Set by the cancel + signal +
        # shutdown handlers BEFORE they trigger the cancellation, so
        # the catch-all default ("external_cancel" — supervisor reap,
        # heartbeat timeout, container restart, anything we didn't
        # initiate ourselves) only fires when none of them ran. Reset
        # to None at the start of each ``assign_task`` so a previous
        # task's cancellation source doesn't leak into the next.
        self._cancellation_source: str | None = None

    async def run(self) -> None:
        """Main loop: read commands from stdin, dispatch to handlers.

        Runs two concurrent asyncio tasks:

        1. **Reader task** — drains stdin in a tight loop. On PING it
           replies PONG **inline** (no queueing). All other messages
           are put on the dispatch queue.
        2. **Dispatcher task** — pulls non-PING messages off the queue
           and runs the matching handler.

        This split is the fix for the heartbeat-starvation crash
        ("Agent X did not PONG within 90s — killing wedged process"):
        before the split, the single main loop blocked stdin reads
        while ``_handle_assign_task`` / ``_handle_chat_message`` were
        in the middle of a long Claude CLI call. PINGs piled up in
        the pipe, the supervisor saw no PONG for 90s, and SIGKILLed
        a perfectly healthy worker. With the reader running
        concurrently, PINGs are answered within milliseconds even
        when a Claude session has been running for an hour.

        Shutdown propagates either via SIGTERM/SIGINT, a SHUTDOWN
        message, or stdin EOF.
        """
        loop = asyncio.get_running_loop()

        # Set up signal handlers for graceful shutdown
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal)

        # Send ready message
        self._send({
            "type": MessageType.READY,
            "pid": os.getpid(),
            "agent_name": self.agent_name,
        })

        # Connect stdin to an asyncio StreamReader for non-blocking reads.
        # Default StreamReader buffer is 64KB; bump to 16MB because a single
        # NDJSON line carrying a large tool_result (e.g. an assign_task
        # payload with embedded task brief + agent config) can exceed 64KB.
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        # Bounded queue: if dispatch backs up, reader will block —
        # which is the desired backpressure (the Orchestrator won't
        # send another ASSIGN_TASK before the previous one finishes).
        # PINGs bypass this queue.
        dispatch_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)

        reader_task = asyncio.create_task(
            self._reader_loop(reader, dispatch_queue),
            name="agent_stdin_reader",
        )
        dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(dispatch_queue),
            name="agent_dispatcher",
        )

        # Wait for shutdown OR either task exiting (whichever first).
        shutdown_wait = asyncio.create_task(
            self._shutdown_event.wait(),
            name="agent_shutdown_wait",
        )
        done, pending = await asyncio.wait(
            {reader_task, dispatcher_task, shutdown_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Tear down — cancel whichever task is still running and
        # surface any exception from the one that finished.
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Worker task raised during shutdown")

        for t in done:
            if t is shutdown_wait:
                continue
            exc = t.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                logger.exception(
                    "Worker task crashed: %s", exc, exc_info=exc,
                )

    async def _reader_loop(
        self,
        reader: asyncio.StreamReader,
        dispatch_queue: "asyncio.Queue[dict]",
    ) -> None:
        """Read stdin lines and split PING (inline reply) from the rest
        (enqueue for dispatch).

        Runs concurrently with ``_dispatcher_loop`` so heartbeat PINGs
        are answered even while a long-running CLI handler holds the
        dispatch queue. See ``run`` for the full rationale.
        """
        while not self._shutdown_event.is_set():
            try:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=300,
                )
            except asyncio.TimeoutError:
                # No message for 5 minutes — send a defensive PONG.
                # The orchestrator normally PINGs every 30s; if we
                # haven't seen anything for 5 minutes either the pipe
                # is broken or the orchestrator is wedged. The PONG
                # keeps us declared "alive" until the orchestrator
                # tears the connection down explicitly.
                self._send({"type": MessageType.PONG})
                continue

            if not line:
                logger.warning("stdin closed -- Orchestrator connection lost")
                self._shutdown_event.set()
                return

            decoded = line.decode().strip()
            if not decoded:
                continue
            try:
                msg = json.loads(decoded)
            except json.JSONDecodeError:
                logger.warning(
                    "Malformed message from Orchestrator: %s", decoded[:200]
                )
                continue

            msg_type = msg.get("type", "")
            if msg_type == MessageType.PING:
                # INLINE: respond immediately, do NOT queue. Keeps the
                # supervisor's heartbeat happy even when the dispatch
                # queue is blocked behind a multi-minute CLI call.
                self._send({"type": MessageType.PONG})
                continue

            if msg_type == MessageType.CANCEL_TASK:
                # Chat-v2 (CHAT-005 review): cancel MUST bypass the
                # dispatch queue so it preempts whatever session is in
                # flight even if a subsequent CHAT_MESSAGE is sitting
                # in the queue. Pre-fix: a user who sent msg1, sent
                # msg2, then clicked Cancel — and whose IPCs arrived
                # in that order — would have Cancel cancel msg2
                # (because dispatcher was already awaiting task1 to
                # finish before processing msg2). Now Cancel reaches
                # the running task1 immediately.
                self._handle_cancel()
                continue

            # Everything else (assign_task, chat_message, shutdown,
            # tool_response) goes to the dispatcher in order.
            await dispatch_queue.put(msg)

    async def _dispatcher_loop(
        self, dispatch_queue: "asyncio.Queue[dict]",
    ) -> None:
        """Drain queued messages and run handlers.

        Chat-v2 (CHAT-005): long-running messages (``assign_task``,
        ``chat_message``) are dispatched as a background
        asyncio.Task tracked on ``self._current_session_task``.
        Successive session messages serialise by awaiting the
        previous task before spawning the next, so two CLI calls
        for the same agent never overlap.

        Inline shortcuts that BYPASS this queue and are handled in
        ``_reader_loop`` (so they can preempt an in-flight session):
        ``PING`` (heartbeat) and ``CANCEL_TASK`` (user cancel).

        Inline-here-but-still-queued: ``TOOL_RESPONSE`` and
        ``SHUTDOWN``. They reach ``_dispatch``, which handles them
        before the wait-for-prev-task block — but they DO wait for
        their turn behind earlier queued session messages. That's
        acceptable for now (SHUTDOWN gets noticed when the
        dispatcher loop iterates next; TOOL_RESPONSE is dead under
        the in-container MCP path anyway).
        """
        while not self._shutdown_event.is_set():
            try:
                msg = await asyncio.wait_for(
                    dispatch_queue.get(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue

            msg_type = msg.get("type", "")
            try:
                await self._dispatch(msg_type, msg)
            except Exception as exc:
                logger.exception(
                    "Error handling message type '%s': %s", msg_type, exc
                )
                self._send({
                    "type": MessageType.ERROR,
                    "message": f"Handler error: {exc}",
                    "task_id": self._current_task_id,
                    "fatal": False,
                })

    async def _dispatch(self, msg_type: str, msg: dict) -> None:
        """Route a message to the appropriate handler.

        Args:
            msg_type: The "type" field from the incoming message.
            msg: The full parsed message dict.
        """
        # Control messages — handled inline so they can preempt a
        # long-running session held by ``self._current_session_task``.
        # PING is NOT listed here: it's intercepted in ``_reader_loop``
        # before the dispatch queue so the supervisor's 90s heartbeat
        # stays answered even while a multi-minute CLI session blocks
        # this dispatcher.
        if msg_type == MessageType.TOOL_RESPONSE:
            self._handle_tool_response(msg)
            return
        if msg_type == MessageType.CANCEL_TASK:
            self._handle_cancel()
            return
        if msg_type == MessageType.SHUTDOWN:
            self._handle_shutdown(msg)
            return

        # Session-running messages — wait for any previous session
        # task to drain (success, error, or cancellation) before
        # spawning the next so we never run two concurrent CLI
        # sessions for the same agent.
        if (
            self._current_session_task is not None
            and not self._current_session_task.done()
        ):
            try:
                await self._current_session_task
            except (asyncio.CancelledError, Exception):
                # Previous session's exception was already logged /
                # surfaced from its own handler — don't propagate.
                pass

        if msg_type == MessageType.ASSIGN_TASK:
            self._current_session_task = asyncio.create_task(
                self._run_session_handler(
                    msg_type, msg, self._handle_assign_task,
                ),
                name="agent_session_assign_task",
            )
        elif msg_type == MessageType.CHAT_MESSAGE:
            self._current_session_task = asyncio.create_task(
                self._run_session_handler(
                    msg_type, msg, self._handle_chat_message,
                ),
                name="agent_session_chat_message",
            )
        else:
            logger.warning("Unknown message type: %s", msg_type)

    async def _run_session_handler(
        self,
        msg_type: str,
        msg: dict,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Wrap a session handler so its exceptions surface as ERROR
        IPC frames, matching the pre-Pass-C dispatcher's inline error
        path.

        Chat-v2 (CHAT-005) lets the dispatcher accept control messages
        (cancel_task, ping) while a session is running, which means the
        session handler now lives inside an asyncio.Task instead of an
        ``await self._dispatch(...)`` call. The dispatcher's
        ``except Exception:`` no longer sees handler errors — we have
        to surface them from within the task itself. ``CancelledError``
        is intentionally allowed to propagate so the cancel flow stays
        clean.
        """
        try:
            await handler(msg)
        except asyncio.CancelledError:
            # Cancel-mid-turn — the handler emits its own
            # response_final (chat) / blocked task_complete (assign).
            raise
        except Exception as exc:
            logger.exception(
                "Error handling message type '%s': %s", msg_type, exc,
            )
            self._send({
                "type": MessageType.ERROR,
                "message": f"Handler error: {exc}",
                "task_id": self._current_task_id,
                "fatal": False,
            })

    # -----------------------------------------------------------------
    # Task assignment handler (Worker role)
    # -----------------------------------------------------------------

    async def _handle_assign_task(self, msg: dict) -> None:
        """Execute a worker task using the Claude Agent SDK.

        This is the main work handler. It receives the full task data
        (brief, agent config, workspace path), builds the worker prompt,
        and runs an SDK session via the container's agent runner.

        On success: sends task_complete with status="review".
        On cancellation: sends task_complete with status="blocked".
        On error: sends error message (non-fatal, agent stays alive).

        Args:
            msg: The assign_task message dict.
        """
        task_id = msg.get("task_id", "")
        readable_id = msg.get("readable_id", "")
        task_status = msg.get("status", "ready")
        self._current_task_id = task_id
        # Fresh task → forget any cancellation source recorded for a
        # prior assignment, so an external_cancel that fires here
        # isn't misattributed to a stale shutdown / explicit_cancel
        # signal from an earlier life of this subprocess.
        self._cancellation_source = None

        is_review = task_status == "review"
        is_triage = task_status == "blocked"
        if is_review:
            mode = "REVIEW"
        elif is_triage:
            mode = "TRIAGE"
        else:
            mode = "EXECUTE"
        logger.info("Assigned task %s (%s) — mode: %s", readable_id, task_id, mode)

        try:
            agent_config = msg.get("agent_config", {})
            self._agent_config = agent_config

            # Run the SDK session
            session_id, total_cost = await self._run_sdk_session(
                agent_config=agent_config,
                task_data=msg,
            )

            # Check if task was skipped (already done or reassigned)
            if session_id is None and total_cost is None:
                logger.info("Task %s skipped (state changed)", readable_id)
                self._send({
                    "type": MessageType.TASK_COMPLETE,
                    "task_id": task_id,
                    "status": task_status,  # Keep current status
                    "comment": "Task skipped — state changed since dispatch.",
                    "token_cost": 0.0,
                    "session_id": "",
                    "is_review_completion": True,  # Don't trigger auto-unassign
                })
                return

            # Report completion — behavior depends on task state
            if is_review:
                # Reviewer: task stays in Review. The reviewer should have
                # posted findings and unassigned the task during execution.
                # We report completion but DON'T trigger status change or
                # auto-unassign — the reviewer handles that explicitly.
                self._send({
                    "type": MessageType.TASK_COMPLETE,
                    "task_id": task_id,
                    "status": "review",  # Stay in review
                    "comment": "Review complete.",
                    "token_cost": total_cost or 0.0,
                    "session_id": session_id or "",
                    "is_review_completion": True,  # Flag: don't auto-unassign
                })
            elif is_triage:
                # Triage dispatch on a blocked task. The MA (or whoever
                # was dispatched here) ran its playbook, posted a
                # synthesis comment, and exited — the task STAYS in
                # blocked. We MUST flag this as a non-status-changing
                # completion so the orchestrator's task_complete handler
                # doesn't try ``move_task(blocked → review)`` (which
                # would fail board-validation and ALSO incorrectly
                # imply the agent finished work). Without this flag
                # the handler kept attempting an invalid transition
                # every triage exit, generating spurious error logs
                # and feeding the reconciler→re-dispatch loop on
                # blocked tasks. (TO-007.T40 regression, 2026-05-14.)
                self._send({
                    "type": MessageType.TASK_COMPLETE,
                    "task_id": task_id,
                    "status": "blocked",  # Stay in blocked
                    "comment": "Triage complete.",
                    "token_cost": total_cost or 0.0,
                    "session_id": session_id or "",
                    "is_review_completion": True,  # Re-use the "don't move" flag
                })
            else:
                # Executor: move to review for Manager
                self._send({
                    "type": MessageType.TASK_COMPLETE,
                    "task_id": task_id,
                    "status": "review",
                    "comment": "Task execution complete.",
                    "token_cost": total_cost or 0.0,
                    "session_id": session_id or "",
                    "is_review_completion": False,
                })

        except asyncio.CancelledError:
            # Cancellation source is whatever the cancel/shutdown/
            # signal handlers stamped earlier. None means nothing in
            # THIS process initiated the cancel — supervisor reap,
            # heartbeat timeout, daemon restart, container kill, etc.
            # "external_cancel" is the catch-all; the backend's
            # task_errors row makes the distinction queryable.
            cancellation_source = (
                self._cancellation_source or "external_cancel"
            )
            logger.info(
                "Task %s cancelled (source=%s)",
                readable_id,
                cancellation_source,
            )
            # Telemetry event FIRST — the backend's task_activity
            # handler picks up error rows with details.error_class
            # and writes a queryable task_errors entry. The
            # task_complete frame below changes status and is
            # consumed by a different handler path that DOESN'T
            # carry structured error context, which is exactly why
            # the user sees a bare "Task was cancelled." today.
            try:
                self._send({
                    "type": MessageType.PROGRESS,
                    "task_id": task_id,
                    "event_type": "error",
                    "content": (
                        f"Worker session for {readable_id} was "
                        f"cancelled ({cancellation_source})."
                    ),
                    "details": {
                        "error_class": "cancelled",
                        "cancellation_source": cancellation_source,
                        "retryable": False,
                    },
                })
            except Exception:
                # ``_send`` writes NDJSON to stdout; the only way this
                # raises is if stdout is closed, in which case the
                # process is already dying. Swallow so we still emit
                # the task_complete below if we can.
                logger.exception(
                    "Failed to emit cancellation telemetry for task %s",
                    task_id,
                )
            self._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "blocked",
                "comment": "Task was cancelled.",
                "token_cost": 0.0,
                "session_id": "",
            })
        except AgentErrorEscalation as esc:
            # Error-recovery retries exhausted OR non-retryable error.
            # Move the task to blocked with a structured comment so the
            # Manager Assistant (Board Operator) can pick it up, read
            # the classification, and decide next steps (split task,
            # refresh auth, etc.). Do NOT send ERROR — that would be
            # treated as a fatal agent crash by the supervisor.
            logger.warning(
                "Task %s escalated to MA: class=%s msg=%s",
                readable_id, esc.error_class, esc.escalation_message,
            )
            comment = (
                f"ESCALATED ({esc.error_class}): {esc.escalation_message}\n\n"
                f"Original error: {esc.original_error[:_ESCALATION_ORIGINAL_LENGTH]}\n\n"
                "Manager Assistant: please investigate. Options typically "
                "include splitting this task into smaller pieces, reducing "
                "scope, or (for config/auth errors) asking the user to "
                "resolve the underlying issue."
            )
            self._send({
                "type": MessageType.TASK_COMPLETE,
                "task_id": task_id,
                "status": "blocked",
                "comment": comment,
                "token_cost": esc.total_cost or 0.0,
                "session_id": esc.session_id or "",
                "details": {
                    "error_class": esc.error_class,
                    "escalation_message": esc.escalation_message,
                },
            })
        except Exception as exc:
            logger.exception("Task %s failed: %s", readable_id, exc)
            self._send({
                "type": MessageType.ERROR,
                "message": str(exc)[:1000],
                "task_id": task_id,
                "fatal": False,
            })
        finally:
            self._current_task_id = None

    # -----------------------------------------------------------------
    # Chat message handler (Manager role)
    # -----------------------------------------------------------------

    async def _handle_chat_message(self, msg: dict) -> None:
        """Handle a Manager chat query (streaming response).

        Receives a user's chat message, builds the Manager system prompt,
        runs an SDK query, and streams response chunks back to the
        Orchestrator. Sends response_final when the query completes.

        Args:
            msg: The chat_message message dict.
        """
        context_key = msg.get("context_key", "general_chat")
        content = msg.get("content", "")
        conversation_id = msg.get("conversation_id", "")
        session_id = msg.get("session_id")
        context_data = msg.get("context_data", {})

        logger.info("Chat message [%s]: %s", context_key, content[:80])

        try:
            from src.orchestrator.manager_controller import build_dynamic_context
            from src.config_sync.sync_service import ConfigStore

            # Build system prompt from context data
            # ConfigStore is populated from agent_config passed in the message
            config_store = ConfigStore()
            self._agent_config = msg.get("agent_config", {})
            config_store.update_from_agent_config(self._agent_config)
            system_prompt = build_dynamic_context(
                context_key, context_data, config_store
            )

            new_session_id, total_cost = await self._run_manager_session(
                user_message=content,
                system_prompt=system_prompt,
                session_id=session_id,
                context_key=context_key,
                conversation_id=conversation_id,
                agent_config=msg.get("agent_config", {}),
            )

            # Report final
            self._send({
                "type": MessageType.RESPONSE_FINAL,
                "conversation_id": conversation_id,
                "context_key": context_key,
                "token_cost": total_cost or 0.0,
                "session_id": new_session_id or "",
            })

        except asyncio.CancelledError:
            # Chat-v2 (CHAT-005): user-initiated cancel. Emit a clean
            # response_final so the ManagerController's chat handler
            # unblocks even if the CLI subprocess didn't get a chance
            # to flush a final NDJSON frame. The session_id is empty —
            # we don't trust the resume point of a half-cancelled
            # session; the next turn will start fresh.
            logger.info(
                "Chat cancelled mid-turn (conv=%s, ctx=%s)",
                (conversation_id or "")[:8], context_key,
            )
            self._send({
                "type": MessageType.RESPONSE_FINAL,
                "conversation_id": conversation_id,
                "context_key": context_key,
                "token_cost": 0.0,
                "session_id": "",
            })
            raise
        except Exception as exc:
            logger.exception("Chat message failed: %s", exc)
            self._send({
                "type": MessageType.ERROR,
                "message": str(exc)[:1000],
                "fatal": False,
            })

    # -----------------------------------------------------------------
    # SDK session runners
    # -----------------------------------------------------------------

    async def _run_sdk_session(
        self,
        agent_config: dict,
        task_data: dict,
    ) -> tuple[str | None, float | None]:
        """Run a Claude CLI session via docker exec and stream events.

        Invokes the Claude CLI directly inside the Docker container using
        ``docker exec``. Events are streamed to the Orchestrator via
        stdout NDJSON messages.

        Returns:
            Tuple of (session_id, total_cost).
        """
        from src.docker.session_bridge import stream_cli_session
        from src.orchestrator.worker_prompt import build_worker_prompt

        task_id = task_data.get("task_id", "")

        # Always fetch fresh task details from the backend to ensure
        # we have the latest state, brief, activities, and artifacts
        if self.backend_url and task_id:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"{self.backend_url}/api/offices/{self.office_id}/tool-call",
                        json={"action": "get_task_detail", "params": {"task_id": task_id}},
                    )
                    if resp.status_code == 200:
                        detail = resp.json()
                        task_data["brief"] = detail.get("brief", task_data.get("brief", {}))
                        task_data["title"] = detail.get("title", task_data.get("title", ""))
                        task_data["reviewer"] = detail.get("reviewer") or task_data.get("reviewer", "")
                        task_data["rework_count"] = detail.get("rework_count", task_data.get("rework_count", 0))
                        task_data["recent_activities"] = detail.get("recent_activities", [])
                        task_data["artifacts"] = detail.get("artifacts", [])

                        # Check if task state has changed since dispatch
                        current_status = detail.get("status", "")
                        current_agent = detail.get("assigned_agent") or ""

                        if current_status in ("done", "archived"):
                            logger.info("Task %s already %s — skipping", task_id, current_status)
                            return None, None

                        # Check authorization: agent must be either the
                        # assigned executor OR the designated reviewer.
                        current_reviewer = detail.get("reviewer") or ""
                        is_authorized = (
                            not current_agent
                            or current_agent == self.agent_name
                            or current_reviewer == self.agent_name
                        )
                        if not is_authorized:
                            logger.info(
                                "Task %s not assigned to us (%s) — agent=%s reviewer=%s — skipping",
                                task_id, self.agent_name, current_agent, current_reviewer,
                            )
                            return None, None

                        # Task is assigned to us (as executor or reviewer).
                        # The agent's prompt (review vs execute mode) tells
                        # it what to do based on the task state.

                        # Update status and agent from fresh data
                        task_data["status"] = current_status
                        task_data["assigned_agent"] = self.agent_name
                        logger.info("Fetched fresh task details for %s (status=%s)", task_id, current_status)
            except Exception as exc:
                logger.warning("Failed to fetch task details: %s", exc)
        container_name = agent_config.get("_container_name", "")
        # F5/R2-F9 (audit): explicit fallback via the central constant.
        # Fires only when the orchestrator dispatched a malformed config;
        # log loudly so the gap surfaces.
        from src.orchestrator._model_defaults import FALLBACK_WORKER_MODEL
        model = agent_config.get("model") or FALLBACK_WORKER_MODEL
        if not agent_config.get("model"):
            logger.warning(
                "Worker agent_config missing 'model' for task %s — "
                "falling back to %s. Investigate the orchestrator "
                "dispatch path.",
                task_id, FALLBACK_WORKER_MODEL,
            )
        # NOTE: We intentionally do NOT pass --allowed-tools to the Claude CLI.
        # The agent's allowed tools are documented in their CLAUDE.md, and the
        # MCP tool server defines which cubicle-tools are available. Passing
        # --allowed-tools would block MCP connector tools (Notion, Figma, etc.)
        # that are configured in the container via `claude mcp add`.
        allowed_tools: list[str] | None = None

        # Build the system prompt from the task brief only.
        # The agent's CLAUDE.md is auto-discovered by Claude CLI from the
        # per-agent working directory (/workspace/agents/{name}/CLAUDE.md).
        # The office-level CLAUDE.md (/workspace/CLAUDE.md) is also
        # auto-discovered via directory hierarchy.
        system_prompt = build_worker_prompt(task_data)
        prompt = f"Execute the task as described in the system prompt. Task ID: {task_id}"

        # Per-agent working directory for Claude CLI
        agent_cwd = f"/workspace/agents/{self.agent_name}"

        # Build MCP config for worker tools. ``triage`` is the new
        # mode for MA dispatch on a blocked task — the MCP server
        # uses it to refuse ``update_status`` / ``move_task`` on the
        # current blocked task, enforcing the playbook rule that the
        # MA never moves blocked → ready itself.
        status_now = task_data.get("status")
        if status_now == "review":
            task_mode = "review"
        elif status_now == "blocked":
            task_mode = "triage"
        else:
            task_mode = "execute"
        mcp_config = self._build_mcp_config(
            "worker",
            task_id,
            task_mode=task_mode,
            workstream_short_code=task_data.get("workstream_short_code") or None,
            scope_readable_id=task_data.get("scope_readable_id") or None,
        )

        total_cost: float | None = None
        session_id: str | None = None

        # MCP tool prefixes to skip in progress reporting (internal tools)
        _skip_prefixes = (
            "mcp__cubicle",
        )

        # Preserve session across rework cycles for context continuity.
        # On rework the backend passes the prior executor/reviewer session_id;
        # we resume it here so the agent sees its own prior work + feedback.
        prior_session_id = task_data.get("prior_session_id") or None
        if prior_session_id:
            logger.info(
                "Resuming prior session %s for task %s (rework cycle)",
                prior_session_id, task_id,
            )

        # Error-aware retry loop. If the CLI returns an error we classify
        # it and either retry with a remedy (raised token limits, adjusted
        # prompt, fresh session) or escalate by raising.
        #
        # Context preserved across retries:
        #   - session_id (most recent non-null from `result` messages)
        #   - total_cost (most recent from `result` messages)
        #   - _output_locked (once a terminal tool is seen, stays locked
        #     for the rest of the worker lifetime — not reset on retry
        #     because the task is already submitted and further output
        #     is spurious)
        _output_locked = False
        current_prompt = prompt
        current_system_prompt = system_prompt
        current_resume = prior_session_id
        current_env: dict[str, str] = {}
        attempt = 0
        max_attempts = _MAX_SESSION_ATTEMPTS
        # P2-E + P2.5-F: track wall-clock so we can fail-fast on
        # slow-burn retries even if the per-attempt CLI timeout
        # never fires. ``time.monotonic`` is the right clock here:
        # immune to wall-clock jumps and doesn't depend on the
        # event-loop instance (avoids the ``get_event_loop()``
        # deprecation surface).
        wallclock_start = time.monotonic()

        while attempt < max_attempts:
            elapsed = time.monotonic() - wallclock_start
            if elapsed > _MAX_SESSION_WALLCLOCK_SECONDS:
                logger.warning(
                    "task %s exceeded wall-clock budget (%.0fs > %ds); escalating",
                    task_id, elapsed, _MAX_SESSION_WALLCLOCK_SECONDS,
                )
                raise AgentErrorEscalation(
                    error_class="TIMEOUT",
                    original_error=(
                        f"Wall-clock budget exhausted after "
                        f"{int(elapsed)}s across {attempt} attempt(s)."
                    ),
                    escalation_message=(
                        "Task exceeded the 6-hour wall-clock budget across "
                        "retries. Investigate why the CLI is taking so long "
                        "(model rate limits, slow tool calls, infinite "
                        "loops in the prompt) before re-queuing."
                    ),
                    session_id=session_id,
                    total_cost=total_cost,
                )
            attempt += 1
            # All three signals live at loop-scope so the classification
            # block below can read them unconditionally (no NameError
            # games across branches).
            #
            # - last_error_text: populated ONLY when session_bridge emits
            #   an `error` stream event (non-zero exit or timeout). This
            #   is the retry trigger — if it stays None, the CLI
            #   succeeded and we return.
            # - last_api_error: captured opportunistically from assistant
            #   text prefixed "API Error:" OR from result.is_error. Used
            #   to ENRICH classification when an error does fire, since
            #   the raw "exited with code N" string matches no pattern.
            # - last_stderr_text: stderr captured by session_bridge and
            #   piggy-backed on the error event. Second-best enrichment.
            last_error_text: str | None = None
            last_api_error: str | None = None
            last_stderr_text: str = ""

            async for msg in stream_cli_session(
                container_name=container_name,
                model=model,
                system_prompt=current_system_prompt,
                prompt=current_prompt,
                cwd=agent_cwd,
                mcp_config=mcp_config,
                allowed_tools=allowed_tools,
                # Always exclude Claude CLI's built-in TaskCreate
                # family — see ``_CLAUDE_CLI_BUILTIN_DISALLOW``. The
                # ``allowed_tools`` whitelist passed above does NOT
                # cover these (Claude CLI built-ins land in the
                # model's tool catalog regardless), so explicit
                # ``--disallowed-tools`` is what keeps them out.
                disallowed_tools=_CLAUDE_CLI_BUILTIN_DISALLOW,
                resume_session=current_resume,
                env_overrides=current_env or None,
            ):
                # P2.5-F: per-message wall-clock check. The
                # between-attempts check at the top of the outer
                # while-loop only fires AFTER an attempt fully
                # finishes. Without this inline check, a slow-burn
                # attempt could individually run past the 6-hour
                # budget (the per-attempt CLI timeout is 4 h) before
                # we even look at the clock. The async generator
                # yields many messages, so this fires roughly once
                # per CLI line — cheap.
                elapsed = time.monotonic() - wallclock_start
                if elapsed > _MAX_SESSION_WALLCLOCK_SECONDS:
                    logger.warning(
                        "task %s exceeded wall-clock budget mid-attempt "
                        "(%.0fs > %ds); aborting attempt %d/%d",
                        task_id, elapsed, _MAX_SESSION_WALLCLOCK_SECONDS,
                        attempt, max_attempts,
                    )
                    raise AgentErrorEscalation(
                        error_class="TIMEOUT",
                        original_error=(
                            f"Wall-clock budget exhausted mid-attempt "
                            f"after {int(elapsed)}s "
                            f"(attempt {attempt}/{max_attempts})."
                        ),
                        escalation_message=(
                            "Task exceeded the 6-hour wall-clock budget. "
                            "Check why the CLI is running so long."
                        ),
                        session_id=session_id,
                        total_cost=total_cost,
                    )

                if msg.type == "result":
                    session_id = msg.data.get("session_id") or session_id
                    total_cost = (
                        msg.data.get("cost_usd")
                        or msg.data.get("total_cost_usd")
                        or total_cost
                    )
                    # Claude CLI reports terminal API errors via the final
                    # result message: is_error=true with the error text in
                    # `result`, or subtype=="error_during_execution". Both
                    # paths must feed the classifier so we don't fall back
                    # to the contentless exit-code string.
                    if (
                        msg.data.get("is_error")
                        or msg.data.get("subtype") == "error_during_execution"
                    ):
                        result_err = (
                            msg.data.get("result")
                            or msg.data.get("error")
                            or ""
                        )
                        if isinstance(result_err, str) and result_err.strip():
                            last_api_error = result_err.strip()
                elif msg.type == "assistant":
                    # Claude CLI stream-json: content blocks may contain
                    # text + tool_use mixed in one message.
                    blocks = msg.data.get("message", {}).get("content", [])

                    # PRE-SCAN: if ANY block is a terminal tool call,
                    # lock output BEFORE processing any block. This
                    # prevents same-turn leaks (e.g., text + update_status
                    # in one message — the text would leak without pre-scan).
                    if not _output_locked:
                        _terminal_tools = (
                            "update_status", "mcp__cubicle-tools__update_status",
                            "move_task", "mcp__cubicle-tools__move_task",
                        )
                        for block in blocks:
                            if block.get("type") == "tool_use":
                                if block.get("name", "") in _terminal_tools:
                                    _output_locked = True
                                    logger.info(
                                        "Output locked — terminal tool detected: %s",
                                        block.get("name"),
                                    )
                                    break

                    if _output_locked:
                        continue  # Skip entire message

                    for block in blocks:
                        if block.get("type") == "text" and block.get("text"):
                            text = block["text"]
                            # Claude CLI surfaces API errors as assistant
                            # text prefixed with "API Error:". Capture the
                            # full text so classify_error receives the
                            # specific diagnostic (e.g. output-token-limit)
                            # and can pick the right remedy instead of
                            # falling through to UNKNOWN_FATAL.
                            stripped = text.lstrip()
                            if stripped.startswith("API Error"):
                                last_api_error = stripped.strip()
                            self._send({
                                "type": MessageType.PROGRESS,
                                "task_id": task_id,
                                "event_type": "checkpoint",
                                "content": text[:500],
                            })
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "unknown")
                            if not any(
                                tool_name.startswith(p) for p in _skip_prefixes
                            ):
                                self._send({
                                    "type": MessageType.PROGRESS,
                                    "task_id": task_id,
                                    "event_type": "tool_run",
                                    "content": f"Using {tool_name}",
                                    "details": {"tool": tool_name},
                                })
                elif msg.type == "error":
                    # Capture and break out of the stream loop so the retry
                    # handler below can decide whether to retry or escalate.
                    last_error_text = msg.data.get("error") or ""
                    last_stderr_text = msg.data.get("stderr") or ""
                    logger.warning(
                        "CLI stream error on attempt %d/%d for task %s: "
                        "err=%s; api_err=%s; stderr=%s",
                        attempt, max_attempts, task_id,
                        last_error_text[:200],
                        (last_api_error or "")[:200],
                        last_stderr_text[:200],
                    )
                    break

            # No error stream event means the CLI finished cleanly — the
            # `assistant` may have mentioned an API error in passing (e.g.
            # quoting documentation), but the process exited 0 so the
            # session is a success. Ignore last_api_error in that case.
            if last_error_text is None:
                return session_id, total_cost

            # Pick the richest classification signal available, in order:
            # 1. API error text surfaced via assistant/result (specific
            #    diagnostic produced by the model/API).
            # 2. Stderr from the CLI subprocess (often contains the
            #    underlying auth/connection failure).
            # 3. The synthetic "Claude CLI exited with code N" string —
            #    last resort; matches no pattern but keeps the loop safe.
            if last_api_error:
                error_for_classify = last_api_error
            elif last_stderr_text.strip():
                error_for_classify = last_stderr_text.strip()
            else:
                error_for_classify = last_error_text

            # Classify and decide what to do.
            remedy = classify_error(error_for_classify)

            # Post a structured `error` activity so the task's feed shows
            # exactly what happened + what the system decided.
            self._send({
                "type": MessageType.PROGRESS,
                "task_id": task_id,
                "event_type": "error",
                "content": (
                    f"CLI error ({remedy.error_class.value}) on attempt "
                    f"{attempt}/{max_attempts}: {last_error_text[:_ERROR_PREVIEW_LENGTH]}"
                ),
                "details": {
                    "error_class": remedy.error_class.value,
                    "retryable": remedy.retryable,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                },
            })

            if not remedy.retryable or attempt >= max_attempts:
                # Non-retryable OR exhausted — escalate. Raise with a
                # structured message so the enclosing _handle_assign_task
                # can move the task to blocked.
                raise AgentErrorEscalation(
                    error_class=remedy.error_class.value,
                    original_error=last_error_text,
                    escalation_message=remedy.escalation_message,
                    session_id=session_id,
                    total_cost=total_cost,
                )

            # Apply remedy and loop. Log a recovery checkpoint so the
            # activity feed tells the user what's happening.
            self._send({
                "type": MessageType.PROGRESS,
                "task_id": task_id,
                "event_type": "checkpoint",
                "content": (
                    f"Recovering from {remedy.error_class.value} "
                    f"(attempt {attempt + 1}/{max_attempts}). Applying remedy: "
                    f"{'fresh session; ' if remedy.reset_session else ''}"
                    f"{'env=' + ','.join(remedy.env_overrides) + '; ' if remedy.env_overrides else ''}"
                    f"appending guidance hint to system prompt."
                ),
                "details": {
                    "error_class": remedy.error_class.value,
                    "env_overrides": list(remedy.env_overrides),
                    "reset_session": remedy.reset_session,
                    "backoff_seconds": remedy.backoff_seconds,
                },
            })

            if remedy.backoff_seconds > 0:
                await asyncio.sleep(remedy.backoff_seconds)

            # Fold the remedy's env into the next call. Later remedies
            # override earlier ones (e.g. repeated token-limit errors
            # keep the bumped var, not stack).
            current_env.update(remedy.env_overrides)

            # Append guidance to the system prompt. We keep previous
            # guidance (if any) so the agent sees the full history of
            # what went wrong — capped to a few entries to avoid bloat.
            #
            # P2.5-E: use a sentinel-style delimiter unlikely to
            # appear in the user's task brief (the previous Markdown
            # heading "## AUTOMATIC RECOVERY — READ THIS" could
            # collide with a brief that copy-pastes that exact
            # phrase, and rotation would corrupt the base prompt).
            # The HTML-comment form is invisible to most renderings
            # but still readable in the model's literal prompt.
            _MARKER = "\n\n<!--CBCL_RECOVERY_BLOCK_START-->\n"
            guidance_block = (
                f"{_MARKER}"
                f"## AUTOMATIC RECOVERY — READ THIS\n"
                f"Attempt {attempt} failed: {remedy.error_class.value}. "
                f"{remedy.guidance}"
            )

            if (
                len(current_system_prompt) + len(guidance_block)
                <= _MAX_SYSTEM_PROMPT_SIZE
            ):
                current_system_prompt = current_system_prompt + guidance_block
            else:
                # P2-F + P2.5-E: rotate oldest blocks out until the
                # new block fits. Previous behaviour rotated AT MOST
                # ONE block, so a prompt that hit the cap with N>=2
                # blocks would simply drop the new guidance — and on
                # the next attempt drop it again, leaving the agent
                # without the latest remedy on every single retry.
                # We now drop blocks oldest-first until the new
                # guidance fits or no blocks remain. If even an
                # empty-block prompt + guidance overflows, we drop
                # the new guidance as a last resort.
                offsets: list[int] = []
                idx = current_system_prompt.find(_MARKER)
                while idx >= 0:
                    offsets.append(idx)
                    idx = current_system_prompt.find(_MARKER, idx + 1)

                rotated = current_system_prompt
                rotated_count = 0
                for i in range(len(offsets)):
                    next_block_idx = (
                        offsets[i + 1] if i + 1 < len(offsets) else None
                    )
                    if next_block_idx is None:
                        rotated = rotated[: offsets[i]]
                    else:
                        # Drop everything from this block's start to
                        # the next block's start.
                        rotated = (
                            rotated[: offsets[i]]
                            + rotated[next_block_idx:]
                        )
                        # Recompute offsets after drop — easiest is
                        # to break and re-scan, but the loop math
                        # above shifts subsequent offsets. Simpler:
                        # break and rebuild offsets each pass.
                    rotated_count += 1
                    if (
                        len(rotated) + len(guidance_block)
                        <= _MAX_SYSTEM_PROMPT_SIZE
                    ):
                        break
                    # Re-scan offsets relative to rotated for next pass.
                    offsets = []
                    idx = rotated.find(_MARKER)
                    while idx >= 0:
                        offsets.append(idx)
                        idx = rotated.find(_MARKER, idx + 1)
                    if not offsets:
                        break

                if (
                    len(rotated) + len(guidance_block)
                    <= _MAX_SYSTEM_PROMPT_SIZE
                ):
                    logger.warning(
                        "Prompt size cap hit; rotated %d guidance "
                        "block(s) to keep attempt %d's remedy",
                        rotated_count, attempt,
                    )
                    current_system_prompt = rotated + guidance_block
                else:
                    logger.warning(
                        "Prompt size cap hit and rotation could not free "
                        "enough room (%d chars after dropping %d blocks); "
                        "dropping new guidance for attempt %d",
                        len(rotated), rotated_count, attempt,
                    )

            if remedy.reset_session:
                current_resume = None  # start a fresh session
            else:
                # Resume the session the CLI just used (if any) so the
                # next attempt sees the partial work — tool calls made,
                # files written, observations recorded — instead of
                # starting a blank conversation. Without this, the
                # retry starts a new session and the agent has no
                # visibility into what it already did on disk, forcing
                # it to redo discovery work and risking duplicate
                # writes / divergent output.
                if session_id:
                    current_resume = session_id

        # Unreachable by construction: the loop body returns on success
        # and raises AgentErrorEscalation on the final failure. This
        # raise only fires if someone refactors the loop incorrectly —
        # it fails loud rather than silently returning None, None.
        raise RuntimeError(
            "agent_worker retry loop exited without return or raise — "
            "this indicates a logic bug. Please file it."
        )

    async def _run_manager_session(
        self,
        user_message: str,
        system_prompt: str,
        session_id: str | None,
        context_key: str,
        conversation_id: str,
        agent_config: dict,
    ) -> tuple[str | None, float | None]:
        """Run a Manager CLI query via docker exec with streaming response.

        The Manager session is long-lived. Each call to this method runs
        one query (one user message -> one response). The session_id is
        used to resume the conversation from the previous query.

        Returns:
            Tuple of (new_session_id, total_cost).
        """
        from src.docker.session_bridge import stream_cli_session

        container_name = agent_config.get("_container_name", "")
        # F5/R2-F9 (audit): central fallback constant. Manager runs Opus
        # in normal operation; fallback only fires when agent_config is
        # malformed. Log so the gap surfaces.
        from src.orchestrator._model_defaults import FALLBACK_MANAGER_MODEL
        model = agent_config.get("model") or FALLBACK_MANAGER_MODEL
        if not agent_config.get("model"):
            logger.warning(
                "Manager agent_config missing 'model' — falling back to "
                "%s. Investigate the chat dispatch path.",
                FALLBACK_MANAGER_MODEL,
            )

        logger.info(
            "Manager session: container=%s, model=%s, prompt_len=%d, session=%s",
            container_name, model, len(system_prompt or ""), session_id,
        )

        if not container_name:
            raise RuntimeError("No _container_name in agent_config — cannot run docker exec")

        # Build MCP config for manager tools (task_mode="manager" bypasses executor guard).
        # context_key determines whether board-write tools are available —
        # General Chat is READ-ONLY; writes require switching to a workstream.
        mcp_config = self._build_mcp_config(
            "manager", task_mode="manager", context_key=context_key,
        )

        total_cost: float | None = None
        new_session_id: str | None = None

        agent_cwd = "/workspace/agents/manager"

        # Token-level streaming state:
        # - ``text_blocks_seen`` counts the text content blocks we've
        #   already streamed within this turn. On the 2nd+ block we
        #   prepend "\n\n" so accumulated markdown keeps its structure
        #   (lists, headings, code fences) instead of running into the
        #   previous paragraph.
        # - ``current_block_kind`` tracks whether the in-flight block
        #   is text or tool_use, so we ignore input_json_delta frames
        #   that belong to a tool_use argument stream.
        text_blocks_seen = 0
        current_block_kind: str | None = None

        msg_count = 0
        # Defense-in-depth at the CLI level. ``Bash`` and ``Task``
        # (subagent spawn) are Claude CLI **built-ins**, NOT MCP
        # tools — the role filter in mcp_tool_server.py only filters
        # ``mcp__cubicle-tools__*`` names, so it cannot exclude
        # these. ``--disallowed-tools`` is the only mechanism that
        # actually keeps the Manager from calling them, so this
        # block is the primary guard (not "belt-and-braces"). The
        # system prompt and ``manager-spec.md`` reinforce it but
        # neither is enforced by Claude CLI on its own. We also
        # exclude Claude CLI's built-in TaskCreate family — see
        # ``_CLAUDE_CLI_BUILTIN_DISALLOW`` for the rationale.
        MANAGER_DISALLOWED_TOOLS = ["Bash", "Task", *_CLAUDE_CLI_BUILTIN_DISALLOW]

        async for msg in stream_cli_session(
            container_name=container_name,
            model=model,
            system_prompt=system_prompt,
            prompt=user_message,
            cwd=agent_cwd,
            mcp_config=mcp_config,
            disallowed_tools=MANAGER_DISALLOWED_TOOLS,
            resume_session=session_id,
            include_partial_messages=True,
        ):
            msg_count += 1
            logger.info("Manager stream msg #%d: type=%s", msg_count, msg.type)
            if msg.type == "result":
                new_session_id = msg.data.get("session_id")
                total_cost = msg.data.get("cost_usd") or msg.data.get("total_cost_usd")
            elif msg.type == "stream_event":
                # --include-partial-messages emits Anthropic-style
                # incremental frames. We only need three of them:
                #   content_block_start  → note kind + paragraph break
                #   content_block_delta  → text_delta → one chunk
                #   content_block_stop   → clear kind
                event = msg.data.get("event", {})
                event_type = event.get("type", "")

                if event_type == "content_block_start":
                    block = event.get("content_block", {}) or {}
                    current_block_kind = block.get("type")
                    if current_block_kind == "text":
                        # Separate the current text block from the
                        # previous one so markdown lists / headings
                        # don't collapse into a single paragraph
                        # (Manager often emits "Here's what I found:"
                        # then a list after a tool call).
                        if text_blocks_seen > 0:
                            self._send({
                                "type": MessageType.RESPONSE_CHUNK,
                                "conversation_id": conversation_id,
                                "context_key": context_key,
                                "content": "\n\n",
                            })
                        text_blocks_seen += 1
                    elif current_block_kind == "tool_use":
                        # User-visible "Manager is using X" signal.
                        # tool_proxy handles the actual tool execution
                        # separately — this is purely a typing-indicator
                        # hint and keeps the 5-min watchdog alive.
                        tool_name = block.get("name") or "tool"
                        # Strip the MCP server prefix for readability
                        # (mcp__cubicle-tools__get_board → get_board).
                        bare = tool_name.split("__")[-1] if "__" in tool_name else tool_name
                        self._send({
                            "type": MessageType.ACTIVITY,
                            "conversation_id": conversation_id,
                            "context_key": context_key,
                            "activity": "tool_use",
                            "tool": bare,
                        })

                elif event_type == "content_block_delta":
                    delta = event.get("delta", {}) or {}
                    if (
                        current_block_kind == "text"
                        and delta.get("type") == "text_delta"
                    ):
                        text = delta.get("text", "")
                        if text:
                            self._send({
                                "type": MessageType.RESPONSE_CHUNK,
                                "conversation_id": conversation_id,
                                "context_key": context_key,
                                "content": text,
                            })

                elif event_type == "content_block_stop":
                    current_block_kind = None

            elif msg.type == "assistant":
                # With --include-partial-messages the full `assistant`
                # message arrives AFTER we've already streamed every
                # text_delta — re-emitting here would duplicate the
                # content in the UI. The full assistant frame is still
                # useful as a fallback when partial frames aren't
                # available (e.g. a future CLI version that drops the
                # flag): only emit if no text deltas have been seen
                # for this turn.
                if text_blocks_seen == 0:
                    for block in msg.data.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            self._send({
                                "type": MessageType.RESPONSE_CHUNK,
                                "conversation_id": conversation_id,
                                "context_key": context_key,
                                "content": block["text"],
                            })
            elif msg.type == "error":
                logger.error("Manager stream error: %s", msg.data)
                raise RuntimeError(msg.data.get("error", "Unknown error"))

        logger.info("Manager stream ended: %d messages, session=%s, cost=%s",
                     msg_count, new_session_id, total_cost)
        return new_session_id, total_cost

    # -----------------------------------------------------------------
    # MCP config builder
    # -----------------------------------------------------------------

    def _build_mcp_config(
        self,
        role: str,
        task_id: str | None = None,
        task_mode: str | None = None,
        context_key: str | None = None,
        workstream_short_code: str | None = None,
        scope_readable_id: str | None = None,
    ) -> dict:
        """Build the MCP server configuration for the Claude CLI.

        Returns a dict suitable for the ``--mcp-config`` flag.  Contains
        only our custom cubicle-tools MCP server.

        Other MCP servers (Notion, Slack, etc.) are managed natively by
        Claude via ``claude mcp add`` and stored in ``~/.claude.json``
        inside the container.  They are available automatically to all
        sessions — no need to pass them via ``--mcp-config``.

        Per-agent tool filtering is handled by the ``--allowed-tools``
        flag, which restricts which MCP tools each agent can call.

        For Manager sessions, ``context_key`` controls whether board-
        mutating tools are available. In General Chat mode, only read/
        discovery tools are offered; write tools are suppressed so the
        Manager cannot create/modify tasks or scopes without first
        switching to a workstream.
        """
        env: dict[str, str] = {
            "BACKEND_URL": self.backend_url or "http://host.docker.internal:8000",
            "OFFICE_ID": self.office_id,
            "TASK_MODE": task_mode or "execute",
        }
        if context_key:
            env["CONTEXT_KEY"] = context_key
        # Route tool calls through local proxy when WS transport is active.
        # The bearer token must travel with the URL — the in-container
        # MCP needs it to authenticate against /tool-call AND
        # /script-execute-host (the latter spawns docker exec with
        # caller-controlled env, so confining it to authenticated
        # callers prevents office-secret exfil from other local procs).
        tool_proxy_url = os.environ.get("CUBICLE_TOOL_PROXY_URL", "")
        if tool_proxy_url:
            env["TOOL_PROXY_URL"] = tool_proxy_url
            tool_proxy_token = os.environ.get(
                "CUBICLE_TOOL_PROXY_TOKEN", "",
            )
            if tool_proxy_token:
                env["TOOL_PROXY_TOKEN"] = tool_proxy_token
        if task_id:
            env["TASK_ID"] = task_id
        # Per-task output dir context. Only the SHORT_CODE is needed
        # for the path; SCOPE_READABLE_ID is optional and present
        # only when the task lives in a scope. The in-container
        # MCP server reads these to inject CUBICLE_OUTPUT_DIR into
        # script subprocesses (mirrors the host-side ScriptRunner).
        if workstream_short_code:
            env["CUBICLE_WORKSTREAM_SHORT_CODE"] = workstream_short_code
        if scope_readable_id:
            env["CUBICLE_SCOPE_READABLE_ID"] = scope_readable_id
        if self.agent_name:
            env["AGENT_NAME"] = self.agent_name

        return {
            "mcpServers": {
                "cubicle-tools": {
                    "type": "stdio",
                    "command": "python3",
                    "args": ["/opt/cubicle/mcp_tool_server.py", "--role", role],
                    "env": env,
                },
            },
        }

    # T1.11 (review): the proxied tool-call path was deleted. Tool
    # dispatch happens entirely in the in-container MCP tool server
    # (`/opt/cubicle/mcp_tool_server.py`) which hits the backend over
    # HTTP. Workers never use IPC `tool_call`/`tool_response` frames.
    # See `manager_controller._handle_event` for the matching warn-
    # and-ignore branch on the Manager side.

    def _handle_tool_response(self, msg: dict) -> None:
        """Legacy stub: log and discard.

        The orchestrator no longer sends `tool_response` frames since
        the proxied tool-call path was retired. If one arrives, log so
        we can investigate.
        """
        logger.warning(
            "Unexpected tool_response IPC frame: %s — the proxied "
            "tool-call path was retired in the v2.4.0 review.",
            msg.get("request_id", "")[:8],
        )

    # -----------------------------------------------------------------
    # Cancel and shutdown handlers
    # -----------------------------------------------------------------

    def _handle_cancel(self) -> None:
        """Cancel the current session task (Chat-v2 / CHAT-005).

        If a chat / assign session is in flight, cancel its asyncio
        Task. ``stream_cli_session`` handles ``CancelledError`` by
        terminating the ``docker exec`` CLI subprocess, so this
        propagates all the way down to the actual Claude process.

        No-op when the worker is idle (the user clicked Cancel after
        the Manager already finished — the orchestrator may still
        forward the message because the cancellation came in over the
        network and arrived just after the final).
        """
        task = self._current_session_task
        if task is None or task.done():
            logger.info("Cancel received but no active session — no-op")
            return
        # Tag the cancellation BEFORE calling task.cancel() — the
        # CancelledError handler in _handle_assign_task reads this
        # to attribute the row in the task_errors telemetry table.
        # Without the tag, an explicit user-driven cancel would be
        # indistinguishable from a heartbeat reap.
        self._cancellation_source = "explicit_cancel"
        logger.info("Cancelling current session task (%s)", task.get_name())
        task.cancel()

    def _handle_shutdown(self, msg: dict) -> None:
        """Initiate graceful shutdown.

        Sets the shutdown event, which causes the main run() loop to exit
        after the current message is processed. The Orchestrator expects
        the process to exit within the grace period.

        Args:
            msg: The shutdown message dict.
        """
        grace = msg.get("grace_period_seconds", 30)
        logger.info("Shutdown requested (grace=%ds)", grace)
        # If an in-flight session gets cancelled by the shutdown grace
        # period, attribute the row to "shutdown" rather than the
        # external_cancel default — the supervisor / daemon initiated
        # this, we know it. The CancelledError handler reads the field.
        self._cancellation_source = "shutdown"
        self._shutdown_event.set()

    def _handle_signal(self) -> None:
        """Handle SIGTERM/SIGINT.

        Sets the shutdown event for graceful exit. The process should
        exit within a few seconds after the current operation completes.
        """
        logger.info("Signal received, shutting down")
        # Same provenance as _handle_shutdown — a signal-driven cancel
        # of an in-flight session is attributable to "shutdown" rather
        # than the external_cancel default.
        self._cancellation_source = "shutdown"
        self._shutdown_event.set()

    # -----------------------------------------------------------------
    # IPC output
    # -----------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        """Write an NDJSON message to stdout.

        This is the ONLY method that writes to stdout. All IPC output
        goes through here. The method catches BrokenPipeError (which
        occurs if the Orchestrator died) and silently ignores it --
        there is nothing the agent can do if its parent is gone.

        Args:
            msg: A dict to serialize and write as NDJSON.
        """
        try:
            line = serialize(msg)
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            # Orchestrator died -- nothing we can do
            pass


def main() -> None:
    """Entry point when spawned as a subprocess.

    Parses command-line arguments, redirects stderr to a log file,
    configures logging, creates the AgentWorker, and runs it.

    Command-line arguments:
        --role: "manager" or "worker" (required).
        --agent-name: The agent's name (required).
        --workspace-path: Path to the workspace directory (default: /workspace).
        --office-id: The office ID (default: "").
        --backend-url: The platform backend URL (default: "").
    """
    parser = argparse.ArgumentParser(description="Cubicle Agent Worker")
    parser.add_argument(
        "--role", choices=["manager", "worker"], required=True
    )
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--workspace-path", default="/workspace")
    parser.add_argument("--office-id", default="")
    parser.add_argument("--backend-url", default="")
    args = parser.parse_args()

    # Redirect stderr to a log file so it does not pollute stdout IPC.
    # This is CRITICAL: any output to stdout that is not valid NDJSON
    # will cause the Orchestrator's _read_stdout() to skip it, but
    # excessive noise degrades debugging. Logging to a file is cleaner.
    log_dir = os.path.join(args.workspace_path, ".cubicle", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"agent-{args.agent_name}.log")
    sys.stderr = open(log_file, "a")  # noqa: SIM115

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    worker = AgentWorker(
        role=args.role,
        agent_name=args.agent_name,
        workspace_path=args.workspace_path,
        office_id=args.office_id,
        backend_url=args.backend_url,
    )

    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
