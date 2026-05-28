"""Office lifecycle handlers (split from handlers.py).

Both `office_created` and `office_deleted` enqueue the work for the
daemon to process out-of-band rather than tearing down / spinning
up in the router callback itself — the router can't stop the
office it's currently running in.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def handle_office_deleted(
    msg: dict,  # noqa: ARG001 — payload not consumed here (id already known)
    *,
    delete_queue,
    office,
) -> None:
    """Enqueue this office for daemon-side teardown."""
    if delete_queue is None:
        logger.warning(
            "office_deleted received but no delete_queue wired — "
            "tear-down will happen on next poll-loop reconcile",
        )
        return
    try:
        delete_queue.put_nowait(str(office.id))
    except asyncio.QueueFull:
        # Bounded at maxsize=1000 in daemon.py — this guard is the
        # backstop for a runaway producer. The poll-loop will pick
        # the office up on its next reconcile tick.
        logger.error(
            "delete_queue full — falling back to poll-loop "
            "reconciliation for office %s", office.id,
        )


async def handle_office_created(
    msg: dict,
    *,
    create_queue,
) -> None:
    """Enqueue the newly-created office for daemon-side connect.

    The backend broadcasts ``office_created`` on every connected
    WebSocket (it has no daemon-wide channel), so multiple routers
    in the same daemon see the same payload. The daemon-level
    consumer dedupes by checking ``connected`` before connecting,
    so duplicate enqueues are harmless.

    We don't connect inline because that would await sub-tasks
    (``init_office_process_model``, ``_connect_office_process_model``)
    which interact with the daemon's shared state — easier to reason
    about when the connect runs from a single consumer task on the
    daemon loop, not from N parallel router callbacks.
    """
    new_office_id = msg.get("office_id", "")
    new_office_name = msg.get("name", "")
    if not new_office_id:
        logger.warning(
            "office_created received with no office_id: %r", msg,
        )
        return
    if create_queue is None:
        logger.debug(
            "office_created received but no create_queue wired — "
            "new office will connect on next poll-loop tick",
        )
        return
    try:
        create_queue.put_nowait({
            "office_id": new_office_id,
            "name": new_office_name,
        })
    except asyncio.QueueFull:
        logger.error(
            "create_queue full — falling back to poll-loop "
            "discovery for office %s", new_office_id,
        )
