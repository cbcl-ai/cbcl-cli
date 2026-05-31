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
from typing import Awaitable, Callable

from src.agent_protocol import MessageType, serialize

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
# MCP config builder + CLI-builtin disallow list moved to
# ``_agent_worker_mcp`` (Wave 10 decomposition). Re-imported here so
# existing call sites — and the ``AgentWorker._build_mcp_config``
# adapter below — keep working unchanged.
from ._agent_worker_mcp import (  # noqa: E402, F401
    _CLAUDE_CLI_BUILTIN_DISALLOW,
    build_mcp_config as _build_mcp_config_impl,
)


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

            # W5-P2-H1: ``errors="replace"`` mirrors the supervisor's
            # reader loop — a single malformed UTF-8 byte from a buggy
            # Orchestrator must NOT crash this dispatcher and leave the
            # agent without a way to receive cancel / shutdown signals.
            # The JSON parse below catches the resulting U+FFFD-laced
            # line and warns + continues.
            decoded = line.decode(errors="replace").strip()
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
        """Handle a task assignment.

        Adapter for the extracted
        ``_agent_worker_task.handle_assign_task`` (Wave 10
        decomposition).
        """
        from ._agent_worker_task import handle_assign_task
        await handle_assign_task(self, msg)

    # -----------------------------------------------------------------
    # Chat message handler (Manager role)
    # -----------------------------------------------------------------

    async def _handle_chat_message(self, msg: dict) -> None:
        """Handle a Manager chat query (streaming response).

        Adapter for the extracted ``_agent_worker_manager.handle_chat_message``
        (Wave 10 decomposition).
        """
        from ._agent_worker_manager import handle_chat_message
        await handle_chat_message(self, msg)

    # -----------------------------------------------------------------
    # SDK session runners
    # -----------------------------------------------------------------

    async def _run_sdk_session(self, *args, **kwargs):
        """Run a Claude CLI worker session.

        Adapter for the extracted
        ``_agent_worker_task.run_sdk_session`` (Wave 10
        decomposition).
        """
        from ._agent_worker_task import run_sdk_session
        return await run_sdk_session(self, *args, **kwargs)

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

        Adapter for the extracted
        ``_agent_worker_manager.run_manager_session``
        (Wave 10 decomposition).
        """
        from ._agent_worker_manager import run_manager_session
        return await run_manager_session(
            self,
            user_message=user_message,
            system_prompt=system_prompt,
            session_id=session_id,
            context_key=context_key,
            conversation_id=conversation_id,
            agent_config=agent_config,
        )

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

        Adapter for the extracted ``_agent_worker_mcp.build_mcp_config``
        (Wave 10 decomposition). The actual builder + the
        ``_CLAUDE_CLI_BUILTIN_DISALLOW`` constant live in the sibling
        module; this adapter keeps the class-method call shape stable
        for every existing caller in this file.
        """
        return _build_mcp_config_impl(
            self,
            role,
            task_id=task_id,
            task_mode=task_mode,
            context_key=context_key,
            workstream_short_code=workstream_short_code,
            scope_readable_id=scope_readable_id,
        )

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
