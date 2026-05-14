"""Docker container health checking and crash detection.

Background monitoring loop for Docker containers, extracted from
ContainerManager.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Number of consecutive failures before attempting a restart.
_RESTART_THRESHOLD = 3

# Type for the restart callback (office_id) -> None
RestartCallback = Callable[[str], Coroutine[Any, Any, None]]


async def health_check_all(
    containers: dict[str, Any],
    on_crash: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    on_restart: RestartCallback | None = None,
) -> None:
    """Background loop: check all containers every 30 seconds.

    Detects crashed containers and attempts recovery:
    1. Logs the crash.
    2. Waits for Docker restart policy (``unless-stopped``).
    3. If callback provided, calls ``on_crash(office_id)`` so the
       caller can move in-progress tasks to Blocked and notify Manager.
    4. After 3 consecutive failures, escalates by calling
       ``on_restart(office_id)`` to force a container restart.

    Parameters
    ----------
    containers:
        Dict mapping office_id to Docker container objects.
    on_crash:
        Optional async callback invoked with office_id on crash detection.
    on_restart:
        Optional async callback invoked with office_id after 3 consecutive
        health check failures, to trigger a forced restart.
    """
    # Track consecutive failure counts per office
    failure_counts: dict[str, int] = defaultdict(int)

    try:
        while True:
            await asyncio.sleep(30)
            for office_id in list(containers):
                container = containers.get(office_id)
                if not container:
                    failure_counts.pop(office_id, None)
                    continue
                try:
                    await asyncio.to_thread(container.reload)
                    if container.status != "running":
                        failure_counts[office_id] += 1
                        consecutive = failure_counts[office_id]
                        logger.warning(
                            "Container for office %s is %s — "
                            "consecutive failures: %d/%d",
                            office_id, container.status,
                            consecutive, _RESTART_THRESHOLD,
                        )
                        if on_crash:
                            try:
                                await on_crash(office_id)
                            except Exception as exc:
                                logger.exception(
                                    "Error in container crash callback for "
                                    "office %s: %s", office_id, exc,
                                )

                        if consecutive >= _RESTART_THRESHOLD:
                            logger.error(
                                "ESCALATION: Container for office %s has "
                                "failed %d consecutive health checks. "
                                "Attempting forced restart.",
                                office_id, consecutive,
                            )
                            failure_counts[office_id] = 0
                            if on_restart:
                                try:
                                    await on_restart(office_id)
                                except Exception as exc:
                                    logger.exception(
                                        "Error attempting restart for "
                                        "office %s: %s", office_id, exc,
                                    )
                    else:
                        # Container is healthy — reset the failure counter
                        if failure_counts.get(office_id, 0) > 0:
                            logger.info(
                                "Container for office %s recovered after "
                                "%d consecutive failures",
                                office_id, failure_counts[office_id],
                            )
                        failure_counts[office_id] = 0
                except Exception as exc:
                    failure_counts[office_id] += 1
                    consecutive = failure_counts[office_id]
                    logger.warning(
                        "Cannot check container for office %s: %s "
                        "(consecutive failures: %d/%d)",
                        office_id, exc,
                        consecutive, _RESTART_THRESHOLD,
                    )
                    if consecutive >= _RESTART_THRESHOLD:
                        logger.error(
                            "ESCALATION: Container for office %s unreachable "
                            "for %d consecutive checks. Attempting restart.",
                            office_id, consecutive,
                        )
                        failure_counts[office_id] = 0
                        if on_restart:
                            try:
                                await on_restart(office_id)
                            except Exception as restart_exc:
                                logger.exception(
                                    "Error attempting restart for "
                                    "office %s: %s", office_id, restart_exc,
                                )
    except asyncio.CancelledError:
        return
