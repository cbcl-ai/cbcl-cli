"""IMP-07 — keep the agent-image cache-hash set and the Dockerfile COPY
set in lockstep.

The agent Docker image is rebuilt only when ``_compute_mcp_server_hash``
changes. If the set of files that hash covers ever drifts from the set
of files ``_agent_image/Dockerfile.agent`` actually ``COPY``s into
``/opt/cubicle``, the image can silently ship STALE MCP code — the
classic symptom is "cubicle-tools MCP server disconnected" inside the
container after an edit that didn't trigger a rebuild.

This is not an active bug (the two sets match today). This guard fails
the moment a future file is added to one set but not the other.
"""
from __future__ import annotations

import re

from src.docker.container_manager import (
    _DOCKER_DIR,
    _mcp_server_source_files,
)

# COPY <src> <dest> lines whose destination is inside the image's
# /opt/cubicle dir (where the MCP server runs).
_COPY_RE = re.compile(r"^COPY\s+(\S+)\s+(/opt/cubicle/\S+)\s*$")


def _dockerfile_copy_py_targets() -> set[str]:
    """The set of ``*.py`` source paths (relative to ``_agent_image/``)
    that Dockerfile.agent COPYs into /opt/cubicle. Directory copies
    (e.g. ``COPY _mcp /opt/cubicle/_mcp``) expand to their ``*.py``."""
    dockerfile = _DOCKER_DIR / "Dockerfile.agent"
    targets: set[str] = set()
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        m = _COPY_RE.match(raw.strip())
        if not m:
            continue
        src = m.group(1)
        src_path = _DOCKER_DIR / src
        if src_path.is_dir():
            for p in src_path.glob("*.py"):
                targets.add(p.relative_to(_DOCKER_DIR).as_posix())
        elif src.endswith(".py"):
            targets.add(src)
    return targets


def test_hash_set_matches_dockerfile_copy_set() -> None:
    hashed = {
        p.relative_to(_DOCKER_DIR).as_posix()
        for p in _mcp_server_source_files()
    }
    copied = _dockerfile_copy_py_targets()
    assert hashed == copied, (
        "agent-image cache-hash set and Dockerfile.agent COPY set "
        "diverged — the image could ship stale MCP code:\n"
        f"  covered by hash only: {sorted(hashed - copied)}\n"
        f"  COPYed but not hashed: {sorted(copied - hashed)}\n"
        "Keep _mcp_server_source_files() and the COPY lines in "
        "_agent_image/Dockerfile.agent in lockstep."
    )


def test_copy_set_is_non_trivial() -> None:
    """Sanity: the parse actually found the COPY lines (so a future
    Dockerfile reformat that breaks the regex can't make the guard
    pass vacuously)."""
    copied = _dockerfile_copy_py_targets()
    assert "mcp_tool_server.py" in copied
    assert any(t.startswith("_mcp/") for t in copied)
