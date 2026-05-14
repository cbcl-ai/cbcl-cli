"""WebSocket transport — wraps PlatformWSClient for direct communication.

Delegates to PlatformWSClient, which opens an outbound WSS connection
to the backend and handles auto-reconnection, message queuing, and
request/response correlation.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from src.connection.ws_client import PlatformWSClient

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class WsTransport:
    """TransportClient backed by direct WebSocket connection."""

    def __init__(
        self,
        platform_url: str,
        office_id: str,
        security_token: str | None = None,
    ) -> None:
        self._client = PlatformWSClient(
            platform_url=platform_url,
            office_id=office_id,
            security_token=security_token,
        )
        self._office_id = office_id

    def on(self, message_type: str, handler: Handler) -> None:
        self._client.on(message_type, handler)

    async def publish_event(self, event: dict[str, Any]) -> None:
        """Send an event to the backend over WebSocket.

        Enriches the event with message_uuid and published_at metadata
        to match what the backend's EventDispatcher expects for idempotency.
        """
        enriched = {
            **event,
            "message_uuid": event.get("message_uuid", uuid.uuid4().hex),
            "published_at": event.get("published_at", time.time()),
        }
        await self._client.send(enriched)

    async def start(self) -> None:
        """Connect to the backend and start listening (blocks)."""
        await self._client.connect()

    async def stop(self) -> None:
        """Disconnect gracefully."""
        await self._client.disconnect()

    @property
    def is_running(self) -> bool:
        return self._client.connected

    @property
    def ws_client(self) -> PlatformWSClient:
        """Expose the underlying WS client for request/response operations."""
        return self._client
