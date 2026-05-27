"""Manager-subprocess streaming-event handlers.

Extracted from ``manager_controller.py`` so that file can stay focused
on lifecycle (spawn / restart / stop), the chat-message dispatcher,
and the ingest/cancel paths. Owns:

* ``handle_manager_event`` — top-level NDJSON dispatcher called by the
  supervisor's stdout reader for every Manager subprocess frame.
* ``on_response_chunk`` / ``on_response_final`` — text-streaming
  forward path to the platform.
* ``on_activity`` — Manager "I'm using tool X" pulse forward path.
* ``on_progress`` — checkpoint / tool_run log-only path.
* ``on_error`` — fatal vs non-fatal error capture + restart trigger.

Each function takes the owning ``ManagerController`` as its first
parameter. The class's own methods (``_on_response_chunk`` etc.)
become one-line adapters that delegate here — same pattern as the
wave-10 ``_agent_worker_*`` extractions and the wave-11
``_mcp_script_exec`` split.

The ``self.x`` references in the original bodies map 1:1 to
``controller.x`` here; this is a behaviour-preserving mechanical
rewrite, not a redesign.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestrator.manager_controller import ManagerController

logger = logging.getLogger("src.orchestrator.manager_controller")

# Pinned local copy — the dispatcher needs it to filter Manager
# frames from other agents' frames. Kept in sync with the parent's
# module-level constant by convention; both modules import once at
# process start so the value is fixed.
MANAGER_AGENT_NAME = "manager"


async def handle_manager_event(
    controller: "ManagerController",
    agent_name: str,
    event: dict,
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
    # without each on_* handler remembering to touch the clock.
    # `ready` and `pong` are subprocess lifecycle signals, not
    # user-turn progress, so they don't refresh.
    if (
        controller._active_conversation_id is not None
        and msg_type not in ("ready", "pong")
    ):
        controller._last_activity_ts = time.monotonic()

    if msg_type == "response_chunk":
        await controller._on_response_chunk(event)
    elif msg_type == "response_final":
        await controller._on_response_final(event)
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
        await controller._on_progress(event)
    elif msg_type == "activity":
        await controller._on_activity(event)
    elif msg_type == "error":
        await controller._on_error(event)
    elif msg_type == "ready":
        logger.debug("Manager process sent ready (PID %s)", event.get("pid"))
    elif msg_type == "pong":
        pass  # Heartbeat response -- no action needed
    else:
        logger.warning("Unknown Manager event type: %s", msg_type)


async def on_activity(
    controller: "ManagerController", event: dict,
) -> None:
    """Forward a Manager-side activity hint to the platform.

    Activity events are lightweight "Manager is doing something"
    pulses emitted when a tool_use content block starts mid-turn.
    They drive the UI typing indicator ("Using get_board (3s)")
    and reset the 5-minute client timeout so a legitimate
    tool-heavy turn doesn't look like a dead session.
    """
    if controller._active_conversation_id is None:
        return
    conversation_id = (
        event.get("conversation_id") or controller._active_conversation_id
    )
    context_key = event.get("context_key", controller._active_context_key)
    try:
        await controller._router.publish_event({
            "type": "manager_activity",
            "conversation_id": conversation_id,
            "context_key": context_key,
            "activity": event.get("activity", "tool_use"),
            "tool": event.get("tool", ""),
        })
    except Exception as exc:
        logger.error("Failed to publish manager_activity: %s", exc)


async def on_response_chunk(
    controller: "ManagerController", event: dict,
) -> None:
    """Forward a streaming response chunk to the platform."""
    content = event.get("content", "")
    if not content:
        return

    # Skip if the active exchange has already been resolved (e.g.,
    # a non-fatal error already completed the exchange).
    conv_id = controller._active_conversation_id
    if conv_id is None:
        return

    conversation_id = event.get("conversation_id", "") or conv_id
    context_key = event.get("context_key", controller._active_context_key)

    try:
        await controller._router.publish_event({
            "type": "manager_response",
            "conversation_id": conversation_id,
            "context_key": context_key,
            "content": content,
            "is_streaming": True,
            "is_final": False,
        })
    except Exception as exc:
        logger.error("Failed to publish response chunk: %s", exc)


async def on_response_final(
    controller: "ManagerController", event: dict,
) -> None:
    """Handle end-of-response from the Manager subprocess."""
    context_key = event.get("context_key", controller._active_context_key)
    session_id = event.get("session_id", "")

    # Update session ID for this context (the subprocess may have
    # created a new session or resumed an existing one).
    if session_id and context_key:
        await controller._sessions.save_session(context_key, session_id)

    # Skip publishing the final marker if the exchange was already
    # completed by an error handler. This prevents duplicate
    # is_final=True messages reaching the UI.
    if controller._response_done.is_set():
        logger.debug("response_final received but exchange already done; skipping publish")
        return

    conversation_id = event.get("conversation_id", "") or controller._active_conversation_id
    token_cost = event.get("token_cost", 0.0)

    try:
        await controller._router.publish_event({
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
    await controller._publish_manager_state(context_key, "idle", "")

    # Signal handle_chat_message() that the response is complete.
    # Always set this even if publish failed so handle_chat_message
    # doesn't hang until timeout.
    controller._response_done.set()


async def on_progress(
    controller: "ManagerController", event: dict,
) -> None:
    """Handle a progress event from the Manager (e.g., tool usage)."""
    event_type = event.get("event_type", "")
    if event_type in ("checkpoint", "tool_run"):
        logger.debug(
            "Manager progress [%s]: %s",
            event_type, event.get("content", "")[:100],
        )


async def on_error(
    controller: "ManagerController", event: dict,
) -> None:
    """Handle an error from the Manager subprocess."""
    error_msg = event.get("message", "Unknown error")
    is_fatal = event.get("fatal", False)

    logger.error(
        "Manager error (fatal=%s): %s", is_fatal, error_msg[:500],
    )

    # Capture the error for the active exchange
    controller._response_error = error_msg

    if is_fatal:
        # Fatal error -- process will exit; trigger restart
        controller._response_done.set()  # Unblock handle_chat_message
        await controller._restart_manager(reason=f"fatal error: {error_msg[:200]}")
    else:
        # Non-fatal error -- subprocess still alive but exchange failed
        controller._response_done.set()
