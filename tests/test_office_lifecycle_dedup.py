"""Lifecycle dedup tests for the office create/delete consumers.

The big risk in the office_created/office_deleted push design is
race-induced double-connect or torn state. Specifically:

  * The backend BROADCASTS ``office_created`` on every connected
    WS, so multiple per-office routers in the same daemon enqueue
    the same office_id. The consumer must dedupe.

  * The poll loop (every 15s) reconciles offices in BOTH directions.
    A new office can appear in the discover list at the same instant
    the create-consumer is mid-connect for it. Without an in-flight
    lock both code paths spawn a parallel ``_connect_office_process_model``
    coroutine, leaving an orphan ProcessModelComponents whose
    container, WS and agent processes leak.

These tests pin the contract: the ``connecting`` set is the mutex
between consumer and poll loop; both must check membership before
calling connect; both must clear the marker on completion.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_consumer_dedupes_against_connected_set() -> None:
    """If the office is already fully connected, the create-consumer
    must NOT call connect a second time. Logs a debug line and moves
    on. This is the simple case — no race, just a duplicate broadcast."""
    from src.daemon import _consume_office_creates

    create_queue: asyncio.Queue = asyncio.Queue()
    connected: dict = {"already-here": object()}  # any sentinel
    connecting: set[str] = set()
    shutdown = asyncio.Event()

    await create_queue.put({"office_id": "already-here", "name": "X"})

    with patch(
        "src.daemon._connect_office_process_model",
        new=AsyncMock(),
    ) as mock_connect:
        # Run the consumer briefly then signal shutdown.
        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        await asyncio.gather(
            _consume_office_creates(
                create_queue, config=None, containers=None,  # type: ignore[arg-type]
                redis_client=None,
                connected=connected, connecting=connecting,
                background_tasks=[], shutdown_event=shutdown,
            ),
            _stop_soon(),
        )
        mock_connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumer_dedupes_against_connecting_set() -> None:
    """If the poll loop is mid-connect for this office (its id is
    in the in-flight ``connecting`` set), the create-consumer MUST
    skip rather than start a parallel connect. This is the race
    that originally leaked containers."""
    from src.daemon import _consume_office_creates

    create_queue: asyncio.Queue = asyncio.Queue()
    connected: dict = {}
    connecting: set[str] = {"in-flight"}  # poll loop is mid-connect
    shutdown = asyncio.Event()

    await create_queue.put({"office_id": "in-flight", "name": "X"})

    with patch(
        "src.daemon._connect_office_process_model",
        new=AsyncMock(),
    ) as mock_connect:
        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        await asyncio.gather(
            _consume_office_creates(
                create_queue, config=None, containers=None,  # type: ignore[arg-type]
                redis_client=None,
                connected=connected, connecting=connecting,
                background_tasks=[], shutdown_event=shutdown,
            ),
            _stop_soon(),
        )
        mock_connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumer_marks_inflight_before_connect_then_clears() -> None:
    """When the consumer DOES call connect, it must:

      1. Add ``office_id`` to ``connecting`` BEFORE awaiting
         ``_connect_office_process_model`` (so the poll loop
         sees the in-flight marker on its next tick).
      2. Discard from ``connecting`` AFTER the connect returns
         (so a future retry doesn't get blocked forever).
    """
    from src.daemon import _consume_office_creates

    create_queue: asyncio.Queue = asyncio.Queue()
    connected: dict = {}
    connecting: set[str] = set()
    shutdown = asyncio.Event()

    inflight_observed: list[bool] = []

    async def _slow_connect(*args, **kwargs) -> None:
        # While inside connect, the office_id MUST be visible in
        # the in-flight set so a parallel poll-loop tick skips.
        inflight_observed.append("new" in connecting)
        await asyncio.sleep(0.01)

    await create_queue.put({"office_id": "new", "name": "X"})

    with patch(
        "src.daemon._connect_office_process_model",
        new=AsyncMock(side_effect=_slow_connect),
    ):
        async def _stop_soon() -> None:
            await asyncio.sleep(0.1)
            shutdown.set()

        await asyncio.gather(
            _consume_office_creates(
                create_queue, config=None, containers=None,  # type: ignore[arg-type]
                redis_client=None,
                connected=connected, connecting=connecting,
                background_tasks=[], shutdown_event=shutdown,
            ),
            _stop_soon(),
        )

    assert inflight_observed == [True], (
        "connecting set must contain office_id during connect"
    )
    assert "new" not in connecting, (
        "connecting set must be cleared after connect completes"
    )


@pytest.mark.asyncio
async def test_consumer_clears_inflight_even_on_connect_exception() -> None:
    """Connect raising must NOT leak the in-flight marker. Otherwise
    a transient backend hiccup would permanently lock the office
    out of subsequent poll-loop retries."""
    from src.daemon import _consume_office_creates

    create_queue: asyncio.Queue = asyncio.Queue()
    connected: dict = {}
    connecting: set[str] = set()
    shutdown = asyncio.Event()

    await create_queue.put({"office_id": "boom", "name": "X"})

    with patch(
        "src.daemon._connect_office_process_model",
        new=AsyncMock(side_effect=RuntimeError("simulated transport error")),
    ):
        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        await asyncio.gather(
            _consume_office_creates(
                create_queue, config=None, containers=None,  # type: ignore[arg-type]
                redis_client=None,
                connected=connected, connecting=connecting,
                background_tasks=[], shutdown_event=shutdown,
            ),
            _stop_soon(),
        )

    assert "boom" not in connecting, (
        "in-flight marker must be cleared even when connect raises"
    )


@pytest.mark.asyncio
async def test_consumer_isolates_failures_across_iterations() -> None:
    """One bad office must not block subsequent ones. Otherwise a
    single corrupt broadcast permanently disables proactive
    teardowns until daemon restart."""
    from src.daemon import _consume_office_creates

    create_queue: asyncio.Queue = asyncio.Queue()
    connected: dict = {}
    connecting: set[str] = set()
    shutdown = asyncio.Event()

    seen: list[str] = []

    async def _flaky_connect(office, *args, **kwargs) -> None:
        seen.append(office.id)
        if office.id == "bad":
            raise RuntimeError("nope")

    await create_queue.put({"office_id": "bad", "name": "Bad"})
    await create_queue.put({"office_id": "good", "name": "Good"})

    with patch(
        "src.daemon._connect_office_process_model",
        new=AsyncMock(side_effect=_flaky_connect),
    ):
        async def _stop_soon() -> None:
            await asyncio.sleep(0.1)
            shutdown.set()

        await asyncio.gather(
            _consume_office_creates(
                create_queue, config=None, containers=None,  # type: ignore[arg-type]
                redis_client=None,
                connected=connected, connecting=connecting,
                background_tasks=[], shutdown_event=shutdown,
            ),
            _stop_soon(),
        )

    assert seen == ["bad", "good"], (
        "consumer must process subsequent items after a failure"
    )


@pytest.mark.asyncio
async def test_delete_consumer_runs_disconnect_for_each_payload() -> None:
    """The delete consumer's only job is to drain the queue and call
    ``_disconnect_office_process_model`` for each office_id. If it
    skipped or batched we'd leak a container; if it raised on a
    single bad disconnect the rest would stall."""
    from src.daemon import _consume_office_deletes

    delete_queue: asyncio.Queue = asyncio.Queue()
    connected: dict = {}
    shutdown = asyncio.Event()

    seen: list[str] = []

    async def _capture(oid, *args, **kwargs) -> None:
        seen.append(oid)

    await delete_queue.put("a")
    await delete_queue.put("b")

    with patch(
        "src.daemon._disconnect_office_process_model",
        new=AsyncMock(side_effect=_capture),
    ):
        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            shutdown.set()

        await asyncio.gather(
            _consume_office_deletes(
                delete_queue, connected=connected, containers=None,  # type: ignore[arg-type]
                redis_client=None, shutdown_event=shutdown,
            ),
            _stop_soon(),
        )

    assert seen == ["a", "b"]
