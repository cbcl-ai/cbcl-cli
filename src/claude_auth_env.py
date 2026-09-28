"""Environment variable names that change how the Claude CLI signs in.

The rule lives in the agent image's pure ``_mcp`` package
(``_agent_image/_mcp/claude_auth_env.py``, which explains why these names are
reserved) so the in-container script executor applies the same check as the
daemon. Daemon modules import it from here.

This is the daemon's own check, so a secret stored before the backend's
refusal never reaches a Claude session or an office container.
"""

from __future__ import annotations

from src._agent_image._mcp.claude_auth_env import (
    RESERVED_NAMES,
    RESERVED_PREFIXES,
    RESERVED_REASON,
    is_reserved_claude_env_name,
)

__all__ = [
    "RESERVED_NAMES",
    "RESERVED_PREFIXES",
    "RESERVED_REASON",
    "is_reserved_claude_env_name",
]
