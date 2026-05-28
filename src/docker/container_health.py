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

# How many escalation attempts before we stop trying. After this
# many restart attempts that don't stick (container exits again
# within the next health window), assume the failure is structural
# (image missing, broken Dockerfile, OOM, port conflict) and stop
# spamming the log every 90 s. The operator sees a single loud
# "GIVING UP" message + the office stays offline until manual
# intervention. The counter resets the next time the container is
# observed as ``running`` (operator fixed it from outside).
_MAX_ESCALATIONS = 10

# Type for the restart callback (office_id) -> None
RestartCallback = Callable[[str], Coroutine[Any, Any, None]]


async def health_check_all(
    containers: dict[str, Any],
    on_crash: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    on_restart: RestartCallback | None = None,
    on_giveup: Callable[[str, str], Coroutine[Any, Any, None]] | None = None,
) -> None:
    """Background loop: check all containers every 30 seconds.

    Detects crashed containers and attempts recovery:
    1. Logs the crash.
    2. Waits for Docker restart policy (``unless-stopped``).
    3. If callback provided, calls ``on_crash(office_id)`` so the
       caller can move in-progress tasks to Blocked and notify Manager.
    4. After 3 consecutive failures, escalates by calling
       ``on_restart(office_id)`` to force a container restart.
    5. After ``_MAX_ESCALATIONS`` failed restart attempts, calls
       ``on_giveup(office_id, message)`` so the caller can push a
       sticky error to the backend (so the UI shows actionable copy
       instead of generic "disconnected").

    Parameters
    ----------
    containers:
        Dict mapping office_id to Docker container objects.
    on_crash:
        Optional async callback invoked with office_id on crash detection.
    on_restart:
        Optional async callback invoked with office_id after 3 consecutive
        health check failures, to trigger a forced restart.
    on_giveup:
        Optional async callback invoked when the loop gives up on an
        office (10 consecutive failed restarts). Caller pushes the
        message to ``connector_statuses.last_error`` so the UI
        surfaces actionable text instead of silent offline state.
    """
    # Track consecutive failure counts per office
    failure_counts: dict[str, int] = defaultdict(int)
    # Track escalation attempts that didn't stick. Once this hits
    # ``_MAX_ESCALATIONS`` for a given office, we stop trying to
    # restart and the loop goes silent for that office until either
    # the operator brings it back up or the daemon restarts. Without
    # this cap the log would spam "ESCALATION" every 90s forever on
    # a structurally-broken container (image gone, OOM-on-start,
    # port conflict).
    escalation_counts: dict[str, int] = defaultdict(int)
    # Suppress duplicate "given up" log lines per office — emit once
    # per escalation-cap-breach, then go quiet.
    given_up: set[str] = set()

    try:
        while True:
            await asyncio.sleep(30)
            for office_id in list(containers):
                container = containers.get(office_id)
                if not container:
                    failure_counts.pop(office_id, None)
                    escalation_counts.pop(office_id, None)
                    given_up.discard(office_id)
                    continue
                try:
                    await asyncio.to_thread(container.reload)
                    if container.status != "running":
                        failure_counts[office_id] += 1
                        consecutive = failure_counts[office_id]
                        # Stay quiet for offices we've already given
                        # up on — they need operator action, not
                        # another log line per minute.
                        if office_id not in given_up:
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
                            if office_id in given_up:
                                # Already gave up — don't log,
                                # don't retry. Just reset the
                                # counter so we don't overflow on
                                # a long-running stalemate.
                                failure_counts[office_id] = 0
                                continue
                            escalation_counts[office_id] += 1
                            attempt = escalation_counts[office_id]
                            if attempt > _MAX_ESCALATIONS:
                                giveup_msg = (
                                    f"Container failed {_MAX_ESCALATIONS} "
                                    "consecutive restart attempts. The "
                                    "office is offline until an operator "
                                    "fixes the underlying problem. Try: "
                                    "(a) docker logs the container to see "
                                    "why it exits on start, (b) cbcl stop "
                                    "&& cbcl start, (c) rebuild the agent "
                                    "image if the issue is in the image."
                                )
                                logger.error(
                                    "GIVING UP on container for office %s: %s",
                                    office_id, giveup_msg,
                                )
                                given_up.add(office_id)
                                failure_counts[office_id] = 0
                                # Push the message to the backend so the UI
                                # shows actionable copy. Best-effort —
                                # callback failure is non-fatal (the log
                                # line above is the operator's backstop).
                                if on_giveup:
                                    try:
                                        await on_giveup(office_id, giveup_msg)
                                    except Exception:
                                        logger.exception(
                                            "on_giveup callback failed for "
                                            "office %s — UI won't show "
                                            "actionable message",
                                            office_id,
                                        )
                                continue
                            logger.error(
                                "ESCALATION %d/%d: Container for office %s "
                                "has failed %d consecutive health checks. "
                                "Attempting forced restart.",
                                attempt, _MAX_ESCALATIONS,
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
                        # Container is healthy — reset all the failure
                        # bookkeeping. Recovery clears the "given up"
                        # flag so we'll re-escalate if it falls over
                        # again later.
                        if failure_counts.get(office_id, 0) > 0 or office_id in given_up:
                            logger.info(
                                "Container for office %s recovered "
                                "(prior failures: %d, escalations: %d)",
                                office_id,
                                failure_counts.get(office_id, 0),
                                escalation_counts.get(office_id, 0),
                            )
                        failure_counts[office_id] = 0
                        escalation_counts.pop(office_id, None)
                        given_up.discard(office_id)
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
