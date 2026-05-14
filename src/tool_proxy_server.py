"""Local HTTP server that proxies tool calls through the WebSocket connection.

Docker containers running MCP tool servers call this local proxy instead
of the remote backend directly. The proxy forwards tool calls as WS
request/response messages through the communicator's WebSocket connection.

This eliminates the need for Docker containers to have direct HTTP access
to the backend, which is required for remote deployment scenarios.

Usage:
    server = ToolProxyServer(ws_client, port=9876)
    await server.start()   # Non-blocking, starts in background
    await server.stop()    # Graceful shutdown
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9876


class ToolProxyServer:
    """HTTP server that proxies tool calls through the WS connection.

    Accepts POST /tool-call from Docker containers (same format as
    the backend's /api/offices/{oid}/tool-call endpoint) and forwards
    them via PlatformWSClient.request() for WS-based request/response.
    """

    def __init__(
        self,
        ws_client: Any,  # PlatformWSClient
        port: int = DEFAULT_PORT,
        host: str = "0.0.0.0",
    ) -> None:
        self._ws_client = ws_client
        self._port = port
        self._host = host
        self._app = web.Application()
        self._app.router.add_post("/tool-call", self._handle_tool_call)
        self._app.router.add_get("/health", self._handle_health)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """Start the HTTP server (non-blocking).

        If ``port`` was 0, the OS assigns a free port. The actual port
        is available via the ``port`` property after ``start()`` returns.
        """
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        # Read back the actual bound port (important when port=0)
        if self._site._server and self._site._server.sockets:
            self._port = self._site._server.sockets[0].getsockname()[1]
        logger.info(
            "Tool proxy server started on http://%s:%d", self._host, self._port
        )

    async def stop(self) -> None:
        """Stop the HTTP server gracefully."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        logger.info("Tool proxy server stopped")

    @property
    def port(self) -> int:
        """The actual bound port (may differ from constructor arg if port=0)."""
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def _handle_tool_call(self, request: web.Request) -> web.Response:
        """Handle POST /tool-call from Docker containers.

        Request body: {"action": "create_task", "params": {...}}
        Response body: {"result": {...}} or {"error": "..."}
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON body"}, status=400
            )

        action = body.get("action")
        params = body.get("params", {})

        if not action:
            return web.json_response(
                {"error": "Missing 'action' field"}, status=400
            )

        if not self._ws_client.connected:
            return web.json_response(
                {"error": "WebSocket not connected to backend"}, status=503
            )

        try:
            result = await self._ws_client.request(
                action=action,
                params=params,
                timeout=30.0,
            )
            return web.json_response(result)
        except TimeoutError:
            logger.warning(
                "Tool call %s timed out (30s)", action
            )
            return web.json_response(
                {"error": f"Tool call '{action}' timed out"}, status=504
            )
        except Exception as exc:
            logger.exception("Tool proxy error for action %s", action)
            return web.json_response(
                {"error": str(exc)}, status=500
            )

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "ws_connected": self._ws_client.connected,
        })
