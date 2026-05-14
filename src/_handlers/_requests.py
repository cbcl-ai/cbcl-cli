"""Request-bridge dispatch helper (split from handlers.py).

The backend's ``RequestBridge`` calls the communicator over the
connector WebSocket with a ``{"type": "request", "action": ..., ...}``
envelope. This module owns the dispatch table for those actions:

- ``fs_*`` → ``fs_handler.handle_request``
- ``mcp_list_query`` → return cached server list from Redis
- ``mcp_get_oauth_creds`` → read credentials.json inside the container
- ``auth_status`` / ``auth_start`` / ``auth_complete`` → Phase 2/3
  pre-flight + setup-wizard auth flow

Each branch eventually calls ``router.ws_client.send({"type":
"response", "request_id", "data"})`` so the backend's awaitable
gets resolved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess

logger = logging.getLogger(__name__)


async def dispatch_backend_request(
    message: dict,
    *,
    router,
    fs_handler,
    office,
    redis_client,
    container_name: str,
) -> None:
    """Route a request from the backend's RequestBridge to its handler."""
    action = message.get("action", "")
    request_id = message.get("request_id", "")

    if action.startswith("fs_"):
        await fs_handler.handle_request(message, router.ws_client.send)
        return

    if action == "mcp_list_query":
        cache_key = f"office:{office.id}:mcp_list"
        cached = (
            await redis_client.get(cache_key) if redis_client else None
        )
        servers = json.loads(cached) if cached else []
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {"servers": servers},
        })
        return

    if action == "mcp_get_oauth_creds":
        server_name = message.get("params", {}).get("name", "")
        creds_data: dict = {}
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "exec", container_name, "cat",
                 "/home/agent/.claude/.credentials.json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                all_creds = json.loads(result.stdout)
                mcp_oauth = all_creds.get("mcpOAuth", {})
                for key, entry in mcp_oauth.items():
                    if (
                        entry.get("serverName") == server_name
                        or server_name in key
                    ):
                        creds_data = {
                            "client_id": entry.get("clientId", ""),
                            "client_secret": entry.get("clientSecret", ""),
                            "auth_server_url": (
                                entry.get("discoveryState", {})
                                .get("authorizationServerUrl", "")
                            ),
                            "step_up_scope": entry.get("stepUpScope", ""),
                        }
                        break
        except Exception as exc:
            logger.warning("Failed to read MCP OAuth creds: %s", exc)
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": creds_data,
        })
        return

    if action == "auth_status":
        # Phase 2 pre-flight: container ``claude --print`` round-trip,
        # ~1-3 s but blocking on subprocess. Wrap in to_thread so the
        # WS reader keeps draining other messages.
        from src.auth_helpers import (
            get_auth_account_info,
            verify_claude_in_container,
        )

        authenticated = False
        account: str | None = None
        if container_name:
            authenticated = await asyncio.to_thread(
                verify_claude_in_container, container_name,
            )
            if authenticated:
                account = await asyncio.to_thread(
                    get_auth_account_info, container_name,
                )
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": {
                "authenticated": authenticated,
                "account": account,
                "container_name": container_name or None,
            },
        })
        return

    if action == "auth_start":
        # Phase 3: PKCE generation + URL build. Sync (no subprocess).
        from src.auth_service import start_auth_flow

        try:
            payload = await asyncio.to_thread(
                start_auth_flow, container_name or "",
            )
            response_data: dict = {**payload, "error": None}
        except ValueError as exc:
            response_data = {"error": str(exc)}
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": response_data,
        })
        return

    if action == "auth_complete":
        # Phase 3: exchange the user-pasted code for tokens and write
        # credentials.json into the container. ~30 s budget for the
        # round-trip + verify.
        from src.auth_service import complete_auth_flow

        params = message.get("params", {})
        session_id = params.get("session_id", "")
        raw_code = params.get("code", "")
        response_data = await asyncio.to_thread(
            complete_auth_flow, session_id, raw_code,
        )
        await router.ws_client.send({
            "type": "response",
            "request_id": request_id,
            "data": response_data,
        })
        return
