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
workspace) and injects values via ``docker exec -e KEY`` (name-only;
value supplied in the client's env so it never appears in host ``ps`` —
NEW-4) — the values never enter the container's filesystem or the backend.

Usage:
    server = ToolProxyServer(ws_client, port=9876, script_runner=runner)
    await server.start()   # Non-blocking, starts in background
    await server.stop()    # Graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9876

# The data_* actions scripts may reach via POST /collections/rpc
# (spec ui-ux-aug19 D4.1). ``data_import`` joined the whitelist on
# 2026-08-21 (script-lane completion #1): bulk CSV append from script
# code — the SDK's ``cubicle.collections.import_csv`` — rides the
# same lane. The datastore's own caps bound it (2 MB CSV / 5000
# rows, ``datastore.IMPORT_MAX_CSV_CHARS`` / ``IMPORT_MAX_ROWS``),
# and the 4 MB body cap below sits comfortably above the CSV cap.
_COLLECTIONS_RPC_ACTIONS = frozenset({
    "data_rows_list",
    "data_row_get",
    "data_row_upsert",
    "data_row_delete",
    "data_rows_count",
    "data_import",
})

# Body cap for /collections/rpc — generous headroom over realistic
# payloads. NOTE the datastore does NOT cap row-data size (text
# fields are unbounded; the only size caps in datastore.py are the
# CSV-import caps, and ``data_import`` doesn't ride this route), so
# this cap is the sole bound on a script's upsert body.
_COLLECTIONS_RPC_MAX_BODY_BYTES = 4 * 1024 * 1024

# Trailing debounce for the ``collection_rows_changed`` daemon→backend
# event (D4.6): a tight script write loop emits one frame per window
# instead of one per row.
_ROWS_CHANGED_DEBOUNCE_SECONDS = 2.0


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
        # (``docs/06-operations/deployment.md``) calls
        # this out. If you need stronger isolation on a SHARED host,
        # set ``CUBICLE_TOOL_PROXY_BIND`` to the specific docker bridge
        # IP (``172.17.0.1`` on default-bridge installs) so only
        # containers on that bridge — not every local process — can
        # reach the secret-injecting ``/script-execute-host`` endpoint
        # (TOOL-05). The default stays ``0.0.0.0`` because it's the only
        # value that works across Linux Docker + Docker Desktop + custom
        # bridges without per-host detection; the env override is the
        # opt-in hardening knob for multi-tenant boxes.
        host: str | None = None,
        script_runner: Any | None = None,  # ScriptRunner
        token: str | None = None,
    ) -> None:
        self._ws_client = ws_client
        self._port = port
        # Explicit arg wins; else the env knob; else the 0.0.0.0 default.
        self._host = host or os.environ.get(
            "CUBICLE_TOOL_PROXY_BIND", "0.0.0.0"
        )  # noqa: S104 — see threat model above
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
        # SECOND, NARROW bearer token (spec ui-ux-aug19 D4.2):
        # accepted ONLY on ``/collections/rpc``. Script subprocesses
        # get this one (as ``CUBICLE_COLLECTIONS_TOKEN``) instead of
        # the main token, so arbitrary script code + its pip deps can
        # never reach ``/tool-call`` (self-asserted ``_caller`` role
        # gates) or ``/script-execute-host`` (office-secret
        # injection). The main token stays valid on the collections
        # route so agent-side callers need no second credential.
        self._collections_token = secrets.token_urlsafe(32)
        # PER-EXECUTION collections tokens (script-lane completion #2,
        # 2026-08-21): the host ScriptRunner mints ONE token per
        # launched run, registers it here, injects it as that run's
        # ``CUBICLE_COLLECTIONS_TOKEN``, and revokes it on every
        # terminal path — so a script env that outlives its run (a
        # leaked child process, a copied env) loses collections
        # access the moment the execution ends. The office-narrow
        # ``_collections_token`` above STAYS valid on
        # ``/collections/rpc``: the in-container agent-triggered
        # script path (``_mcp_script_exec``) runs scripts with the
        # office-narrow token from the agent session env and never
        # sees host-side per-exec minting — that split is deliberate.
        self._exec_collections_tokens: set[str] = set()
        # Back-wire the per-exec token registry into the host
        # ScriptRunner. handlers.py already constructs the proxy WITH
        # ``script_runner=...``, so this needs no new wiring call
        # site there; hasattr-guarded for callers that pass runner
        # stand-ins without the setter.
        if script_runner is not None and hasattr(
            script_runner, "set_collections_token_registry"
        ):
            script_runner.set_collections_token_registry(self)
        # Office-local collections datastore, wired post-construction
        # via :meth:`set_datastore` (the ``script_runner`` pattern —
        # the datastore is built AFTER the proxy in handlers.py).
        self._datastore: Any | None = None
        # D4.6 debounce state: latest reported row_count per
        # collection + the pending trailing-send task.
        self._rows_changed_pending: dict[str, int] = {}
        self._rows_changed_tasks: dict[str, asyncio.Task] = {}
        self._rows_changed_debounce = _ROWS_CHANGED_DEBOUNCE_SECONDS
        self._app = web.Application()
        self._app.router.add_post("/tool-call", self._handle_tool_call)
        self._app.router.add_post(
            "/script-execute-host", self._handle_script_execute_host,
        )
        # /script-status forwards completion events from the
        # in-container MCP runner (``_mcp_script_exec``) up to the
        # backend via the same WS the host-side ScriptRunner uses.
        # Without this, agent-triggered scripts that ran via the
        # in-container path (the common case: no office_secret
        # refs) never landed a ScriptExecution row in the DB — the
        # Execution History panel stayed empty for everything
        # except UI-Run-triggered and office-secret-bearing runs.
        self._app.router.add_post(
            "/script-status", self._handle_script_status,
        )
        # /outbox-scan triggers a one-shot ``scan_and_dispatch`` of
        # a script's ``.outbox/`` directory — required because the
        # in-container MCP runner doesn't go through the host-side
        # monitor loop (which is what triggers outbox scans for
        # UI/cron/host-runner runs). Without this nudge, agent-
        # triggered in-container runs that call
        # ``cubicle.notify_manager()`` would land payloads in
        # ``.outbox/`` that nobody ever dispatches.
        self._app.router.add_post(
            "/outbox-scan", self._handle_outbox_scan,
        )
        # /collections/rpc is the SCRIPT lane into the office-local
        # collections datastore (spec ui-ux-aug19 Item 4): same
        # ``data_*`` envelope as the RequestBridge actions
        # (ws-protocol.md §3.5), served locally — no backend
        # round-trip, rows never leave the host.
        self._app.router.add_post(
            "/collections/rpc", self._handle_collections_rpc,
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

    def _check_collections_auth(self, request: web.Request) -> bool:
        """Auth for ``/collections/rpc`` ONLY: the narrow collections
        token, the main token (D4.2), or a LIVE per-execution token
        (script-lane completion #2). Every other route keeps the
        main-token-only :meth:`_check_auth`."""
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        presented = header[7:]
        # Evaluate every compare (no short-circuit) so timing doesn't
        # reveal WHICH token matched; each individual compare is
        # constant-time.
        is_narrow = secrets.compare_digest(presented, self._collections_token)
        is_main = secrets.compare_digest(presented, self._token)
        is_exec = False
        for exec_token in tuple(self._exec_collections_tokens):
            if secrets.compare_digest(presented, exec_token):
                is_exec = True
        return is_narrow or is_main or is_exec

    def set_datastore(self, datastore: Any) -> None:
        """Wire the office-local :class:`OfficeDatastore` after
        construction (it is built later in the office init order —
        the ``script_runner`` / ``set_tool_proxy`` pattern).
        ``/collections/rpc`` returns 503 until this runs."""
        self._datastore = datastore

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
        for task in list(self._rows_changed_tasks.values()):
            task.cancel()
        self._rows_changed_tasks.clear()
        self._rows_changed_pending.clear()
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

    @property
    def collections_token(self) -> str:
        """The NARROW bearer token valid ONLY on ``/collections/rpc``.
        Plumbed into script subprocesses as
        ``CUBICLE_COLLECTIONS_TOKEN`` (D4.2/D4.3).

        Since script-lane completion #2 (2026-08-21) this token is
        the AGENT-SIDE credential: the supervisor threads it into
        agent sessions, whose in-container script runs
        (``_mcp_script_exec``) inject it. HOST-launched runs get a
        per-execution token instead (:meth:`register_exec_collections_token`).
        """
        return self._collections_token

    def register_exec_collections_token(self, token: str) -> None:
        """Accept ``token`` on ``/collections/rpc`` until revoked
        (script-lane completion #2, 2026-08-21).

        Called by the host ScriptRunner at launch: each execution
        gets its OWN collections credential, revoked at every
        terminal path, so collections access is scoped to the run's
        lifetime instead of the daemon's. Grants nothing on any
        other route (``/tool-call`` / ``/script-execute-host`` keep
        the main-token-only check).
        """
        if token:
            self._exec_collections_tokens.add(token)

    def revoke_exec_collections_token(self, token: str) -> None:
        """Stop accepting a per-execution collections token.

        Idempotent — revoking an unknown/already-revoked token is a
        no-op, so double completion paths are safe. A daemon restart
        drops the whole in-memory set with the process (matching the
        standing tokens, which are re-minted per proxy instance).
        """
        self._exec_collections_tokens.discard(token)

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

        # Preserve the daemon-attested ``_caller`` identity the in-container
        # MCP stamps on the body. The WS relay (``ws_client.request``) only
        # carries ``action`` + ``params``, so fold ``_caller`` INTO params —
        # otherwise the backend's ``resolve_effective_actor`` sees an empty
        # actor and fail-closed role gates (e.g. complete_scope_verification,
        # which carries no ``actor`` field) reject the call with
        # ``actor='(none)'``. The direct-HTTP path preserves it via the
        # /tool-call route; this is the matching fix for the proxy→WS path.
        caller = body.get("_caller")
        if caller and isinstance(params, dict) and "_caller" not in params:
            params = {**params, "_caller": caller}

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
        values via ``docker exec -e KEY`` (name-only; value in the
        client's env — NEW-4). The values never
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

    async def _handle_script_status(
        self, request: web.Request,
    ) -> web.Response:
        """Forward a ``script_status`` event from the in-container
        MCP runner up to the backend.

        The in-container path (``_mcp_script_exec._execute_script``
        / ``_monitor_script``) writes its own ``status.json`` on the
        bind-mounted workspace, but in split-host production the
        backend has no filesystem access to that volume — so a row
        only appears in the Execution History DB table when an
        explicit ``script_status`` event reaches the backend's
        ``handle_script_status`` handler (which calls
        ``store_script_execution`` on terminal states).

        Mirrors the host-side ``script_notifier.notify_completion``
        WS publish shape so the backend handler accepts both
        sources uniformly. Best-effort: a WS disconnection drops
        the notification but the script row still lives on disk;
        the next reconnect's startup sync OR a future ``cbcl
        backfill`` job can recover it from disk.

        Request body (same shape ``script_notifier`` sends)::
          {
            "script_name": "...",
            "execution_id": "exec-...",
            "status": "completed" | "failed" | "running",
            "task_id": "..." | null,
            "cron_id": "..." | null,
            "triggered_by": "agent-name" | "user" | "cron:name",
            "started_at": "ISO8601",
            "completed_at": "ISO8601" | null,
            "duration_seconds": <int> | null,
            "error_message": "..." | null,
            "progress": {...} | null
          }

        Response: ``{"status": "queued"}`` on success.
        """
        if not self._check_auth(request):
            return web.json_response(
                {"error": "unauthorized"}, status=401,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON body"}, status=400,
            )
        # Light validation — the backend handler is the strict
        # arbiter, but reject obviously-malformed frames here so
        # the WS isn't spammed with nonsense.
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "body must be a JSON object"}, status=400,
            )
        script_name = body.get("script_name")
        execution_id = body.get("execution_id")
        status = body.get("status")
        if not all(
            isinstance(x, str) and x
            for x in (script_name, execution_id, status)
        ):
            return web.json_response(
                {"error": "script_name, execution_id, status required"},
                status=400,
            )
        if not self._ws_client.connected:
            # The DB write is the user-visible signal so we DO
            # need the WS up. Surface the gap so the caller can
            # log it (the script still completed on disk).
            return web.json_response(
                {"error": "WebSocket not connected to backend"},
                status=503,
            )
        try:
            # ``send`` is fire-and-forget; the WS client serialises
            # internally so concurrent sends are safe. We don't
            # await any backend ack here — script_status is a
            # one-way notification.
            await self._ws_client.send(
                {"type": "script_status", **body},
            )
            return web.json_response({"status": "queued"})
        except Exception as exc:
            logger.exception(
                "Failed to forward script_status for %s/%s",
                script_name, execution_id,
            )
            return web.json_response(
                {"error": str(exc) or type(exc).__name__},
                status=500,
            )

    async def _handle_outbox_scan(
        self, request: web.Request,
    ) -> web.Response:
        """Trigger a one-shot outbox scan for a single script.

        Called by ``_mcp_script_exec._monitor_script`` after the
        in-container subprocess exits. Without this, agent-triggered
        ``execute_script`` runs that call ``cubicle.notify_manager()``
        would leave the JSON drop in ``.outbox/`` forever — the
        host-side monitor loop only knows about scripts spawned via
        the host runner.

        Request body::
          {"script_name": "..."}

        Response: ``{"dispatched": <int>}`` — number of notify
        files routed to the Manager.
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
        script_name = body.get("script_name") if isinstance(body, dict) else None
        if not isinstance(script_name, str) or not script_name:
            return web.json_response(
                {"error": "script_name required"}, status=400,
            )
        try:
            dispatched = await self._script_runner.scan_outbox_for(
                script_name,
            )
            return web.json_response({"dispatched": dispatched})
        except Exception as exc:
            logger.exception(
                "outbox scan failed for %s", script_name,
            )
            return web.json_response(
                {"error": str(exc) or type(exc).__name__},
                status=500,
            )

    async def _handle_collections_rpc(
        self, request: web.Request,
    ) -> web.Response:
        """Handle POST /collections/rpc — the script SDK's lane into
        the office-local collections datastore (spec ui-ux-aug19
        Item 4, D4.1).

        Request body is the existing ``data_*`` envelope
        (ws-protocol.md §3.5 param shapes, no third shape)::

          {"action": "data_rows_list", "params": {"collection": "..."}}

        Whitelisted actions: ``data_rows_list`` / ``data_row_get`` /
        ``data_row_upsert`` / ``data_row_delete`` / ``data_rows_count``
        / ``data_import`` (CSV append — the datastore's 2 MB /
        5000-row caps apply; script-lane completion #1). Success returns
        the datastore's per-action dict verbatim; a
        :class:`~src.datastore.DatastoreError` maps to
        ``{"error": ...}`` with its status.

        Auth: narrow collections token OR the main proxy token
        (:meth:`_check_collections_auth`).
        """
        if not self._check_collections_auth(request):
            return web.json_response(
                {"error": "unauthorized"}, status=401,
            )
        if self._datastore is None:
            return web.json_response(
                {"error": (
                    "Collections datastore is not wired into the "
                    "tool proxy. Restart cbcl."
                )},
                status=503,
            )
        if (
            request.content_length is not None
            and request.content_length > _COLLECTIONS_RPC_MAX_BODY_BYTES
        ):
            return web.json_response(
                {"error": "request body too large"}, status=413,
            )
        # Re-enforce the cap while reading — a chunked body carries
        # no Content-Length, so the header check alone is spoofable.
        # Accumulate via readany(): StreamReader.read(n) returns only
        # what is ALREADY buffered (not n bytes, not EOF), so a large
        # body spanning multiple feed events would be truncated and a
        # valid request would 400 on the partial JSON.
        raw = b""
        while True:
            chunk = await request.content.readany()
            if not chunk:
                break
            raw += chunk
            if len(raw) > _COLLECTIONS_RPC_MAX_BODY_BYTES:
                return web.json_response(
                    {"error": "request body too large"}, status=413,
                )
        try:
            body = json.loads(raw)
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON body"}, status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "body must be a JSON object"}, status=400,
            )
        action = body.get("action")
        if action not in _COLLECTIONS_RPC_ACTIONS:
            return web.json_response(
                {"error": (
                    f"unknown collections action {action!r} — one of "
                    f"{sorted(_COLLECTIONS_RPC_ACTIONS)} required"
                )},
                status=400,
            )
        params = body.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return web.json_response(
                {"error": "'params' must be a JSON object"}, status=400,
            )

        from src.datastore import DatastoreError

        try:
            result = await self._datastore.dispatch(action, params)
        except DatastoreError as exc:
            return web.json_response(
                {"error": str(exc)}, status=exc.status,
            )
        except Exception as exc:
            logger.exception(
                "Collections RPC failed for action %s", action,
            )
            return web.json_response(
                {"error": str(exc) or type(exc).__name__}, status=500,
            )

        if action in ("data_row_upsert", "data_row_delete", "data_import"):
            self._schedule_rows_changed(
                str(params.get("collection") or ""),
                result.get("row_count") if isinstance(result, dict) else None,
            )
        return web.json_response(result)

    def _schedule_rows_changed(
        self, collection: str, row_count: Any,
    ) -> None:
        """Debounced ``collection_rows_changed`` daemon→backend event
        (D4.6): stash the latest count and arm ONE trailing-send task
        per collection, so a tight script write loop emits at most one
        frame per debounce window. Best-effort end to end — a send
        failure never affects the RPC response (the ~30s health
        heartbeat remains the backstop)."""
        if not collection or not isinstance(row_count, int):
            return
        self._rows_changed_pending[collection] = row_count
        existing = self._rows_changed_tasks.get(collection)
        if existing is not None and not existing.done():
            return
        self._rows_changed_tasks[collection] = asyncio.create_task(
            self._flush_rows_changed(collection),
        )

    async def _flush_rows_changed(self, collection: str) -> None:
        try:
            await asyncio.sleep(self._rows_changed_debounce)
            row_count = self._rows_changed_pending.pop(collection, None)
            if row_count is None:
                return
            if not self._ws_client.connected:
                return
            await self._ws_client.send({
                "type": "collection_rows_changed",
                "collection": collection,
                "row_count": row_count,
            })
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "collection_rows_changed send failed for %s",
                collection,
                exc_info=True,
            )
        finally:
            self._rows_changed_tasks.pop(collection, None)
            # A mutation that landed while ``send`` was in flight
            # stashed a fresh count but found this task "not done"
            # and didn't arm a new one — re-arm here so it isn't
            # stranded until the next write.
            if collection in self._rows_changed_pending:
                self._rows_changed_tasks[collection] = asyncio.create_task(
                    self._flush_rows_changed(collection),
                )

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "ws_connected": self._ws_client.connected,
        })
