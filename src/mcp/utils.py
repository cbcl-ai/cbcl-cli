"""Shared MCP response helpers."""

from __future__ import annotations


def mcp_text(text: str) -> dict:
    """Build a standard MCP text response.

    Replaces the repeated ``{"content": [{"type": "text", "text": ...}]}``
    boilerplate used across all MCP tool implementations.
    """
    return {"content": [{"type": "text", "text": text}]}
