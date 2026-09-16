"""Load current Manager context and recover history once per CLI session."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from src.orchestrator.manager_context import render_chat_history

if TYPE_CHECKING:
    from src.transport.ws_transport import WsTransport


async def load_manager_context(
    router: WsTransport, message: dict, *, fresh: bool
) -> dict:
    """Rebuild snapshots after turn serialization; never admit an ungrounded turn."""
    context_key = message.get("context_key", "general_chat")
    params = {"context_key": context_key, "include_history": fresh}
    # User chat conversation IDs are their persisted message UUIDs. Synthetic
    # event IDs are not messages and must not exclude arbitrary history rows.
    if message.get("turn_id"):
        params["turn_id"] = message["turn_id"]
    elif not isinstance(message.get("_turn_outcome"), dict):
        try:
            params["exclude_message_id"] = str(
                uuid.UUID(str(message.get("conversation_id")))
            )
        except (ValueError, TypeError, AttributeError):
            pass
    result = await router.ws_client.request("get_manager_context", params, timeout=10)
    if not isinstance(result, dict) or result.get("context_key") != context_key:
        raise ValueError("Manager context response does not match this conversation")
    data = result.get("context_data")
    if not isinstance(data, dict) or result.get("error"):
        raise ValueError("Manager context is unavailable")
    if context_key.startswith("workstream:"):
        if data.get("workstream_id") != context_key.split(":", 1)[1]:
            raise ValueError("Manager context belongs to a different workstream")
    elif context_key != "general_chat" or data.get("workstream_id"):
        raise ValueError("Invalid Manager conversation context")
    if fresh and not isinstance(data.get("chat_history"), str):
        raise ValueError("Manager conversation recovery history is unavailable")
    context = dict(data)
    # These flags describe this exact user's choice event, not current DB state.
    # All other data (mode, instructions, board, memory) comes from the fresh RPC.
    original = message.get("context_data") or {}
    if isinstance(original, dict):
        if original.get("choice_superseded") is True:
            context["choice_superseded"] = True
        if isinstance(original.get("choice_handoff_note"), str):
            context["choice_handoff_note"] = original["choice_handoff_note"]
    return context


def history_bootstrap(content: str, context: dict, *, fresh: bool) -> str:
    """Place recovery evidence into the transcript, not a temporary system prompt."""
    history = context.get("chat_history") if fresh else None
    if not history:
        return content
    return render_chat_history(history) + "\n\n## Current message\n" + content
