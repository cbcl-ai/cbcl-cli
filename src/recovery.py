"""Startup recovery helpers.

Handles cleanup of stale state from previous communicator sessions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("cbcl.recovery")


def mark_stale_script_executions(workspace_path: str) -> int:
    """Mark any 'running' script executions from a previous session as failed.

    Returns the number of stale executions found and marked.
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
