"""MCP add/remove handler bodies (split from handlers.py).

The OAuth-heavy flows live in ``_oauth.py``; this module covers
the plain ``mcp_add`` / ``mcp_remove`` paths plus the on-demand
``mcp_list`` refresh.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

logger = logging.getLogger(__name__)


async def run_mcp_add(
    msg: dict,
    *,
    container_name: str,
    refresh_mcp_list,
) -> None:
    """Add an MCP server inside the container via ``claude mcp add``."""
    name = msg.get("name", "")
    transport = msg.get("transport", "http")
    url = msg.get("url", "")
    if not name or not url:
        logger.warning("mcp_add: missing name or url")
        return
    cmd = [
        "docker", "exec", container_name,
        "claude", "mcp", "add", "--transport", transport,
        "--scope", "user",
        name, url,
    ]
    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd,
            capture_output=True, text=True, timeout=30,
        )
        logger.info(
            "mcp_add %s: rc=%d, out=%s",
            name, result.returncode, result.stdout[:200],
        )
        await refresh_mcp_list()
    except Exception as exc:
        logger.warning("mcp_add failed: %s", exc)


async def run_mcp_remove(
    msg: dict,
    *,
    container_name: str,
    refresh_mcp_list,
) -> None:
    """Remove an MCP server from the container.

    Tries removing from both user and local scopes since the server
    may exist in multiple scopes.
    """
    name = msg.get("name", "")
    if not name:
        return
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", container_name,
             "claude", "mcp", "remove", name, "-s", "user"],
            capture_output=True, text=True, timeout=15,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "exec", container_name,
             "claude", "mcp", "remove", name, "-s", "local"],
            capture_output=True, text=True, timeout=15,
        )
        logger.info("mcp_remove %s: removed from all scopes", name)
        await refresh_mcp_list()
    except Exception as exc:
        logger.warning("mcp_remove failed: %s", exc)
