"""WebSocket reconnection and message queue management.

Handles exponential backoff, message queueing during disconnect,
replay on reconnect, and reconnect callback management.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

from src.connection.protocol import encode_message

logger = logging.getLogger(__name__)

# Type alias for async handler functions
Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

# Message types that should be sent fresh (not queued)
TIME_SENSITIVE_TYPES = frozenset({"health_report"})

# Maximum number of messages to queue during disconnect (configurable via env)
MAX_QUEUE_SIZE = int(os.environ.get("CBCL_MAX_QUEUE_SIZE", "100"))


class ReconnectManager:
    """Manages reconnection state, message queueing, and backoff."""

    def __init__(self, max_reconnect_delay: float = 60.0) -> None:
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = max_reconnect_delay
        self._has_connected_before = False
        self._disconnect_time: float | None = None
        self._message_queue: deque[dict[str, Any]] = deque(maxlen=MAX_QUEUE_SIZE)
        self._on_reconnect_callbacks: list[Handler] = []

    @property
    def disconnect_duration(self) -> float | None:
        """Seconds since the last disconnect, or None if no disconnect tracked."""
        if self._disconnect_time is None:
            return None
        return time.monotonic() - self._disconnect_time

    def on_reconnect(self, callback: Handler) -> None:
        """Register a callback to fire after successful reconnect."""
        self._on_reconnect_callbacks.append(callback)

    def reset_delay(self) -> None:
        """Reset reconnect delay after a successful connection."""
        self._reconnect_delay = 1.0

    def mark_connected(self) -> bool:
        """Record that we connected. Returns True if this is a reconnect."""
        was_reconnect = self._has_connected_before
        self._has_connected_before = True
        self._disconnect_time = None
        self._reconnect_delay = 1.0
        return was_reconnect

    def mark_disconnected(self, was_connected: bool) -> None:
        """Record disconnect state and timestamp."""
        if was_connected and self._disconnect_time is None:
            self._disconnect_time = time.monotonic()

    def queue_message(self, message: dict[str, Any]) -> None:
        """Queue a message for later replay. Skips time-sensitive types."""
        msg_type = message.get("type", "")
        if msg_type in TIME_SENSITIVE_TYPES:
            raise ConnectionError("Not connected to platform")
        if len(self._message_queue) >= MAX_QUEUE_SIZE:
            dropped = self._message_queue[0]
            logger.warning(
                "Message queue full (%d messages). Dropping oldest message "
                "(type=%s) to make room for new message (type=%s).",
                MAX_QUEUE_SIZE,
                dropped.get("type", "unknown"),
                msg_type,
            )
        self._message_queue.append(message)
        logger.debug(
            "Queued message (type=%s, queue_size=%d)",
            msg_type, len(self._message_queue),
        )

    async def replay_and_notify(self, ws) -> None:
        """Replay queued messages and fire reconnect callbacks.

        Parameters
        ----------
        ws:
            The websocket connection object with a ``.send()`` method.
        """
        # Replay queued messages
        replayed = 0
        while self._message_queue:
            queued_msg = self._message_queue.popleft()
            msg_type = queued_msg.get("type", "")
            if msg_type in TIME_SENSITIVE_TYPES:
                continue
            try:
                raw = encode_message(queued_msg)
                await ws.send(raw)
                replayed += 1
            except (OSError, ConnectionError, RuntimeError) as exc:
                logger.warning(
                    "Failed to replay queued message (type=%s): %s",
                    msg_type, exc,
                )
                break
        if replayed:
            logger.info("Replayed %d queued messages after reconnect", replayed)

        # Fire reconnect callbacks
        for callback in self._on_reconnect_callbacks:
            try:
                await callback({})
            except Exception as exc:
                logger.exception("Error in reconnect callback: %s", exc)

    def clear_queue(self) -> None:
        """Clear the message queue (e.g., on graceful disconnect)."""
        self._message_queue.clear()

    async def backoff_sleep(self) -> None:
        """Sleep with exponential backoff and jitter before reconnecting."""
        jittered_delay = self._reconnect_delay * (0.5 + random.random())
        logger.info("Reconnecting in %.1fs ...", jittered_delay)
        await asyncio.sleep(jittered_delay)
        self._reconnect_delay = min(
            self._reconnect_delay * 2, self._max_reconnect_delay,
        )
