"""agent_protocol.py -- IPC message types for Orchestrator <-> Agent communication.

All messages are single-line JSON objects (NDJSON) sent over stdin/stdout pipes.
Messages are newline-terminated. Each message has a "type" field for routing.

Orchestrator -> Agent (stdin):
  assign_task, chat_message, tool_response, cancel_task, shutdown, ping

Agent -> Orchestrator (stdout):
  ready, progress, tool_call, task_complete, response_chunk, response_final,
  error, pong

Protocol design:
- Fire-and-forget for events (progress, response_chunk, error, pong, ready).
- Request/response for tool calls (tool_call has request_id, correlated with
  tool_response carrying the same request_id).
- No protocol version field in PoC. Future versions can add one.

Usage:
  # Agent side (synchronous write to stdout):
  write_message(ReadyMessage(pid=os.getpid(), agent_name="analyst"))

  # Orchestrator side (async read from subprocess stdout):
  async for msg in read_messages(process.stdout):
      if msg["type"] == "ready":
          ...
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    """All IPC message types."""

    # Orchestrator -> Agent
    ASSIGN_TASK = "assign_task"
    CHAT_MESSAGE = "chat_message"
    TOOL_RESPONSE = "tool_response"
    CANCEL_TASK = "cancel_task"
    SHUTDOWN = "shutdown"
    PING = "ping"

    # Agent -> Orchestrator
    READY = "ready"
    PROGRESS = "progress"
    TOOL_CALL = "tool_call"
    TASK_COMPLETE = "task_complete"
    RESPONSE_CHUNK = "response_chunk"
    RESPONSE_FINAL = "response_final"
    # Lightweight "Manager is doing something" signal. Emitted for any
    # tool use that doesn't fit the narrow set of board-write actions
    # (kanban read tools, Read/Grep, file ops, etc.). Drives the UI
    # typing indicator and resets the 5-minute client timeout so the
    # user doesn't see a false "Manager stopped responding" while the
    # subprocess is still making tool calls.
    ACTIVITY = "activity"
    ERROR = "error"
    PONG = "pong"


# ---------------------------------------------------------------------------
# Orchestrator -> Agent messages
# ---------------------------------------------------------------------------


@dataclass
class AssignTaskMessage:
    """Assign a task to the agent process.

    Sent after the agent sends a "ready" message. Contains the full task data
    including the brief, agent configuration, and workspace path. The agent
    reads the brief, builds its prompt, and starts an SDK session.
    """

    type: str = field(default=MessageType.ASSIGN_TASK, init=False)
    task_id: str = ""
    readable_id: str = ""
    title: str = ""
    brief: dict[str, Any] = field(default_factory=dict)
    agent_config: dict[str, Any] = field(default_factory=dict)
    workspace_path: str = ""
    session_id: str | None = None
    rework_feedback: str | None = None
    rework_count: int = 0
    workstream_name: str = ""
    workstream_short_code: str = ""
    backend_url: str = ""
    office_id: str = ""


@dataclass
class ChatMessage:
    """Send a user chat message to the Manager agent.

    Sent to the Manager subprocess when a user sends a message in the chat UI.
    The Manager process runs an SDK query with the user's message and streams
    response chunks back via ResponseChunkMessage / ResponseFinalMessage.
    """

    type: str = field(default=MessageType.CHAT_MESSAGE, init=False)
    context_key: str = ""
    content: str = ""
    context_data: dict[str, Any] = field(default_factory=dict)
    conversation_id: str = ""
    session_id: str | None = None
    agent_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResponseMessage:
    """Response to a tool_call from the agent.

    Sent by the Orchestrator after executing a proxied tool call (e.g.,
    kanban.create_task). The request_id correlates with the original
    ToolCallMessage. Either result or error is set, never both.
    """

    type: str = field(default=MessageType.TOOL_RESPONSE, init=False)
    request_id: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class CancelTaskMessage:
    """Cancel the current task and prepare for a new one or shutdown.

    Sent when the Manager cancels a task or the Orchestrator needs to
    preempt the current work. The agent should abort the current SDK
    session and send a TaskCompleteMessage with status="blocked".
    """

    type: str = field(default=MessageType.CANCEL_TASK, init=False)
    reason: str = ""


@dataclass
class ShutdownMessage:
    """Gracefully shut down the agent process.

    Sent during Orchestrator shutdown. The agent should finish any current
    operation within the grace period and then exit with code 0. If the
    agent does not exit within the grace period, the Orchestrator sends
    SIGTERM followed by SIGKILL.
    """

    type: str = field(default=MessageType.SHUTDOWN, init=False)
    grace_period_seconds: int = 30


@dataclass
class PingMessage:
    """Heartbeat ping from Orchestrator.

    Sent every 30 seconds. The agent must respond with PongMessage.
    If the Orchestrator receives no pong within 90 seconds (3 missed pings),
    the agent process is considered dead and killed.

    (Amendment A4: Orchestrator sends PING every 30s, kills after 90s.)
    """

    type: str = field(default=MessageType.PING, init=False)


# ---------------------------------------------------------------------------
# Agent -> Orchestrator messages
# ---------------------------------------------------------------------------


@dataclass
class ReadyMessage:
    """Agent process has started and is ready to receive tasks.

    Sent once during agent startup after MCP servers and signal handlers
    are initialized. The Orchestrator waits for this message before
    sending assign_task or chat_message. If not received within
    SPAWN_TIMEOUT_SECONDS (30s), the process is killed.
    """

    type: str = field(default=MessageType.READY, init=False)
    pid: int = 0
    agent_name: str = ""


@dataclass
class ProgressMessage:
    """Progress event during task execution (checkpoint, tool_run, etc.).

    Fire-and-forget. The Orchestrator forwards these to the backend to
    update the task's Activity feed. Does not require a response.
    """

    type: str = field(default=MessageType.PROGRESS, init=False)
    task_id: str = ""
    event_type: str = ""  # "checkpoint", "tool_run", "question", etc.
    content: str = ""
    details: dict[str, Any] | None = None
    token_cost: float | None = None


@dataclass
class ToolCallMessage:
    """Agent needs to call a proxied tool (create_task, move_task, etc.).

    Request/response pattern. The agent creates a unique request_id and
    waits for a ToolResponseMessage with the matching request_id.

    (Amendment C-1: Tool call futures timeout after 60 seconds. If the
    Orchestrator does not respond within 60s, the agent sends an error
    and moves the task to blocked.)
    """

    type: str = field(default=MessageType.TOOL_CALL, init=False)
    request_id: str = ""
    tool: str = ""  # e.g., "kanban.create_task"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskCompleteMessage:
    """Agent has finished the task.

    Sent when the agent has completed its work. The status is typically
    "review" (work done, awaiting Manager review) or "blocked" (agent
    could not complete, needs help). The Orchestrator transitions the
    agent state to READY after receiving this.
    """

    type: str = field(default=MessageType.TASK_COMPLETE, init=False)
    task_id: str = ""
    status: str = "review"  # "review" or "blocked"
    comment: str = ""
    token_cost: float = 0.0
    session_id: str = ""


@dataclass
class ResponseChunkMessage:
    """Streaming response chunk (Manager only).

    Sent by the Manager process during a chat query. Multiple chunks
    form a complete response. The Orchestrator forwards each chunk to
    the backend for real-time display in the chat UI.
    """

    type: str = field(default=MessageType.RESPONSE_CHUNK, init=False)
    conversation_id: str = ""
    context_key: str = ""
    content: str = ""


@dataclass
class ResponseFinalMessage:
    """Final message marking end of Manager response.

    Sent after all ResponseChunkMessages for a query. Contains the total
    token cost and the session_id for resumption. The Orchestrator
    transitions the Manager state from WORKING back to READY.
    """

    type: str = field(default=MessageType.RESPONSE_FINAL, init=False)
    conversation_id: str = ""
    context_key: str = ""
    token_cost: float = 0.0
    session_id: str = ""


@dataclass
class ActivityMessage:
    """Lightweight "agent is doing something" signal (Manager only).

    Emitted when a tool_use content block starts mid-turn. Drives the
    UI typing indicator (``Using get_board (3s)``) and resets the
    client-side 5-minute "no response" watchdog so a legitimate
    tool-heavy turn isn't mistaken for a stuck session. The Orchestrator
    forwards this to the backend as a ``manager_activity`` WS event.
    """

    type: str = field(default=MessageType.ACTIVITY, init=False)
    conversation_id: str = ""
    context_key: str = ""
    # "tool_use" is the only value in use today; kept as a field so
    # future signals (e.g. "thinking", "retry") can share the route.
    activity: str = "tool_use"
    # Tool name, prefix stripped for readability
    # (``mcp__cubicle-tools__get_board`` → ``get_board``).
    tool: str = ""


@dataclass
class ErrorMessage:
    """Error from the agent process.

    Non-fatal errors are logged by the Orchestrator but the agent continues.
    Fatal errors cause the Orchestrator to kill the agent process.
    """

    type: str = field(default=MessageType.ERROR, init=False)
    message: str = ""
    task_id: str | None = None
    fatal: bool = False  # If true, the agent process will exit


@dataclass
class PongMessage:
    """Heartbeat pong from agent.

    Response to PingMessage. Resets the heartbeat timeout counter
    in the Orchestrator.
    """

    type: str = field(default=MessageType.PONG, init=False)


# ---------------------------------------------------------------------------
# Message type registry (for deserialization)
# ---------------------------------------------------------------------------


MESSAGE_CLASSES: dict[str, type] = {
    MessageType.ASSIGN_TASK: AssignTaskMessage,
    MessageType.CHAT_MESSAGE: ChatMessage,
    MessageType.TOOL_RESPONSE: ToolResponseMessage,
    MessageType.CANCEL_TASK: CancelTaskMessage,
    MessageType.SHUTDOWN: ShutdownMessage,
    MessageType.PING: PingMessage,
    MessageType.READY: ReadyMessage,
    MessageType.PROGRESS: ProgressMessage,
    MessageType.TOOL_CALL: ToolCallMessage,
    MessageType.TASK_COMPLETE: TaskCompleteMessage,
    MessageType.RESPONSE_CHUNK: ResponseChunkMessage,
    MessageType.RESPONSE_FINAL: ResponseFinalMessage,
    MessageType.ACTIVITY: ActivityMessage,
    MessageType.ERROR: ErrorMessage,
    MessageType.PONG: PongMessage,
}

# Convenience grouping for validation
ORCHESTRATOR_TO_AGENT_TYPES = frozenset({
    MessageType.ASSIGN_TASK,
    MessageType.CHAT_MESSAGE,
    MessageType.TOOL_RESPONSE,
    MessageType.CANCEL_TASK,
    MessageType.SHUTDOWN,
    MessageType.PING,
})

AGENT_TO_ORCHESTRATOR_TYPES = frozenset({
    MessageType.READY,
    MessageType.PROGRESS,
    MessageType.TOOL_CALL,
    MessageType.TASK_COMPLETE,
    MessageType.RESPONSE_CHUNK,
    MessageType.RESPONSE_FINAL,
    MessageType.ACTIVITY,
    MessageType.ERROR,
    MessageType.PONG,
})


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def serialize(msg: Any) -> str:
    """Serialize a message dataclass to a single NDJSON line (no trailing newline).

    Accepts dataclass instances or plain dicts. Uses compact JSON separators
    (no spaces) to minimize pipe traffic. The ``default=str`` fallback handles
    UUIDs, datetimes, and other non-JSON-native types gracefully.

    Args:
        msg: A dataclass instance or dict to serialize.

    Returns:
        A single-line JSON string (no trailing newline).

    Raises:
        TypeError: If msg is neither a dataclass nor a dict.
    """
    if hasattr(msg, "__dataclass_fields__"):
        data = asdict(msg)
    elif isinstance(msg, dict):
        data = msg
    else:
        raise TypeError(f"Cannot serialize {type(msg)}")
    return json.dumps(data, separators=(",", ":"), default=str)


def deserialize(line: str) -> dict[str, Any]:
    """Parse a single NDJSON line into a dict.

    Strips whitespace and newlines before parsing. Does NOT instantiate
    a dataclass -- returns a plain dict for routing flexibility. Callers
    can use MESSAGE_CLASSES[msg["type"]] to reconstruct typed objects
    if needed.

    Args:
        line: A single JSON line from stdin/stdout.

    Returns:
        Parsed dict with at least a "type" key.

    Raises:
        json.JSONDecodeError: If the line is not valid JSON.
    """
    return json.loads(line.strip())


def write_message(msg: Any, stream=None) -> None:
    """Write a message to a stream (default: stdout). Flushes immediately.

    Used by agent subprocesses to send messages to the Orchestrator.
    Synchronous -- safe to call from any context. The flush is critical:
    without it, Python's line buffering may delay delivery indefinitely.

    Args:
        msg: A dataclass instance or dict to write.
        stream: Output stream (default: sys.stdout).
    """
    stream = stream or sys.stdout
    stream.write(serialize(msg) + "\n")
    stream.flush()


async def read_messages(stream) -> Any:
    """Async generator that reads NDJSON messages from a stream.

    Used by the Orchestrator to read stdout from agent processes.
    Yields parsed dict for each line. Stops on EOF (process exited).
    Skips blank lines and malformed JSON (e.g., stray print statements
    from imported libraries).

    Args:
        stream: An asyncio StreamReader (from subprocess stdout).

    Yields:
        Parsed dict for each valid NDJSON line.
    """
    while True:
        line = await stream.readline()
        if not line:
            break  # EOF -- process exited
        decoded = line.decode().strip() if isinstance(line, bytes) else line.strip()
        if not decoded:
            continue
        try:
            yield deserialize(decoded)
        except json.JSONDecodeError:
            pass  # Skip malformed lines (e.g., stray print statements)
