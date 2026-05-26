"""Local HTTP server that proxies tool calls through the WebSocket connection.

Docker containers running MCP tool servers call this local proxy instead
of the remote backend directly. The proxy forwards tool calls as WS
request/response messages through the communicator's WebSocket connection.

This eliminates the need for Docker containers to have direct HTTP access
to the backend, which is required for remote deployment scenarios.

A second endpoint, ``/script-execute-host``, delegates the request to the
LOCAL host-side ``ScriptRunner`` instead of routing through the backend.
This is the path the in-container MCP uses for scripts that reference
office secrets via ``from_office_secret``: the host runner reads the
office-secrets file (which lives outside the container's bind-mounted
workspace) and injects values via ``docker exec -e KEY=VALUE`` — the
values never enter the container's filesystem or the backend.

Usage:
    server = ToolProxyServer(ws_client, port=9876, script_runner=runner)
    await server.start()   # Non-blocking, starts in background
    await server.stop()    # Graceful shutdown
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9876


class ToolProxyServer:
    """HTTP server that proxies tool calls through the WS connection.

    Accepts POST /tool-call from Docker containers (same format as
    the backend's /api/offices/{oid}/tool-call endpoint) and forwards
    them via PlatformWSClient.request() for WS-based request/response.

    Also accepts POST /script-execute-host for the host-runner
    delegation path described in the module docstring.
    """

    def __init__(
        self,
        ws_client: Any,  # PlatformWSClient
        port: int = DEFAULT_PORT,
        # Bind to 0.0.0.0 by default. On LINUX Docker (the prod
        # deployment), the agent container reaches the proxy via
        # ``host.docker.internal:host-gateway`` → the docker bridge
        # interface (typically ``172.17.0.1``), which is NOT
        # loopback. A 127.0.0.1-only proxy is unreachable from the
        # container — the TCP connect just hangs until
        # ConnectionTimeoutError (the symptom that surfaced in the
        # ESCALATED (external_outage) report after v0.2.23 made
        # host.docker.internal resolvable).
        #
        # On Docker Desktop (Mac/Windows), ``host.docker.internal``
        # routes to the host's loopback interface, so 127.0.0.1
        # used to work — but 0.0.0.0 works there too. Standardising
        # on 0.0.0.0 keeps the prod + dev paths identical.
        #
        # Threat model: the proxy hosts ``/script-execute-host``
        # which spawns ``docker exec`` with caller-controlled env.
        # 0.0.0.0 means any process on the cbcl host can hit the
        # endpoint. Defence: cbcl is intended to run on a
        # single-tenant machine the operator controls (their dev
        # box or a dedicated office host). The deployment guide
        # (``docs/handbook/01-architecture/deployment.md``) calls
        # this out. If you need stronger isolation, override
        # ``host=`` to the specific docker bridge IP (``172.17.0.1``
        # on default-bridge installs) so only containers on that
        # bridge can reach the proxy.
        host: str = "0.0.0.0",  # noqa: S104 — see threat model above
        script_runner: Any | None = None,  # ScriptRunner
        token: str | None = None,
    ) -> None:
        self._ws_client = ws_client
        self._port = port
        self._host = host
        self._script_runner = script_runner
        # Per-process random bearer token. Required on every POST
        # (``/tool-call`` AND ``/script-execute-host``). The supervisor
        # plumbs it into spawned agent containers via the
        # ``TOOL_PROXY_TOKEN`` env var; the in-container MCP server
        # sends it as ``Authorization: Bearer <token>`` on every call.
        # Together with the 0.0.0.0 bind (required so Linux Docker
        # containers can reach the host), this closes the gap where
        # any local process could exfiltrate office secrets by hitting
        # ``/script-execute-host`` directly. The token never leaves
        # the cbcl host (passed via env, not over the WS).
        self._token = token or secrets.token_urlsafe(32)
        self._app = web.Application()
        self._app.router.add_post("/tool-call", self._handle_tool_call)
        self._app.router.add_post(
            "/script-execute-host", self._handle_script_execute_host,
        )
        # /health is intentionally unauthenticated — operator probes
        # like ``curl localhost:.../health`` should work without the
        # token. It reveals nothing sensitive (only ws_connected).
        self._app.router.add_get("/health", self._handle_health)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def _check_auth(self, request: web.Request) -> bool:
        """Constant-time bearer-token compare against ``self._token``."""
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return secrets.compare_digest(header[7:], self._token)

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

    @property
    def token(self) -> str:
        """The bearer token required on POST endpoints. Plumbed into
        spawned agent containers as ``TOOL_PROXY_TOKEN``."""
        return self._token

    async def _handle_tool_call(self, request: web.Request) -> web.Response:
        """Handle POST /tool-call from Docker containers.

        Request body: {"action": "create_task", "params": {...}}
        Response body: {"result": {...}} or {"error": "..."}
        """
        if not self._check_auth(request):
            return web.json_response(
                {"error": "unauthorized"}, status=401,
            )
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

    async def _handle_script_execute_host(
        self, request: web.Request,
    ) -> web.Response:
        """Handle POST /script-execute-host from the in-container MCP.

        Delegates to the host-side :class:`ScriptRunner`, which has
        access to the office secrets file at
        ``~/.cubicle/office-secrets/<slug>.json``. The runner reads
        any office-secret references (from a variable BINDING set via
        the Variables UI, or — for legacy scripts — from a manifest
        ``from_office_secret`` field) at execute time and injects
        values via ``docker exec -e KEY=VALUE``. The values never
        enter the container's filesystem or the WS / backend
        pipeline.

        Request body::
          {
            "script_name": "...",
            "variable_overrides": {...},    # optional
            "task_id": "...",               # optional
            "workstream_short_code": "...", # optional (for output dir)
            "scope_readable_id": "..."      # optional
          }

        Response body::
          {"execution_id": "exec-..."}
            or
          {"error": "missing_office_secret", "missing": ["NAME", ...]}
            or
          {"error": "..."}  # other failures
        """
        if not self._check_auth(request):
            return web.json_response(
                {"error": "unauthorized"}, status=401,
            )
        if self._script_runner is None:
            return web.json_response(
                {"error": (
                    "Host-side ScriptRunner is not wired into the "
                    "tool proxy. Restart cbcl."
                )},
                status=503,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON body"}, status=400,
            )
        script_name = body.get("script_name")
        if not isinstance(script_name, str) or not script_name:
            return web.json_response(
                {"error": "Missing 'script_name'"}, status=400,
            )

        # Defer imports so the proxy module stays loadable in unit
        # tests that don't wire a ScriptRunner.
        from src.scripts.script_runner import (
            MissingOfficeSecretError,
            OfficeSecretsCorruptError,
        )

        try:
            exec_id = await self._script_runner.execute(
                script_name=script_name,
                variable_overrides=body.get("variable_overrides") or {},
                task_id=body.get("task_id"),
                triggered_by=body.get("triggered_by") or "agent",
                workstream_short_code=(
                    body.get("workstream_short_code") or None
                ),
                scope_readable_id=body.get("scope_readable_id") or None,
            )
            return web.json_response({"execution_id": exec_id})
        except MissingOfficeSecretError as exc:
            # The agent gets a typed shape it can pattern-match on.
            # ``missing`` is the list of office-secret names the user
            # must add via Settings → Security; the backend's
            # ``setup_office_secret`` action_request handles the
            # inbox UX for that case.
            return web.json_response(
                {
                    "error": "missing_office_secret",
                    "missing": exc.missing,
                    "message": str(exc),
                },
                status=409,
            )
        except OfficeSecretsCorruptError as exc:
            return web.json_response(
                {
                    "error": "office_secrets_corrupt",
                    # The base exception carries the description in
                    # ``str(exc)`` — kept as ``detail`` on the wire so
                    # the agent's pattern-match path stays stable.
                    "detail": str(exc),
                    "message": str(exc),
                },
                status=409,
            )
        except FileNotFoundError as exc:
            return web.json_response(
                {"error": "script_not_found", "message": str(exc)},
                status=404,
            )
        except Exception as exc:
            logger.exception(
                "Host script execute failed for %s", script_name,
            )
            return web.json_response(
                {"error": str(exc) or type(exc).__name__},
                status=500,
            )

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "ws_connected": self._ws_client.connected,
        })
