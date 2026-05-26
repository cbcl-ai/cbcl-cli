"""Host-side chown helpers — keep workspace files writable by the agent.

The daemon (cbcl) runs as root on the host. Files / directories it
creates on the bind-mounted workspace inherit root:root ownership.
The agent process inside the office container runs as ``uid=1000``
(``agent`` user; see ``_agent_image/Dockerfile.agent`` line ``USER
agent``). Root-owned files in the workspace are not writable by the
agent, which manifests as ``EACCES`` whenever the agent tries to
``Edit`` a script boilerplate file or land an MD output under
``/workspace/outputs/<workstream>/``.

This module centralises the "chown to agent uid" step so every
host-side write site applies it the same way. Apply it AFTER
``mkdir(...)`` / ``write_text(...)`` / ``symlink_to(...)`` so the
on-disk inode is owned by the right uid before the agent touches it.

Bind-mount semantics: numeric uid/gid is what crosses the boundary.
The host may not have a "1000" user (it does on cbcl-stg —
``deploy`` — but that's coincidence). What matters is the container
side sees uid 1000 as its ``agent`` user.

The helper is best-effort: on macOS dev or when the daemon isn't
running as root (no CAP_CHOWN), ``os.chown`` raises and we silently
fall through. Single-host dev workflows where the dev IS uid 1000
on the host don't need chown anyway.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirrors ``USER agent`` + ``useradd -m`` in
# ``_agent_image/Dockerfile.agent``. If the Dockerfile ever pins a
# different uid the value MUST be bumped here in lock-step or every
# workspace write will keep landing root-owned.
AGENT_UID = 1000
AGENT_GID = 1000


def chown_to_agent(path: Path | str) -> None:
    """Best-effort ``chown`` to the in-container agent uid/gid.

    Use after every host-side ``mkdir`` / file write that lands in
    the bind-mounted workspace. Silently swallows ``PermissionError``
    (we're not root — common on dev macOS) and ``FileNotFoundError``
    (the caller's write failed but we ran anyway — let the caller's
    exception propagate).
    """
    try:
        os.chown(path, AGENT_UID, AGENT_GID)
    except (PermissionError, OSError) as exc:
        # On macOS dev or when the daemon isn't root, chown fails.
        # Log at DEBUG so production-on-linux-as-root operators see
        # nothing in normal logs; only verbose troubleshooting
        # surfaces it. The bind-mount permissions issue this exists
        # to fix is a Linux-prod-only concern.
        logger.debug("chown to agent failed for %s: %s", path, exc)


def chown_tree_to_agent(root: Path | str) -> int:
    """Recursively chown an existing subtree to the agent uid/gid.

    Used for one-off fix-ups when historical files were written
    before the per-write chown landed. Returns the number of inodes
    actually chowned (skipped ones don't count). Best-effort like
    ``chown_to_agent``.
    """
    root_path = Path(root)
    if not root_path.exists():
        return 0
    chowned = 0
    try:
        os.chown(root_path, AGENT_UID, AGENT_GID)
        chowned += 1
    except (PermissionError, OSError):
        return 0
    if root_path.is_dir():
        for sub in root_path.rglob("*"):
            try:
                os.chown(sub, AGENT_UID, AGENT_GID, follow_symlinks=False)
                chowned += 1
            except (PermissionError, OSError):
                continue
    return chowned
