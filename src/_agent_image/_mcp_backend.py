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
# TASK_MODE is "manager" for a Manager session (which runs with an EMPTY
# AGENT_NAME). We need it so the _caller envelope can name the Manager —
# otherwise fail-closed backend role gates (e.g. complete_scope_verification)
# see an empty actor and reject the Manager with "actor='(none)'".
TASK_MODE = os.environ.get("TASK_MODE", "execute")
# Bubble honesty (owner directive 2026-08-04): "1" when this session is a
# daemon consult RE-RUN (infra / verdictless refire) — threaded by the
# worker (``_agent_worker_mcp.build_mcp_config``) so the backend's
# planner_completed bubbles can say "re-run after interruption".
CONSULT_REFIRE = os.environ.get("CONSULT_REFIRE", "") == "1"
# SEC3-01: per-office capability secret for the DIRECT /tool-call fallback.
# Sent as the ``X-Office-Secret`` header so the backend can authenticate
# this office's tool calls (the proxy→WS path is office-pinned separately).
OFFICE_TOOL_SECRET = os.environ.get("OFFICE_TOOL_SECRET", "")

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


def _caller_envelope() -> dict:
    """The ``_caller`` identity stamped on every backend tool-call.

    Carries who the agent CLAIMS to be so the backend dispatcher can apply
    defense-in-depth role gates (the in-container tool-list filter is the
    primary defense; this is the backstop).

    The Manager session runs with an EMPTY ``AGENT_NAME`` but
    ``TASK_MODE=="manager"`` — without a concrete ``agent_name`` here,
    ``resolve_effective_actor`` (which reads agent_name, NOT role) sees an
    empty actor and the fail-closed plan/verify gates reject the Manager
    with ``actor='(none)'``. So name it "manager". Workers (AGENT_NAME set)
    and the Planner (AGENT_NAME=="planner") are unaffected.
    """
    caller_name = AGENT_NAME or ("manager" if TASK_MODE == "manager" else "")
    envelope = {
        "agent_name": caller_name,
        "role": "worker" if AGENT_NAME else "manager",
    }
    if CONSULT_REFIRE:
        # Daemon consult re-run marker (bubble honesty, 2026-08-04) —
        # only ever stamped on refired Planner consult sessions.
        envelope["consult_refire"] = True
    return envelope


async def _call_backend(action: str, params: dict) -> dict:
    """Call the backend tool-call endpoint with retry logic.

    If TOOL_PROXY_URL is set, routes through the local communicator proxy
    (which forwards via WebSocket). Falls back to direct backend HTTP if
    the proxy is unavailable.
    """
    import aiohttp

    payload = {
        "action": action,
        "params": params,
        "_caller": _caller_envelope(),
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

    # Direct backend call (fallback path). Authenticate with the per-office
    # capability secret (SEC3-01) so the backend accepts it — the proxy path
    # above is office-pinned over the WS and needs no header.
    url = f"{BACKEND_URL}/api/offices/{OFFICE_ID}/tool-call"
    direct_headers = (
        {"X-Office-Secret": OFFICE_TOOL_SECRET} if OFFICE_TOOL_SECRET else {}
    )
    for attempt in range(3):
        try:
            async with session.post(
                url, json=payload, headers=direct_headers
            ) as resp:
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
