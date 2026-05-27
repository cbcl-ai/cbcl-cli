"""Backend HTTP proxy for the in-container MCP tool server.

Extracted from ``mcp_tool_server.py`` to keep that file focused on
JSON-RPC dispatch + role/lock guards. Owns the singleton aiohttp
session and the proxy-then-direct retry path that every backend-
backed tool call goes through.

The module reads its config (BACKEND_URL, TOOL_PROXY_URL,
TOOL_PROXY_TOKEN, OFFICE_ID, AGENT_NAME) from os.environ at import
time, matching the parent module's pattern. Identical values come
out — both modules are imported once per agent process and the env
is fixed at process start.
"""

from __future__ import annotations

import asyncio
import os

# Module-level config — same env vars the parent reads.
BACKEND_URL = os.environ.get("BACKEND_URL", "http://host.docker.internal:8000")
TOOL_PROXY_URL = os.environ.get("TOOL_PROXY_URL", "")
TOOL_PROXY_TOKEN = os.environ.get("TOOL_PROXY_TOKEN", "")
OFFICE_ID = os.environ.get("OFFICE_ID", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "")

# Singleton aiohttp session. Created lazily on first call so the
# import path stays cheap (agent spawn-time matters).
_http_session = None


async def _get_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=30, connect=5)
        connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=30)
        _http_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"Content-Type": "application/json"},
        )
    return _http_session


async def _close_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
    _http_session = None


async def _call_backend(action: str, params: dict) -> dict:
    """Call the backend tool-call endpoint with retry logic.

    If TOOL_PROXY_URL is set, routes through the local communicator proxy
    (which forwards via WebSocket). Falls back to direct backend HTTP if
    the proxy is unavailable.
    """
    import aiohttp

    # Always carry the caller's identity through to the backend so
    # the dispatcher can apply defense-in-depth role gates (the
    # in-container tool-list filter is the primary defense; this
    # is the backstop for ASD-only actions like
    # ``bind_script_variable`` / ``install_script_from_template``
    # so a misbehaving call path that bypasses the filter can't
    # rebind someone else's script). Sent as a top-level envelope
    # field so handlers can read it without changing every
    # tool's params schema.
    payload = {
        "action": action,
        "params": params,
        "_caller": {
            "agent_name": AGENT_NAME or "",
            "role": "worker" if AGENT_NAME else "manager",
        },
    }
    session = await _get_session()
    last_error = None

    # Try local proxy first (WS-routed, lower latency for remote setups)
    if TOOL_PROXY_URL:
        proxy_url = f"{TOOL_PROXY_URL}/tool-call"
        proxy_headers = (
            {"Authorization": f"Bearer {TOOL_PROXY_TOKEN}"}
            if TOOL_PROXY_TOKEN
            else None
        )
        try:
            async with session.post(
                proxy_url, json=payload, headers=proxy_headers,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                # Proxy returned an error — fall through to direct backend
                body = await resp.text()
                last_error = f"Proxy HTTP {resp.status}: {body[:300]}"
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError):
            last_error = "Tool proxy unreachable, falling back to direct backend"

    # Direct backend call (original path)
    url = f"{BACKEND_URL}/api/offices/{OFFICE_ID}/tool-call"
    for attempt in range(3):
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await resp.text()
                last_error = f"HTTP {resp.status}: {body[:500]}"
                if resp.status < 500:
                    break  # Don't retry client errors
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))

    return {"error": True, "message": f"Backend call failed: {last_error}"}
