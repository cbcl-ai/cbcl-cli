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
import re
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MCPRefreshState:
    """Tracks the timestamp of the last successful refresh for debouncing."""

    last: float = 0.0


# ``claude mcp list`` has shipped at least two output formats in the
# wild. Old (pre-2025): ``name: <url> - <status>`` with a dash
# separator. Current (2025+): ``name: <url> (<transport>) ✓ Connected``
# with a check / cross glyph and no dash. We need to handle BOTH —
# otherwise users on a newer CLI see their MCPs cached with
# status="unknown" and the UI filters them out completely.
#
# Status detection: scan the whole line for substring keywords. The
# CLI prints stable English phrases regardless of glyph variation:
# "Connected", "Failed", "Needs authentication". Substring match is
# robust to leading glyphs (``✓ Connected``), color escapes
# (``\x1b[32mConnected\x1b[0m``), and the historical ``- Connected``.
#
# Transport detection: parse the parenthesised tag (``(stdio)`` /
# ``(http)`` / ``(sse)`` / ``(HTTP)``) anywhere in the body. Case-
# insensitive so newer CLIs that print ``(HTTP)`` don't drop to the
# default.
_TRANSPORT_RE = re.compile(r"\(([A-Za-z]+)\)")
# ANSI / glyph noise we strip when extracting the URL or command
# token. Keeps the cached payload clean for display.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_STATUS_GLYPH_RE = re.compile(r"[✓✗×•·]")


def _detect_status(body: str) -> str:
    """Return ``connected`` / ``needs_auth`` / ``failed`` / ``unknown``.

    Substring-based so the same logic works for both the old
    ``- Connected`` and the new ``✓ Connected`` formats.
    Order matters: ``Needs authentication`` is checked BEFORE
    ``Failed`` because some CLI versions render auth-needed as
    ``✗ Failed to connect: needs authentication`` and we want the
    more specific bucket.
    """
    haystack = body.lower()
    if "needs authentication" in haystack or "needs_auth" in haystack:
        return "needs_auth"
    if "connected" in haystack:
        return "connected"
    if "failed" in haystack or "error" in haystack:
        return "failed"
    return "unknown"


def _clean_url(text: str) -> str:
    """Strip ANSI escapes, status glyphs, and the transport tag from
    the URL/command portion of a parsed line.

    Marker search is case-INSENSITIVE because ``_detect_status``
    already substring-matches lowercased input — without matching
    casings here, a future CLI build that prints lowercase
    ``"connected"`` would classify status correctly but leave the
    literal word "connected" stuck in the URL field of the cached
    server payload (visible to operators in the detail panel).
    """
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    cleaned = _TRANSPORT_RE.sub("", cleaned)
    cleaned = _STATUS_GLYPH_RE.sub("", cleaned)
    # Drop trailing status text after the LAST occurrence of any
    # status keyword so the URL doesn't include "Connected" etc.
    # Lowercased compare so glyph-stripped + cased variants
    # ("CONNECTED", "Connected", "connected") all trim.
    lowered = cleaned.lower()
    for marker in ("connected", "failed", "needs authentication", "error"):
        idx = lowered.rfind(marker)
        if idx > 0:
            cleaned = cleaned[:idx]
            lowered = cleaned.lower()
    # Old format used " - " as separator; drop the trailing dash.
    if " - " in cleaned:
        cleaned = cleaned.rsplit(" - ", 1)[0]
    return cleaned.strip(" -\t")


def parse_mcp_list(text: str) -> list[dict]:
    """Parse the text output of ``claude mcp list`` into structured data.

    Handles both historical formats:

    * Old: ``name: url - status``
    * New: ``name: url (transport) ✓ Connected``

    Status keywords are detected by substring match anywhere in the
    body so glyph / color / dash variations don't drop the entry.
    Transport tags (``(stdio)`` / ``(http)`` / ``(sse)``) are parsed
    case-insensitively.

    Returns one dict per server with keys: name, url, transport,
    status, source. ``source`` is ``"claude.ai"`` when the name
    starts with that prefix (registry catalog entries), else
    ``"local"`` (custom adds).

    A server that's present in the CLI's output but whose status
    line can't be classified gets ``status="unknown"`` — the UI
    surfaces it under "Other" rather than dropping it. Dropping was
    the original bug that made user-added MCPs invisible.
    """
    servers: list[dict] = []
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("checking"):
            continue
        # The CLI sometimes prints a blank header / footer line we
        # want to ignore. Real entries always have ``name: body``.
        if ": " not in line:
            continue
        name, rest = line.split(": ", 1)
        name = name.strip()
        if not name:
            continue
        source = "claude.ai" if name.startswith("claude.ai ") else "local"
        transport_match = _TRANSPORT_RE.search(rest)
        transport = (
            transport_match.group(1).lower() if transport_match else "http"
        )
        status = _detect_status(rest)
        url = _clean_url(rest)
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
