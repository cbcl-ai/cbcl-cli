"""Manager controller -- routes chat messages through the Manager subprocess.

The Manager runs as a long-lived subprocess managed by the AgentSupervisor.
This controller sends chat messages to that subprocess via NDJSON over stdin
and routes response events (chunks, actions, errors) back to the platform
via the WsTransport.

The Manager's static rules live in /workspace/CLAUDE.md (written by
ClaudeMdWriter on sync). The system_prompt sent per session contains
ONLY dynamic context: current context header, team roster, board
summary, KB status, and recent conversation history.
"""

from __future__ import annotations

import asyncio
import time
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.config_sync.sync_service import ConfigStore
    from src.connection.ws_client import PlatformWSClient
    from src.orchestrator.agent_supervisor import AgentSupervisor
    from src.orchestrator.session_manager import SessionManager

logger = logging.getLogger(__name__)

# How long to wait for a response after sending a chat message before
# considering the Manager hung. Configurable via env var.
#
# Semantics: INACTIVITY timer, NOT a total-turn cap.
#
# The deadline resets every time the Manager emits something — a text
# chunk, a tool_call, a tool_use activity pulse, a progress event.
# Only a truly silent Manager (Claude CLI deadlocked; no stdout for N
# straight seconds) will trigger this timeout. A long but healthy turn
# — e.g. creating an 18-task scope, which can take 15+ minutes of
# back-and-forth tool calls — is fine because every tool call resets
# the counter.
#
# Before this was an inactivity timer we had a fixed 10-minute total
# cap that killed complex scope-creation turns mid-stream and printed
# a misleading "session timed out" message into the chat.
#
# 300s (5 min) aligns with the frontend's own RESPONSE_TIMEOUT_MS so
# server and client watchdogs fire at the same point.
MANAGER_INACTIVITY_TIMEOUT = int(
    os.environ.get("CUBICLE_MANAGER_INACTIVITY_TIMEOUT", "300")
)

# Absolute ceiling on turn duration — last-resort watchdog against
# a pathological Manager that keeps emitting tool calls in an infinite
# loop. Very generous (1 hour) because legitimate scope-first planning
# for large bodies of work can approach 30 minutes. A hit here means
# something is wrong with the Manager's prompt/plan, not with the
# user's request.
MANAGER_HARD_TIMEOUT = int(
    os.environ.get("CUBICLE_MANAGER_HARD_TIMEOUT", "3600")
)

# After a Manager crash, wait this long before attempting restart.
MANAGER_RESTART_DELAY = 2.0  # seconds

# Maximum consecutive crashes before giving up on auto-restart.
MANAGER_MAX_CONSECUTIVE_CRASHES = 5

# The agent_name used by the supervisor for the Manager subprocess.
# The supervisor hardcodes "manager" as the agent_name in spawn_manager().
MANAGER_AGENT_NAME = "manager"


class ManagerController:
    """Controls the AI Manager via a supervised subprocess.

    Instead of running the SDK inline, this controller sends chat messages
    to the Manager subprocess and routes response events back to the
    platform backend via the message router.
    """

    def __init__(
        self,
        supervisor: AgentSupervisor | None,
        router: object | None,
        session_manager: SessionManager,
        config_store: ConfigStore,
        office_id: str = "",
        workspace_path: str = "",
        *,
        ws_client: PlatformWSClient | None = None,
        container_name: str = "",
        backend_url: str = "",
        secrets_store: Any = None,
    ) -> None:
        self._supervisor = supervisor
        self._router = router
        self._sessions = session_manager
        self._config = config_store
        self._office_id = office_id
        self._workspace_path = workspace_path

        # Single-process mode fallbacks (used when supervisor/router are None)
        self._ws = ws_client
        self._container_name = container_name
        self._backend_url = backend_url
        self._secrets_store = secrets_store

        # Tracks the conversation_id of the currently active chat exchange.
        # Used to correlate response chunks from the subprocess back to the
        # correct chat conversation on the platform side.
        self._active_conversation_id: str | None = None

        # Tracks the context_key for the active exchange (for routing).
        self._active_context_key: str = "general_chat"

        # Response completion event: set when the Manager subprocess sends
        # response_final, allowing handle_chat_message() to return.
        self._response_done: asyncio.Event = asyncio.Event()

        # Monotonic timestamp of the last activity pulse in the current
        # turn. Every Manager event (response chunk, tool call, activity,
        # progress, final) refreshes this. The inactivity watchdog in
        # _handle_chat_message_locked compares ``time.monotonic()`` to
        # this value to decide whether the Manager has gone silent.
        self._last_activity_ts: float = 0.0

        # Serialises ``handle_chat_message`` so concurrent callers
        # (e.g. a user chat message arriving while a script's
        # outbox watcher is mid-ingest) can't clobber each other's
        # ``_active_conversation_id`` / ``_response_done`` state.
        # Phase 4's outbox watcher runs on the same event loop as
        # the chat gateway, which makes interleaving trivially
        # reachable without this lock.
        self._chat_lock: asyncio.Lock = asyncio.Lock()

        # Distinguishes user-originated chat turns from
        # script-originated ones. When a user's turn is in flight
        # (``_user_streaming`` set), incoming script drops wait on
        # ``_user_turn_done`` before acquiring ``_chat_lock``. This
        # prevents a script drop from hijacking the lock between
        # the user's request and the Manager's streamed reply —
        # that would cause the user's next chunk to resume against
        # whatever session the script drop left active.
        self._user_streaming: bool = False
        self._user_turn_done: asyncio.Event = asyncio.Event()
        self._user_turn_done.set()  # Default: no user turn in flight.

        # Error captured from the subprocess during the active exchange.
        self._response_error: str | None = None

        # Set by ``cancel_current_turn`` so the user-message handler's
        # finally block knows NOT to overwrite the published
        # ``manager_state("cancelled", ...)`` with idle. Reset at the
        # start of every new chat turn.
        self._turn_cancelled: bool = False

        # Consecutive crash counter for auto-restart circuit breaker.
        self._consecutive_crashes: int = 0

    # -- Wiring (setters) -----------------------------------------------------

    def set_supervisor(self, supervisor: AgentSupervisor | None) -> None:
        """Attach (or clear) the AgentSupervisor.

        P2-H: handlers.py historically wrote ``mgr._supervisor = ...``
        directly. Going through a setter keeps the field private,
        documents the wire-up step, and gives us a place to add
        validation later (e.g. "controller already running" checks).
        Accepts None for shutdown teardown.
        """
        self._supervisor = supervisor

    def set_router(self, router: Any) -> None:
        """Attach (or clear) the MessageRouter. See ``set_supervisor``."""
        self._router = router

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start the Manager subprocess.

        Called during Orchestrator startup. Spawns the Manager process
        via the supervisor. The supervisor must be attached first
        (`set_supervisor`).
        """
        if self._supervisor is None:
            logger.warning(
                "Manager start() called before supervisor was attached "
                "— skipping spawn. Wire up set_supervisor() first."
            )
            return

        await self._spawn_manager()

    async def _spawn_manager(self) -> None:
        """Spawn the Manager process and wait for READY."""
        logger.info("Spawning Manager subprocess...")

        model = "claude-sonnet-4-6"
        if self._config.office_config:
            model = self._config.office_config.get("manager_model", model)

        agent_config = {
            "model": model,
        }

        try:
            result = await self._supervisor.spawn_manager(agent_config)
            if not result:
                raise RuntimeError("spawn_manager returned False")
        except Exception as exc:
            logger.error("Failed to spawn Manager process: %s", exc)
            raise

        # spawn_manager already waits for READY internally.
        self._consecutive_crashes = 0
        statuses = self._supervisor.get_all_statuses()
        pid = statuses.get(MANAGER_AGENT_NAME, {}).get("pid", "?")
        logger.info("Manager subprocess is READY (PID %s)", pid)

    async def stop(self) -> None:
        """Gracefully stop the Manager subprocess.

        Uses the supervisor's internal send + kill for the Manager process
        only, not the global shutdown() which would kill all agents.
        """
        if self._supervisor is None:
            return
        try:
            # Send shutdown command to the Manager process specifically.
            await self._supervisor._send_to_agent(MANAGER_AGENT_NAME, {
                "type": "shutdown",
                "grace_period_seconds": 10,
            })
            logger.info("Manager subprocess stop requested")
        except Exception as exc:
            logger.warning("Error stopping Manager: %s", exc)

    async def _restart_manager(self, reason: str) -> None:
        """Restart the Manager subprocess after a crash.

        Implements a circuit breaker: after MANAGER_MAX_CONSECUTIVE_CRASHES
        consecutive crashes, stops attempting restarts and publishes an error.
        """
        self._consecutive_crashes += 1
        if self._consecutive_crashes > MANAGER_MAX_CONSECUTIVE_CRASHES:
            logger.error(
                "Manager has crashed %d consecutive times -- giving up on "
                "auto-restart. Manual intervention required.",
                self._consecutive_crashes,
            )
            return

        logger.warning(
            "Manager crashed (reason: %s, consecutive: %d/%d) -- "
            "restarting in %.1fs",
            reason, self._consecutive_crashes,
            MANAGER_MAX_CONSECUTIVE_CRASHES, MANAGER_RESTART_DELAY,
        )
        await asyncio.sleep(MANAGER_RESTART_DELAY)

        try:
            await self._spawn_manager()
        except Exception as exc:
            logger.error("Failed to restart Manager: %s", exc)

    # -- Chat message handling ------------------------------------------------

    async def handle_chat_message(
        self, message: dict, *, source: str = "user",
    ) -> None:
        """Handle a chat_message from the platform.

        Sends the message to the Manager subprocess and waits for the
        response to complete (indicated by a response_final event).
        Serialised via ``_chat_lock`` so concurrent callers — e.g.
        a user chat + a script notification via
        :meth:`ingest_script_message` — don't clobber each other's
        in-flight response tracking.

        ``source`` is ``"user"`` for user-originated chats or
        ``"script"`` for outbox-watcher drops. While a user turn is
        in flight, script turns wait on ``_user_turn_done`` BEFORE
        acquiring ``_chat_lock`` — this prevents a burst of script
        drops from starving user chat. User turns never wait.
        """
        if source == "script":
            # Park behind any in-flight user turn. The lock below
            # also serialises, but waiting here surfaces a clean
            # "delayed behind user input" log line and avoids the
            # user seeing their "Send" button spin while a script
            # holds the lock.
            while self._user_streaming:
                logger.info(
                    "ingest_script_message: user turn in flight, "
                    "waiting for it to complete before dispatching",
                )
                await self._user_turn_done.wait()

        # User turns: flip the streaming flag before lock acquire
        # so any script drop arriving mid-lock-wait sees the flag
        # and defers instead of racing us for the lock.
        is_user = source == "user"
        if is_user:
            self._user_streaming = True
            self._user_turn_done.clear()

        try:
            async with self._chat_lock:
                await self._handle_chat_message_locked(message)
        finally:
            if is_user:
                self._user_streaming = False
                self._user_turn_done.set()

    async def _handle_chat_message_locked(self, message: dict) -> None:
        """Body of ``handle_chat_message``, runs with the chat lock
        held. Split out so the lock-acquisition point is obvious in
        stack traces when the Manager is blocked."""
        context_key = message.get("context_key", "general_chat")
        user_message = message.get("user_message", "")
        context_data = message.get("context_data", {})
        conversation_id = message.get("conversation_id", "")

        logger.info(
            "Chat message [%s] conv=%s: %s",
            context_key,
            conversation_id[:8] if conversation_id else "?",
            user_message[:80],
        )

        # Refresh API token in workspace before the session
        try:
            from src.handlers import _write_api_token
            _write_api_token(self._workspace_path)
        except Exception:
            pass

        # Build system prompt (dynamic context only)
        system_prompt = build_dynamic_context(
            context_key, context_data, self._config,
        )

        # Get/create session_id for this context
        session_id = self._sessions.switch_context(context_key)

        # R2-F1/R2-F9 (audit): central fallback constant. Manager normally
        # runs Opus per the curated catalog; this fallback only fires when
        # office_config is missing manager_model — which would be an
        # orchestrator-side bug. Log so the gap surfaces.
        from src.orchestrator._model_defaults import FALLBACK_MANAGER_MODEL
        model = FALLBACK_MANAGER_MODEL
        if self._config.office_config:
            model = (
                self._config.office_config.get("manager_model")
                or FALLBACK_MANAGER_MODEL
            )
        if model == FALLBACK_MANAGER_MODEL and (
            not self._config.office_config
            or not self._config.office_config.get("manager_model")
        ):
            logger.warning(
                "office_config missing 'manager_model' — Manager "
                "falling back to %s. Investigate the sync_config path.",
                FALLBACK_MANAGER_MODEL,
            )

        # Prepare the IPC message
        chat_msg = {
            "context_key": context_key,
            "content": user_message,
            "context_data": context_data,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "system_prompt": system_prompt,
            "model": model,
        }

        # Set up response tracking
        self._active_conversation_id = conversation_id
        self._active_context_key = context_key
        self._response_done.clear()
        self._response_error = None
        # Fresh turn — clear any leftover cancel flag from a prior
        # turn so the finally block at the bottom correctly publishes
        # idle if this turn ends normally.
        self._turn_cancelled = False
        # Initialise the inactivity clock BEFORE dispatch so any tiny
        # delay between send_chat_to_manager and the first event doesn't
        # falsely consume the first timeout window.
        self._last_activity_ts = time.monotonic()
        turn_started_at = self._last_activity_ts

        try:
            if self._supervisor is not None:
                # Process-per-agent mode: send to Manager subprocess.
                # If the supervisor reports the Manager isn't running
                # (heartbeat-timeout SIGKILL, OOM, supervisor restart
                # mid-session), try to respawn ONCE before failing the
                # user's message. The supervisor's auto-restart loop
                # (`_restart_manager`) usually has the Manager back up
                # within MANAGER_RESTART_DELAY seconds, but a freshly-
                # arrived message can race that window. Telling the
                # user "Manager restarting…" beats "An error occurred".
                try:
                    await self._supervisor.send_chat_to_manager(chat_msg)
                except RuntimeError as exc:
                    if "not running" not in str(exc).lower():
                        raise
                    logger.warning(
                        "Manager not running for [%s] — spawning before "
                        "retrying chat dispatch", context_key,
                    )
                    await self._publish_manager_state(
                        context_key,
                        "restarting",
                        "Manager is restarting — your message will be "
                        "delivered as soon as it's ready.",
                    )
                    await self._spawn_manager()
                    await self._supervisor.send_chat_to_manager(chat_msg)
                    await self._publish_manager_state(
                        context_key, "ready",
                        "Manager is back. Working on your message…",
                    )

                # Inactivity watchdog: the Manager may work on a single
                # turn for 15+ minutes (e.g. creating an 18-task scope
                # with full briefs). We DON'T cap total turn duration.
                # Instead we poll in short intervals and only bail if
                # MANAGER_INACTIVITY_TIMEOUT seconds pass with zero
                # Manager output. Every event handler below refreshes
                # ``_last_activity_ts`` so healthy work resets the
                # counter.
                #
                # A MANAGER_HARD_TIMEOUT ceiling (default 1 hour) is the
                # last-resort safety net against a pathological loop.
                # Chat-v2: heartbeat the UI every 20 s so the status
                # pill stays accurate. The previous code only emitted
                # `manager_state(working)` once per minute, which made
                # the pill look frozen between updates. With a 20 s
                # cadence the user sees the elapsed counter advance
                # continuously without spamming the WS.
                _heartbeat_interval = 20
                _last_heartbeat = 0
                while not self._response_done.is_set():
                    try:
                        await asyncio.wait_for(
                            self._response_done.wait(),
                            timeout=min(_heartbeat_interval, MANAGER_INACTIVITY_TIMEOUT),
                        )
                    except asyncio.TimeoutError:
                        now = time.monotonic()
                        silence = now - self._last_activity_ts
                        elapsed = now - turn_started_at
                        if silence >= MANAGER_INACTIVITY_TIMEOUT:
                            logger.error(
                                "Manager silent for %ds (>= inactivity "
                                "timeout %ds) for [%s] — giving up",
                                int(silence), MANAGER_INACTIVITY_TIMEOUT,
                                context_key,
                            )
                            raise
                        if elapsed >= MANAGER_HARD_TIMEOUT:
                            logger.error(
                                "Manager turn exceeded hard cap %ds for "
                                "[%s] — killing",
                                MANAGER_HARD_TIMEOUT, context_key,
                            )
                            raise
                        # Still active: log once per minute so operators
                        # can see long turns in the cbcl log.
                        if int(elapsed) - _last_heartbeat >= _heartbeat_interval:
                            _last_heartbeat = int(elapsed)
                            logger.info(
                                "Manager still working [%s]: elapsed=%ds "
                                "silence=%ds",
                                context_key, int(elapsed), int(silence),
                            )
                            # Push the same heartbeat to the chat UI so
                            # the user sees real progress (rather than a
                            # spinner that's been spinning silently for
                            # 5 minutes). Halfway to the inactivity
                            # threshold we switch the message to a soft
                            # warning so the user can decide whether to
                            # let it keep working.
                            half = MANAGER_INACTIVITY_TIMEOUT / 2
                            if silence >= half:
                                await self._publish_manager_state(
                                    context_key, "stuck",
                                    f"Manager has been silent for "
                                    f"{int(silence)}s. Will give up at "
                                    f"{MANAGER_INACTIVITY_TIMEOUT}s of "
                                    f"silence if no progress arrives.",
                                )
                            else:
                                await self._publish_manager_state(
                                    context_key, "working",
                                    f"Manager working — {int(elapsed)}s "
                                    f"elapsed.",
                                )

                # Check for errors captured during the exchange
                if self._response_error:
                    await self._publish_error_response(
                        conversation_id, context_key, self._response_error,
                    )
            else:
                # The supervisor should have been attached during
                # daemon startup. If a chat message arrives without
                # one, the wiring is broken — fail loudly rather than
                # silently dropping the message.
                raise RuntimeError(
                    "AgentSupervisor not attached; cannot dispatch chat"
                )

        except asyncio.TimeoutError:
            logger.error(
                "Manager exchange timed out for [%s]",
                context_key,
            )
            await self._publish_error_response(
                conversation_id, context_key,
                "The Manager hasn't responded for several minutes and "
                "appears stuck. The session has been cancelled. Please "
                "try again — if this keeps happening, break the request "
                "into smaller pieces or restart the Communicator.",
            )
        except Exception as exc:
            logger.exception(
                "Error sending chat to Manager [%s]: %s", context_key, exc,
            )
            await self._sessions.clear_session(context_key)
            await self._publish_error_response(
                conversation_id, context_key,
                f"An error occurred: {str(exc)[:500]}\n\n"
                "The session has been reset. Please resend your message.",
            )
        finally:
            self._active_conversation_id = None
            # Defense-in-depth: clear the "Manager working — Xs"
            # status pill on EVERY exit path (success, error, timeout,
            # crash). EXCEPT cancel — ``cancel_current_turn`` publishes
            # its own ``manager_state("cancelled", ...)`` and the user
            # should see THAT message persist, not be overwritten with
            # idle one tick later. The ``_turn_cancelled`` flag is set
            # by the cancel path and consumed here.
            if not self._turn_cancelled:
                try:
                    await self._publish_manager_state(context_key, "idle", "")
                except Exception:
                    logger.debug(
                        "idle state publish in finally failed (non-fatal)",
                        exc_info=True,
                    )

    async def ingest_script_message(
        self,
        *,
        context_key: str,
        script_name: str,
        content: str,
        execution_id: str,
        attachments: list[str] | None = None,
    ) -> None:
        """Route a notification from a running script into the Manager.

        Prefixes the content with ``[Script: <name>]`` so the user and
        the Manager can distinguish it from direct user input, then
        feeds it through the same :meth:`handle_chat_message` path a
        user message would take. Attachments are appended as a
        workspace-path list the Manager can ``Read`` directly.

        Fire-and-forget from the script's POV — the script doesn't
        wait for the Manager's reaction. The Watcher caller DOES
        await here because ``handle_chat_message`` serialises on
        ``_chat_lock`` to keep response-tracking state coherent
        when user chat + script drop overlap.
        """
        prefixed = f"[Script: {script_name}] {content}"
        if attachments:
            block = "\n".join(f"- `{a}`" for a in attachments)
            prefixed = f"{prefixed}\n\n**Attachments:**\n{block}"

        # Synthesise a conversation_id that's recognisably from a
        # script — useful in logs. Deterministic from the
        # execution id so a replay of the same payload gets the
        # same id (handy for dedup on the backend side later).
        # Defensive fallback when both identifiers are blank
        # shouldn't happen in practice (the watcher always passes
        # ``script_name``), but defend against future callers.
        conv_id = (
            f"script-{execution_id}" if execution_id
            else f"script-{script_name}" if script_name
            else f"script-{id(self)}"
        )

        msg = {
            "context_key": context_key,
            "user_message": prefixed,
            # Populate the context header the Manager's prompt
            # builder expects. Without this the prompt renders
            # "Workstream Unknown" and the Manager can't correctly
            # scope create_task calls. Session memory covers
            # CONVERSATIONAL state; context_data covers the prompt
            # HEADER the builder stamps in on every turn.
            "context_data": self._build_script_context_data(context_key),
            "conversation_id": conv_id,
        }
        logger.info(
            "Ingesting script notification: script=%s exec=%s ctx=%s",
            script_name, execution_id, context_key,
        )
        await self.handle_chat_message(msg, source="script")

        # Fire a board event so the UI can show a tiny "script
        # pinged the Manager" indicator without waiting for the
        # 5 s REST poll. Best-effort — a publish failure here must
        # NOT re-raise (the actual Manager ingest already succeeded
        # by this point; tripping the caller would trigger a phantom
        # retry on the outbox side).
        ws_id: str | None = None
        if context_key.startswith("workstream:"):
            ws_id = context_key.split(":", 1)[1]
        event_payload = {
            "type": "script_notified_manager",
            "script_name": script_name,
            "execution_id": execution_id,
            "workstream_id": ws_id,
            "message_preview": (content or "")[:200],
        }
        if self._router is not None:
            try:
                await self._router.publish_event(event_payload)
            except Exception:
                logger.debug(
                    "script_notified_manager publish failed "
                    "(non-fatal)",
                    exc_info=True,
                )

    def _build_script_context_data(self, context_key: str) -> dict:
        """Context-data envelope for a script-origin chat turn.

        Populates the same shape ``build_dynamic_context`` reads for
        a user-chat turn so the Manager's prompt header looks
        identical whether the current turn is user- or script-
        originated — avoids drift where a script drop sees a
        degraded ("Workstream Unknown") prompt while a user turn
        sees the full context.

        What we CAN populate from the communicator side:
          * workstream_id / name / description / goals / priority
            (from ``config_store.get_workstream``).
          * ``scopes`` for the workstream (from config_store — kept
            fresh by the backend's sync_config broadcasts).

        What we deliberately OMIT:
          * ``task_summary`` / ``kb_summary`` / ``chat_history`` —
            these would require a synchronous backend RPC, which a
            script drop's critical path shouldn't pay for. The
            Manager's persistent session already has the latest
            summary from the most recent user turn (common case) or
            from the startup sync_config broadcast. If a drop is
            the first turn of the session after a cold boot, the
            prompt will lack a board summary; the Manager can
            request one via kanban.get_board if it needs it.
        """
        if context_key == "general_chat":
            return {}
        if not context_key.startswith("workstream:"):
            return {}
        ws_id = context_key.split(":", 1)[1]
        ws = None
        try:
            ws = self._config.get_workstream(ws_id)
        except Exception:
            logger.exception(
                "Failed to look up workstream %s for script context_data",
                ws_id,
            )
        if ws is None:
            return {}
        data: dict = {
            "workstream_id": ws.get("id", ws_id),
            "workstream_name": ws.get("name", ""),
            "workstream_description": ws.get("description", ""),
            "workstream_goals": ws.get("goals", ""),
            "workstream_priority": ws.get("priority", "medium"),
        }
        # Scopes: the Manager uses these to avoid creating a second
        # 'preparing' scope, adding tasks to the wrong scope, etc.
        # Missing them on a script-origin turn would meaningfully
        # degrade the Manager's create_task decisions.
        try:
            scopes = self._config.get_scopes_for_workstream(ws_id)
        except AttributeError:
            # Older ConfigStore without the helper — omit silently
            # rather than crash the whole drop.
            scopes = None
        except Exception:
            logger.exception(
                "Failed to fetch scopes for workstream %s (script ctx)",
                ws_id,
            )
            scopes = None
        if scopes:
            data["scopes"] = scopes
        return data

    async def _publish_error_response(
        self, conversation_id: str, context_key: str, content: str,
    ) -> None:
        """Publish an error/fallback response via the message router or WS."""
        error_msg = {
            "type": "manager_response",
            "conversation_id": conversation_id,
            "context_key": context_key,
            "content": content,
            "is_streaming": False,
            "is_final": True,
        }
        try:
            if self._router is not None:
                await self._router.publish_event(error_msg)
            elif self._ws is not None:
                await self._ws.send(error_msg)
            else:
                logger.error(
                    "Cannot publish error response: no router or ws_client"
                )
        except Exception as exc:
            logger.error("Failed to publish error response: %s", exc)

    async def _publish_manager_state(
        self, context_key: str, state: str, message: str,
    ) -> None:
        """Push a Manager-state update to the chat UI.

        Distinct from ``manager_response`` (which carries assistant
        content); ``manager_state`` carries lifecycle transitions:

        | state         | meaning                                    |
        |---------------|--------------------------------------------|
        | ``working``   | Manager is mid-turn (periodic heartbeat to the UI so it can render a "thinking…" pill that reflects real activity, not a fixed timeout) |
        | ``restarting``| Supervisor killed/crashed Manager; we're respawning. The next chat message will be served by the new process. |
        | ``ready``     | Manager just came back up after a restart. |
        | ``stuck``     | Inactivity watchdog about to fire. The user can decide whether to cancel or wait. |

        Best-effort: a publish failure must never raise to the caller.
        """
        state_msg = {
            "type": "manager_state",
            "context_key": context_key,
            "state": state,
            "message": message,
        }
        try:
            if self._router is not None:
                await self._router.publish_event(state_msg)
            elif self._ws is not None:
                await self._ws.send(state_msg)
        except Exception:
            # Best-effort — never propagate. The Manager flow itself
            # is the source of truth; a missed status pill is a UX
            # nit, not a correctness issue.
            logger.debug(
                "Failed to publish manager_state (%s) for [%s] — non-fatal",
                state, context_key, exc_info=True,
            )

    # -- Event handler for Manager subprocess output --------------------------

    async def handle_manager_event(
        self, agent_name: str, event: dict[str, Any],
    ) -> None:
        """Handle an NDJSON event from the Manager subprocess.

        Called by the AgentSupervisor's stdout reader for every message
        the Manager process writes to stdout. Registered as the on_event
        callback on the supervisor.
        """
        # Only handle events from the Manager process
        if agent_name != MANAGER_AGENT_NAME:
            return

        msg_type = event.get("type", "")

        # Refresh the inactivity watchdog for any content-bearing event
        # emitted during the active exchange. The watchdog in
        # _handle_chat_message_locked compares time.monotonic() to
        # _last_activity_ts; keeping this update in ONE place (here)
        # means every new event type automatically counts as activity
        # without each _on_* handler remembering to touch the clock.
        # `ready` and `pong` are subprocess lifecycle signals, not
        # user-turn progress, so they don't refresh.
        if (
            self._active_conversation_id is not None
            and msg_type not in ("ready", "pong")
        ):
            self._last_activity_ts = time.monotonic()

        if msg_type == "response_chunk":
            await self._on_response_chunk(event)
        elif msg_type == "response_final":
            await self._on_response_final(event)
        elif msg_type == "tool_call":
            # T1.11 (review): the proxied tool-call path is dead — the
            # in-container MCP server hits the backend directly via
            # /api/offices/{oid}/tool-call and the response goes back to
            # Claude inline via JSON-RPC. The Manager subprocess never
            # actually emits `tool_call` IPC frames. Log if one appears
            # so we can investigate; don't act on it.
            logger.warning(
                "Unexpected tool_call IPC frame from Manager — the "
                "in-container MCP server should handle tool dispatch "
                "directly. Frame ignored. tool=%s",
                event.get("tool", ""),
            )
        elif msg_type == "progress":
            await self._on_progress(event)
        elif msg_type == "activity":
            await self._on_activity(event)
        elif msg_type == "error":
            await self._on_error(event)
        elif msg_type == "ready":
            logger.debug("Manager process sent ready (PID %s)", event.get("pid"))
        elif msg_type == "pong":
            pass  # Heartbeat response -- no action needed
        else:
            logger.warning("Unknown Manager event type: %s", msg_type)

    async def _on_activity(self, event: dict) -> None:
        """Forward a Manager-side activity hint to the platform.

        Activity events are lightweight "Manager is doing something"
        pulses emitted when a tool_use content block starts mid-turn.
        They drive the UI typing indicator ("Using get_board (3s)")
        and reset the 5-minute client timeout so a legitimate
        tool-heavy turn doesn't look like a dead session.
        """
        if self._active_conversation_id is None:
            return
        conversation_id = (
            event.get("conversation_id") or self._active_conversation_id
        )
        context_key = event.get("context_key", self._active_context_key)
        try:
            await self._router.publish_event({
                "type": "manager_activity",
                "conversation_id": conversation_id,
                "context_key": context_key,
                "activity": event.get("activity", "tool_use"),
                "tool": event.get("tool", ""),
            })
        except Exception as exc:
            logger.error("Failed to publish manager_activity: %s", exc)

    async def _on_response_chunk(self, event: dict) -> None:
        """Forward a streaming response chunk to the platform."""
        content = event.get("content", "")
        if not content:
            return

        # Skip if the active exchange has already been resolved (e.g.,
        # a non-fatal error already completed the exchange).
        conv_id = self._active_conversation_id
        if conv_id is None:
            return

        conversation_id = event.get("conversation_id", "") or conv_id
        context_key = event.get("context_key", self._active_context_key)

        try:
            await self._router.publish_event({
                "type": "manager_response",
                "conversation_id": conversation_id,
                "context_key": context_key,
                "content": content,
                "is_streaming": True,
                "is_final": False,
            })
        except Exception as exc:
            logger.error("Failed to publish response chunk: %s", exc)

    async def _on_response_final(self, event: dict) -> None:
        """Handle end-of-response from the Manager subprocess."""
        context_key = event.get("context_key", self._active_context_key)
        session_id = event.get("session_id", "")

        # Update session ID for this context (the subprocess may have
        # created a new session or resumed an existing one).
        if session_id and context_key:
            await self._sessions.save_session(context_key, session_id)

        # Skip publishing the final marker if the exchange was already
        # completed by an error handler. This prevents duplicate
        # is_final=True messages reaching the UI.
        if self._response_done.is_set():
            logger.debug("response_final received but exchange already done; skipping publish")
            return

        conversation_id = event.get("conversation_id", "") or self._active_conversation_id
        token_cost = event.get("token_cost", 0.0)

        try:
            await self._router.publish_event({
                "type": "manager_response",
                "conversation_id": conversation_id,
                "context_key": context_key,
                "content": "",
                "is_streaming": False,
                "is_final": True,
                "token_cost": token_cost,
            })
        except Exception as exc:
            logger.error("Failed to publish response final: %s", exc)

        # Clear the "Manager working — Xs elapsed" status pill. The
        # heartbeat loop in ``_handle_chat_message_locked`` publishes
        # working/stuck states every 20s during the turn, but nothing
        # clears them when the turn ends naturally — the pill stays
        # frozen on the last working heartbeat until the user starts
        # a new turn. Publishing ``idle`` with an empty message
        # causes the frontend's ChatPanel to hide the pill (it gates
        # rendering on ``managerState.message`` truthiness).
        #
        # Only reached when this is a NATURAL response_final — the
        # ``_response_done.is_set()`` early-return above ensures
        # cancel / error paths (which set their own terminal state
        # like ``cancelled``) aren't overwritten by this idle state.
        await self._publish_manager_state(context_key, "idle", "")

        # Signal handle_chat_message() that the response is complete.
        # Always set this even if publish failed so handle_chat_message
        # doesn't hang until timeout.
        self._response_done.set()

    async def _on_progress(self, event: dict) -> None:
        """Handle a progress event from the Manager (e.g., tool usage)."""
        event_type = event.get("event_type", "")
        if event_type in ("checkpoint", "tool_run"):
            logger.debug(
                "Manager progress [%s]: %s",
                event_type, event.get("content", "")[:100],
            )

    async def _on_error(self, event: dict) -> None:
        """Handle an error from the Manager subprocess."""
        error_msg = event.get("message", "Unknown error")
        is_fatal = event.get("fatal", False)

        logger.error(
            "Manager error (fatal=%s): %s", is_fatal, error_msg[:500],
        )

        # Capture the error for the active exchange
        self._response_error = error_msg

        if is_fatal:
            # Fatal error -- process will exit; trigger restart
            self._response_done.set()  # Unblock handle_chat_message
            await self._restart_manager(reason=f"fatal error: {error_msg[:200]}")
        else:
            # Non-fatal error -- subprocess still alive but exchange failed
            self._response_done.set()

    # T1.11 (review): the proxied tool-call path was deleted. Tool
    # dispatch happens entirely in the in-container MCP server which
    # hits POST /api/offices/{oid}/tool-call directly. The Manager
    # subprocess never sends `tool_call` IPC frames in production. The
    # legacy `_on_tool_call`, `_execute_tool`, `_tool_to_action`
    # helpers were removed along with the matching `proxy_tool_call`
    # machinery in agent_worker.py. See `_handle_event` above for the
    # warning-and-ignore branch.

    # -- Crash recovery -------------------------------------------------------

    async def handle_manager_crash(self, exit_code: int) -> None:
        """Called when the Manager process exits unexpectedly.

        The supervisor detects the process exit via _monitor_exit and
        calls the on_event callback with a fatal error. This method is
        called from handle_manager_event when a fatal error for the
        manager is received, or can be called directly.
        """
        logger.error("Manager process exited with code %d", exit_code)

        # If there is an active conversation, signal failure
        if self._active_conversation_id:
            self._response_error = (
                f"The Manager session crashed (exit code {exit_code}). "
                "Please resend your message."
            )
            self._response_done.set()

        # Clear session for the current context (it may be corrupted)
        await self._sessions.clear_session(self._active_context_key)

        # Attempt restart
        await self._restart_manager(reason=f"exit code {exit_code}")

    # -- is_busy property -----------------------------------------------------

    @property
    def is_busy(self) -> bool:
        """Whether the Manager is currently processing a message.

        Derived from whether we are waiting for a response (active
        conversation_id set). Also checks the supervisor's agent state.
        """
        # If we have an active conversation awaiting response, we are busy
        if self._active_conversation_id is not None:
            return True

        # In process-per-agent mode, also check the supervisor
        if self._supervisor is not None:
            try:
                from src.orchestrator.agent_supervisor import AgentState
                state = self._supervisor.get_agent_state(MANAGER_AGENT_NAME)
                return state == AgentState.WORKING
            except Exception:
                pass

        return False

    # -- Scope completion notification ---------------------------------------

    async def ingest_scope_completed(self, message: dict) -> None:
        """Route a scope-completion event from the backend into the
        Manager so it can plan the next step (CHAT-2026-05).

        The backend fires this when an executing scope transitions to
        ``done`` AND there's no next scope queued in the workstream.
        Without this nudge, the Manager waits indefinitely after each
        scope even when the user's overall request needs more work.

        Body is a synthetic ``[Scope Completed: ...]`` chat turn — the
        Manager treats it like a user message but the prefix makes it
        clear who's speaking. We rely on the existing
        ``handle_chat_message(source="script")`` path so script and
        scope notifications share the same serialisation /
        user-turn-deferral guarantees.
        """
        context_key = (message or {}).get("context_key", "general_chat")
        readable_id = (message or {}).get("scope_readable_id", "")
        scope_name = (message or {}).get("scope_name") or readable_id
        task_count = (message or {}).get("task_count") or 0

        logger.info(
            "Ingesting scope_completed notification for %s (%s, %d tasks)",
            readable_id, context_key, task_count,
        )

        lines = [
            f"[Scope Completed: {readable_id}]",
            (
                f'Scope "{scope_name}" finished. {task_count} '
                f"task{'s' if task_count != 1 else ''} done. No "
                "follow-up scope is queued."
            ),
            "",
            "Assess the current workstream state via list_scopes / "
            "get_board and decide the next step: plan and activate "
            "the next scope, ask the user for clarification if the "
            "overall goal isn't clear, or report completion if the "
            "original request is fulfilled.",
        ]
        content = "\n".join(lines)

        # Deterministic conversation id derived from the scope id so
        # a duplicate event (rare but possible on retries) doesn't
        # double-prompt the Manager.
        conv_id = (
            f"scope-{readable_id}" if readable_id
            else f"scope-{id(self)}"
        )

        msg = {
            "context_key": context_key,
            "user_message": content,
            "context_data": self._build_script_context_data(context_key),
            "conversation_id": conv_id,
        }
        await self.handle_chat_message(msg, source="script")

    # -- Action-request decision notification --------------------------------

    async def ingest_action_request_decided(self, message: dict) -> None:
        """Route a user's action_request decision into the Manager.

        Mirrors the scope_completed nudge: the backend fires this
        when the user clicks approve / reject in the Inbox panel,
        and the Manager gets a synthetic chat turn so it can react
        without polling.

        Common cases:
          * Decision was ``approved`` and ``resulting_task_id`` is
            set → a new task landed; Manager may want to verify
            placement, brief, dependencies.
          * Decision was ``approved`` with no resulting task → the
            user said "do it" for a sensitive action the worker
            wanted permission for. The worker is no longer blocked.
          * Decision was ``rejected`` → the worker that proposed
            this is blocked on a different path. Manager may need
            to redirect the original task.
        """
        context_key = (message or {}).get("context_key", "general_chat")
        request_id = (message or {}).get("request_id", "")
        request_type = (message or {}).get("request_type", "unknown")
        decision = (message or {}).get("decision", "decided")
        notes = (message or {}).get("decision_notes", "") or ""
        resulting_task = (message or {}).get("resulting_task_id") or None
        source_task = (message or {}).get("source_task_id") or None
        requesting_agent = (message or {}).get("requesting_agent", "")

        logger.info(
            "Ingesting action_request_decided: id=%s decision=%s type=%s",
            request_id[:8] if request_id else "?", decision, request_type,
        )

        lines = [
            f"[Action Request {decision.title()}: {request_type}]",
            (
                f"The user just {decision} an action request"
                + (f" from {requesting_agent}" if requesting_agent else "")
                + f" (type: {request_type})."
            ),
        ]
        if notes:
            lines.append(f"User notes: {notes}")
        if resulting_task:
            lines.append(
                f"A new task was created from the approval: {resulting_task}. "
                "Verify it has the right brief and dependencies."
            )
        if source_task:
            lines.append(
                f"The request originated from task {source_task}. "
                "Check whether that task is now unblocked and any "
                "follow-up planning is needed."
            )
        lines.append(
            "Assess what (if anything) you need to do next: re-plan, "
            "update a task brief, post a comment, or report back to "
            "the user. Do NOT take action that the user already "
            "explicitly rejected."
        )
        content = "\n".join(lines)

        # Deterministic conv id so a duplicate delivery of the same
        # decision doesn't double-prompt.
        conv_id = (
            f"action-req-{request_id}" if request_id
            else f"action-req-{id(self)}"
        )
        msg = {
            "context_key": context_key,
            "user_message": content,
            "context_data": self._build_script_context_data(context_key),
            "conversation_id": conv_id,
        }
        await self.handle_chat_message(msg, source="script")

    # -- Auto-decide nudge for Manager-decidable action requests ------------

    async def ingest_action_request_auto_decide(self, message: dict) -> None:
        """Route a newly-created Manager-decidable action_request into
        the Manager so it decides without waiting on the user.

        The backend fires this immediately after creating an
        action_request with ``requires_user=False`` (Phase 1 routing
        policy: workstream / scope categories at non-critical
        severity). The body is a synthetic chat turn that names the
        request id + payload + a brief instruction reminding the
        Manager to use ``mcp__cubicle-tools__decide_action_request``
        with the right id.

        Sister handler to ``ingest_scope_completed`` /
        ``ingest_action_request_decided`` — they all use
        ``handle_chat_message(source="script")`` so they share the
        same serialisation guarantees and never preempt an active
        user turn.
        """
        context_key = (message or {}).get("context_key", "general_chat")
        request_id = (message or {}).get("request_id", "")
        request_type = (message or {}).get("request_type", "unknown")
        severity = (message or {}).get("severity", "medium")
        category = (message or {}).get("category", "workstream")
        requesting_agent = (message or {}).get("requesting_agent", "")
        source_task = (message or {}).get("source_task_id") or None
        scope_id = (message or {}).get("scope_id") or None
        payload = (message or {}).get("payload") or {}
        justification = (message or {}).get("justification") or ""

        logger.info(
            "Ingesting action_request_auto_decide: id=%s type=%s "
            "severity=%s category=%s",
            (request_id[:8] if request_id else "?"),
            request_type, severity, category,
        )

        # Format a compact summary so the Manager has the essentials
        # without parsing the whole payload itself. Keep this terse —
        # the Manager will pull the full row via the action_request
        # service if it needs to.
        payload_lines: list[str] = []
        for key, value in (payload.items() if isinstance(payload, dict) else []):
            v = str(value)
            if len(v) > 200:
                v = v[:197] + "…"
            payload_lines.append(f"  - {key}: {v}")

        lines = [
            f"[Action Request — Auto-Decide: {request_type}]",
            (
                f"A new action_request landed in the Manager-auto-decide "
                f"queue (id `{request_id}`, severity `{severity}`, "
                f"category `{category}`). The user has NOT been "
                "notified — you decide directly."
            ),
            "",
            f"Requested by: {requesting_agent or '(system)'}",
        ]
        if source_task:
            lines.append(f"Source task: {source_task}")
        if scope_id:
            lines.append(f"Scope: {scope_id}")
        if justification:
            lines.append("")
            lines.append("Justification:")
            for ln in justification.splitlines():
                lines.append(f"  {ln}")
        if payload_lines:
            lines.append("")
            lines.append("Payload:")
            lines.extend(payload_lines)
        lines.append("")
        lines.append(
            "**Decide now via "
            "`mcp__cubicle-tools__decide_action_request`** with the "
            "request_id above and either `decision=\"approved\"` "
            "(if the proposal fits the workstream goal) or "
            "`decision=\"rejected\"` (with `decision_notes` "
            "explaining why)."
        )
        lines.append("")
        lines.append(
            "**CRITICAL — side-effects on approval are NOT automatic "
            "for most request types.** Only `create_task` (creates "
            "the task) and `request_clarification` (posts an answer "
            "activity on the source task) apply their side-effect "
            "inside `decide_action_request`. For ALL other types — "
            "`create_subtask`, `update_task`, `move_task`, "
            "`split_into_scope`, `request_review_check`, "
            "`escalate_blocker`, `propose_artifact_handoff`, "
            "`board_overview`, `setup_office_secret` — approve "
            "records the decision but you MUST ALSO call the "
            "corresponding tool yourself in the SAME turn:"
        )
        lines.append("")
        lines.append("  - `create_subtask` → call `create_task` with `parent_task_id`")
        lines.append("  - `update_task` / `move_task` → call those tools")
        lines.append("  - `split_into_scope` → call `create_scope` then `create_task` × N")
        lines.append("  - `request_review_check` → call `update_task` to set reviewer (or trigger your own review)")
        lines.append("  - `propose_artifact_handoff` → create the consumer task that needs the artifact")
        lines.append("  - `escalate_blocker` → take the user-visible remedial action (typically the user is the actor; you may need to comment or create a clarifying task)")
        lines.append("  - `board_overview` → no side-effect needed; the row is informational")
        lines.append("  - `setup_office_secret` → no Manager-side action; the user adds the secret in Settings → Security and the backend auto-resolves the row")
        lines.append("")
        lines.append(
            "Approving without the follow-up tool call silently "
            "drops the proposed work. Reject closes the row with "
            "no side-effect. The `informational` type is "
            "acknowledge-only — neither approve nor reject applies; "
            "use `decide_action_request` with `decision=\"approved\"` "
            "to mark it acknowledged. See your Manager CLAUDE.md — "
            "the **Auto-Deciding Action Requests** section — for "
            "the full decision tree."
        )
        content = "\n".join(lines)

        # Deterministic conv id so a duplicate delivery doesn't
        # double-prompt the Manager.
        conv_id = (
            f"auto-decide-{request_id}" if request_id
            else f"auto-decide-{id(self)}"
        )
        msg = {
            "context_key": context_key,
            "user_message": content,
            "context_data": self._build_script_context_data(context_key),
            "conversation_id": conv_id,
        }
        await self.handle_chat_message(msg, source="script")

    # -- Cancellation ---------------------------------------------------------

    async def cancel_current_turn(self, message: dict) -> None:
        """Cancel the Manager's in-flight turn (Chat-v2 / CHAT-005).

        Backend → router dispatches here when the user clicks "Cancel"
        on the chat UI. The path:

        1. Send ``cancel_task`` to the Manager subprocess via the
           supervisor. The agent_worker's reader_loop accepts it
           concurrently with the in-flight CLI call and cancels the
           tracked session task; ``stream_cli_session`` then kills the
           ``docker exec`` subprocess.
        2. Publish ``manager_state(cancelled)`` so the UI status pill
           switches immediately — the agent_worker's final NDJSON
           response_final may take a moment to land.
        3. Stamp ``_response_error`` and set ``_response_done`` so the
           in-flight chat handler unblocks even if the subprocess
           dies before emitting a clean response_final.

        Idempotent / safe no-op when no turn is in flight (e.g. the
        user clicked Cancel a tick after the Manager already finished).
        """
        context_key = (message or {}).get(
            "context_key", self._active_context_key,
        )

        if self._active_conversation_id is None:
            logger.info(
                "cancel_current_turn [%s]: no active turn — no-op",
                context_key,
            )
            return
        # Race window: ``_response_done`` is set the instant the
        # turn ends (natural-final OR error). The finally block then
        # clears ``_active_conversation_id`` ~ms later. A click on
        # Cancel landing in between would otherwise publish
        # ``cancelled`` for a turn that ACTUALLY completed normally,
        # and the finally would then skip the idle publish (because
        # ``_turn_cancelled`` got set). Net result: the user sees
        # "Cancelled by user" on a turn that wasn't cancelled. Gate
        # on ``_response_done`` to short-circuit these late clicks.
        if self._response_done.is_set():
            logger.info(
                "cancel_current_turn [%s]: turn already finished "
                "(_response_done set) — no-op",
                context_key,
            )
            return

        logger.info(
            "Cancelling Manager turn [%s] (conv=%s)",
            context_key,
            (self._active_conversation_id or "")[:8],
        )

        # 1) Tell the Manager subprocess to cancel. Best-effort: a
        #    delivery failure still lets us emit the user-facing
        #    cancelled state below.
        if self._supervisor is not None:
            try:
                await self._supervisor._send_to_agent(MANAGER_AGENT_NAME, {
                    "type": "cancel_task",
                    "reason": "user_cancel",
                })
            except Exception as exc:
                logger.warning(
                    "Failed to send cancel_task to Manager subprocess: %s",
                    exc,
                )

        # 2) Tell the UI immediately so the pill flips without waiting
        #    for the subprocess to wind down its event stream. Set
        #    ``_turn_cancelled`` BEFORE publishing so the user-message
        #    handler's finally block (which fires next as the chat
        #    handler unblocks) sees the flag and skips its own
        #    ``manager_state("idle", "")`` publish — otherwise idle
        #    would race-overwrite this cancelled message ~0-50ms
        #    later.
        self._turn_cancelled = True
        await self._publish_manager_state(
            context_key, "cancelled", "Cancelled by user.",
        )

        # 3) Unblock the chat handler. The subprocess SHOULD send a
        #    clean response_final after cancellation, but the response
        #    handler dedupes on ``_response_done.is_set()`` so a second
        #    final is a no-op.
        self._response_error = (
            "The current turn was cancelled. Send a new message when "
            "you're ready."
        )
        self._response_done.set()

    # -- Context switching ----------------------------------------------------

    async def handle_switch_context(self, message: dict) -> None:
        """Handle a context switch request (no chat message).

        Updates the session manager. The actual subprocess session_id
        change happens on the next chat_message.
        """
        context_key = message.get("context_key", "general_chat")
        self._sessions.switch_context(context_key)
        self._active_context_key = context_key

    # -- Auto-orchestration (REMOVED) -----------------------------------------
    # Review and blocked task management is now handled by the Manager
    # Assistant (Board Operator) via per-agent queues. The AI Manager
    # focuses exclusively on user chat and task creation.


# -- Module-level helper (usable without a controller instance) -----------
#
# Moved to ``src.orchestrator.manager_context`` so agent_worker can import
# it without pulling the entire controller / supervisor / WS-client chain.
# Re-exported here so the historical
# ``from src.orchestrator.manager_controller import build_dynamic_context``
# import keeps working.
from src.orchestrator.manager_context import build_dynamic_context  # noqa: E402, F401

