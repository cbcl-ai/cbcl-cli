"""MCP server listing helpers (split from handlers.py).

``refresh_mcp_list`` runs ``claude mcp list`` inside the office
container, parses the human-readable output, caches the structured
result in Redis (TTL 10 min), and broadcasts an ``mcp_list_updated``
WebSocket event so the frontend's Connectors page refreshes without
polling.

Debounced via a caller-supplied ``MCPRefreshState`` so a flurry of
mcp_add / mcp_remove events don't queue 10 concurrent ``mcp list``
docker-exec calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MCPRefreshState:
    """Tracks the timestamp of the last successful refresh for debouncing."""

    last: float = 0.0


def parse_mcp_list(text: str) -> list[dict]:
    """Parse the text output of ``claude mcp list`` into structured data.

    Each line has the form ``name: url - status`` or just ``name: url``
    (no status). Status keywords matched: "Connected", "Needs
    authentication", "Failed". Transport is parsed from a trailing
    ``(http)`` / ``(stdio)`` parenthesised suffix on the URL.

    Returns one dict per server with keys: name, url, transport,
    status, source ("claude.ai" if the name starts with that prefix,
    else "local").
    """
    servers: list[dict] = []
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Checking"):
            continue
        if ": " not in line:
            continue
        name, rest = line.split(": ", 1)
        name = name.strip()
        source = "claude.ai" if name.startswith("claude.ai ") else "local"
        url = ""
        transport = "http"
        status = "unknown"
        if " - " in rest:
            url_part, status_part = rest.rsplit(" - ", 1)
            if "Connected" in status_part:
                status = "connected"
            elif "Needs authentication" in status_part:
                status = "needs_auth"
            elif "Failed" in status_part:
                status = "failed"
            url_part = url_part.strip()
            if url_part.endswith(")"):
                paren = url_part.rfind("(")
                if paren > 0:
                    transport = url_part[paren + 1:-1].lower()
                    url = url_part[:paren].strip()
                else:
                    url = url_part
            else:
                url = url_part
        else:
            url = rest.strip()
        servers.append({
            "name": name,
            "url": url,
            "transport": transport,
            "status": status,
            "source": source,
        })
    return servers


async def refresh_mcp_list(
    *,
    state: MCPRefreshState,
    container_name: str,
    redis_client,
    router,
    office_id: str,
    force: bool = False,
) -> None:
    """Refresh the cached MCP list.

    The 5s debounce protects the periodic office-startup refresh
    from spamming ``claude mcp list`` if multiple subsystems wake
    up at once. It is WRONG for post-mutation refreshes — if an
    operator clicks "Add MCP" within 5s of office connect, the
    debounce would silently swallow the post-add refresh and the
    UI would never see the new server appear (the ``mcp_list_updated``
    WS event never fires → no React-Query invalidation → operator
    refreshes the page in confusion).

    ``force=True`` bypasses the debounce. The mcp_add / mcp_remove
    / mcp_oauth_callback handlers all pass it; routine periodic
    refreshes leave it False.
    """
    now = time.monotonic()
    if not force and now - state.last < 5:
        return
    state.last = now

    cmd = ["docker", "exec", container_name, "claude", "mcp", "list"]
    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            servers = parse_mcp_list(result.stdout)
            cache_key = f"office:{office_id}:mcp_list"
            await redis_client.set(
                cache_key, json.dumps(servers), ex=600,
            )
            logger.info("MCP list cached: %d servers", len(servers))
            await router.publish_event({
                "type": "mcp_list_updated",
                "servers": servers,
            })
    except Exception as exc:
        logger.debug("MCP list refresh failed: %s", exc)
