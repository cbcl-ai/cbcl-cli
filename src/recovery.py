"""Startup recovery helpers.

Handles cleanup of stale state from previous communicator sessions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("cbcl.recovery")


def mark_stale_script_executions(workspace_path: str) -> int:
    """Blindly mark any 'running' script executions as failed (legacy).

    DEPRECATED (ADD-C1): this rewrote status running→failed without
    checking whether the in-container process was still alive. Because
    the office container is reused across daemon restarts, a job that
    was still running (or had already succeeded) got reported failed,
    which made the Manager rework runs that actually worked. The office
    init path now calls the container-aware
    :func:`src.scripts.script_execution.reconcile_orphaned_executions`
    instead, which checks the real PID and kills orphans cleanly.

    Kept only as a synchronous, container-blind fallback for callers
    that have no container handle (host-only test contexts). Returns the
    number of stale executions found and marked.
    """
    scripts_dir = Path(workspace_path) / ".scripts"
    if not scripts_dir.exists():
        return 0

    count = 0
    for status_file in scripts_dir.glob("*/executions/*/status.json"):
        try:
            data = json.loads(status_file.read_text())
            if data.get("status") == "running":
                data["status"] = "failed"
                data["error_message"] = "Communicator restarted while script was running"
                status_file.write_text(json.dumps(data, indent=2))
                count += 1
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read/update stale script status file %s: %s",
                status_file, exc,
            )
    return count
