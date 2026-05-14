"""WebSocket protocol message types for Communicator ↔ Platform communication.

See docs/specs/ws-protocol.md section 2 (Connector Gateway) for details.
"""

from __future__ import annotations

import json
from typing import Any


# -- Server → Communicator message types --

MSG_CHAT_MESSAGE = "chat_message"
MSG_SWITCH_CONTEXT = "switch_context"
MSG_SYNC_CONFIG = "sync_config"
MSG_TASK_READY = "task_ready"
MSG_TASK_REWORK = "task_rework"
MSG_SCRIPT_EXECUTE = "script_execute"
MSG_SCRIPT_SECRET_UPDATE = "script_secret_update"
MSG_SKILL_SECRET_UPDATE = "skill_secret_update"
MSG_TASK_KILL = "task_kill"
MSG_PING = "ping"

# -- Communicator → Server message types --

MSG_PONG = "pong"

# -- Request/Response pattern --

MSG_RESPONSE = "response"


def encode_message(message: dict[str, Any]) -> str:
    """Encode a message dict to JSON string."""
    return json.dumps(message, default=str)


def decode_message(raw: str) -> dict[str, Any]:
    """Decode a JSON string to message dict."""
    return json.loads(raw)
