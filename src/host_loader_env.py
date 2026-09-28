"""Dynamic-loader environment names the daemon never hands to a host process.

The rule lives in the agent image's pure ``_mcp`` package
(``_agent_image/_mcp/host_loader_env.py``, which explains why these names are
dropped) so the in-container script executor applies the same rule as the
daemon. Daemon modules import it from here.
"""

from __future__ import annotations

from src._agent_image._mcp.host_loader_env import (
    LOADER_ENV_NAMES,
    LOADER_ENV_PREFIXES,
    LOADER_REASON,
    is_loader_env_name,
)

__all__ = [
    "LOADER_ENV_NAMES",
    "LOADER_ENV_PREFIXES",
    "LOADER_REASON",
    "is_loader_env_name",
]
