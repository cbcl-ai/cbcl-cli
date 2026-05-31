"""WebSocket client for connecting to the Cubicle platform backend.

Connects to ws://<platform>/ws/connector/<office_id> and handles:
- Auto-reconnect with exponential backoff (via ReconnectManager)
- Message dispatch to registered handlers
- Request/response pattern for MCP tool queries
- Ping/pong heartbeat
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidStatus, WebSocketException

from src.connection.protocol import (
    MSG_PING,
    MSG_PONG,
    MSG_RESPONSE,
    decode_message,
    encode_message,
)
from src.connection.ws_reconnect import ReconnectManager

logger = logging.getLogger(__name__)

# Type alias for async handler functions
Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class PlatformWSClient:
    """Async WebSocket client connecting to the platform backend."""

    def __init__(
        self,
        platform_url: str,
        office_id: str,
        security_token: str | None = None,
    ) -> None:
        # Convert http(s):// to ws(s)://
        ws_url = platform_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        base_url = f"{ws_url}/ws/connector/{office_id}"
        self.url = (
            f"{base_url}?token={security_token}" if security_token else base_url
        )
        self.office_id = office_id
        self._ws: ClientConnection | None = None
        self._connected = False
        self._handlers: dict[str, list[Handler]] = {}
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._should_run = False
        self._reconnect = ReconnectManager()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def disconnect_duration(self) -> float | None:
        """Seconds since the last disconnect, or None if connected."""
        if self._connected:
            return None
        return self._reconnect.disconnect_duration

    def on(self, message_type: str, handler: Handler) -> None:
        """Register a handler for a message type."""
        if message_type not in self._handlers:
            self._handlers[message_type] = []
        self._handlers[message_type].append(handler)

    def on_reconnect(self, callback: Handler) -> None:
        """Register a callback to fire after successful reconnect."""
        self._reconnect.on_reconnect(callback)

    async def connect(self) -> None:
        """Connect to the platform and start listening.

        Blocks until disconnect() is called or connection fails permanently.
        Auto-reconnects on transient failures.
        """
        self._should_run = True
        self._reconnect.reset_delay()

        while self._should_run:
            try:
                logger.info("Connecting to %s ...", self.url.split("?")[0])
                # max_size raised to 16 MiB so backend-pushed file chunks
                # (1 MiB raw → ~1.37 MiB base64 + JSON envelope) don't
                # exceed the default 1 MiB frame limit and silently drop
                # the connection with code 1006. Matches uvicorn's
                # ws_max_size default on the backend side.
                #
                # ``open_timeout`` is set explicitly so the handshake
                # never hangs indefinitely. Without it, a backend that
                # accepts the TCP but stalls on the WS upgrade (e.g.
                # mid-uvicorn-reload window during dev) would block the
                # reconnect loop forever and leave the daemon thinking
                # it's still trying to connect. 10s is the websockets
                # library default — we just make it explicit so the
                # contract is visible at the call site.
                self._ws = await websockets.connect(
                    self.url,
                    max_size=16 * 1024 * 1024,
                    open_timeout=10,
                )
                self._connected = True

                was_reconnect = self._reconnect.mark_connected()
                if was_reconnect:
                    logger.info(
                        "Reconnected to platform (office %s)", self.office_id,
                    )
                    await self._reconnect.replay_and_notify(self._ws)
                else:
                    logger.info(
                        "Connected to platform (office %s)", self.office_id,
                    )

                await self._listen_loop()
            except websockets.ConnectionClosed as exc:
                self._mark_disconnected()
                if not self._should_run:
                    logger.info("Connection closed (shutting down)")
                    break
                logger.warning(
                    "Connection closed (code=%s, reason=%s), reconnecting...",
                    exc.code, exc.reason,
                )
            except InvalidStatus as exc:
                self._mark_disconnected()
                if not self._should_run:
                    break
                logger.warning(
                    "Connection rejected (HTTP %s), reconnecting...",
                    exc.response.status_code,
                )
            except (OSError, ConnectionRefusedError) as exc:
                self._mark_disconnected()
                if not self._should_run:
                    break
                logger.warning("Connection failed: %s", exc)
            except (asyncio.TimeoutError, RuntimeError) as exc:
                self._mark_disconnected()
                if not self._should_run:
                    break
                logger.warning("Connection error: %s", exc)
            except WebSocketException as exc:
                # Catch-all for every other websockets-library exception
                # type: InvalidHandshake, InvalidUpgrade, InvalidHeader,
                # InvalidMessage, ProtocolError, PayloadTooBig, etc. Pre-
                # iter-5 these would propagate out of ``connect()`` and
                # silently kill the reconnect task, leaving the daemon
                # in a stuck state ("Failed to publish health" forever).
                # We log, mark disconnected, and let the backoff retry —
                # same posture as the explicit cases above.
                self._mark_disconnected()
                if not self._should_run:
                    break
                logger.warning(
                    "WebSocket protocol error: %s: %s",
                    type(exc).__name__, exc,
                )
            except Exception as exc:
                # Last-resort safety net. Anything else (SSL hiccup,
                # unexpected library bug) should NOT permanently break
                # the reconnect loop. Log at error so the operator sees
                # it, then fall through to the backoff sleep.
                self._mark_disconnected()
                if not self._should_run:
                    break
                logger.exception(
                    "Unexpected exception in connect loop: %s: %s",
                    type(exc).__name__, exc,
                )

            if self._should_run:
                await self._reconnect.backoff_sleep()

    def _mark_disconnected(self) -> None:
        """Record disconnect state."""
        was_connected = self._connected
        self._connected = False
        self._ws = None
        self._reconnect.mark_disconnected(was_connected)
        # Fail in-flight RPC futures fast on a transient drop instead of
        # letting them hang until their 30s timeout — a caller (e.g. the
        # tool proxy's /tool-call) should learn the channel is gone right
        # away and surface it. Mirrors the graceful disconnect() path; the
        # request() finally-block pops its own id, so clearing here is safe.
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

    async def disconnect(self) -> None:
        """Graceful disconnect."""
        self._should_run = False
        if self._ws:
            try:
                await self._ws.close()
            except (OSError, RuntimeError, asyncio.TimeoutError):
                pass
        self._connected = False
        self._ws = None
        self._reconnect.clear_queue()
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()
        logger.info("Disconnected from platform")

    async def send(self, message: dict[str, Any]) -> None:
        """Send a JSON message to the platform.

        When disconnected, non-time-sensitive messages are queued
        and replayed on reconnect.
        """
        if not self._ws or not self._connected:
            self._reconnect.queue_message(message)
            return
        raw = encode_message(message)
        await self._ws.send(raw)

    async def safe_send(
        self, message: dict[str, Any], context: str = "",
    ) -> bool:
        """Send a message, suppressing WS transport errors.

        Returns True on success, False on failure.
        """
        try:
            await self.send(message)
            return True
        except (OSError, ConnectionError, RuntimeError) as exc:
            if context:
                logger.debug("WS send failed (%s): %s", context, exc)
            else:
                logger.debug("WS send failed: %s", exc)
            return False

    async def request(
        self, action: str, params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a request and wait for a matching response.

        Returns the response data dict.
        Raises TimeoutError if no response within timeout.
        """
        request_id = str(uuid.uuid4())
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future

        try:
            await self.send({
                "type": "request",
                "request_id": request_id,
                "action": action,
                "params": params or {},
            })
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Request '{action}' timed out after {timeout}s"
            )
        finally:
            self._pending_requests.pop(request_id, None)

    async def _listen_loop(self) -> None:
        """Receive messages and dispatch to handlers."""
        if not self._ws:
            return

        async for raw in self._ws:
            if not self._should_run:
                break

            try:
                message = decode_message(raw)
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "Failed to decode message: %s (error: %s)",
                    raw[:200], exc,
                )
                continue

            msg_type = message.get("type", "")

            # Handle pings inline
            if msg_type == MSG_PING:
                try:
                    await self.send({"type": MSG_PONG})
                except (OSError, ConnectionError, RuntimeError) as exc:
                    logger.warning("Failed to send pong: %s", exc)
                continue

            # Handle responses for request/response pattern
            if msg_type == MSG_RESPONSE:
                request_id = message.get("request_id")
                if request_id and request_id in self._pending_requests:
                    future = self._pending_requests[request_id]
                    if not future.done():
                        future.set_result(message.get("data", {}))
                continue

            # Handle action_result (response to manager_action)
            if msg_type == "action_result":
                request_id = message.get("request_id")
                if request_id and request_id in self._pending_requests:
                    future = self._pending_requests[request_id]
                    if not future.done():
                        future.set_result(message)
                await self._dispatch(msg_type, message)
                continue

            await self._dispatch(msg_type, message)

    async def _dispatch(
        self, msg_type: str, message: dict[str, Any],
    ) -> None:
        """Dispatch a message to all registered handlers."""
        handlers = self._handlers.get(msg_type, [])
        if not handlers:
            logger.warning("No handler for message type: %s", msg_type)
            return

        for handler in handlers:
            asyncio.create_task(self._run_handler(handler, msg_type, message))

    @staticmethod
    async def _run_handler(
        handler: Handler, msg_type: str, message: dict[str, Any],
    ) -> None:
        """Run a single handler with error catching."""
        try:
            await handler(message)
        except Exception as exc:
            logger.exception(
                "Error in handler for message type '%s': %s",
                msg_type, exc,
            )
