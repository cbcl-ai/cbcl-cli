"""Startup recovery helpers.

Handles cleanup of stale state from previous communicator sessions.
"""

from __future__ import annotations

import logging

from src.docker.task_process_cleanup import reap_worker_executions

logger = logging.getLogger("cbcl.recovery")

async def reap_orphan_agent_sessions(container_name: str) -> int:
    """Verify marked worker cleanup before reconstructed queues may dispatch.

    Unsupported cleanup, Docker failure and untracked legacy CLI sessions abort
    office initialization rather than run a second execution beside an orphan.
    """
    count = await reap_worker_executions(container_name)
    logger.info(
        "Verified orphan worker recovery for %s: %d executions", container_name, count
    )
    return count
