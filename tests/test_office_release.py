"""A moved or parked office is released, never wiped.

The platform sends ``office_released`` when an office is assigned to
another Company Token or parked, and ``office_deleted`` only when it is
really deleted. A release is a hint: discovery decides whether this daemon
still serves the office, and the disconnect keeps the office's files,
Office Secrets and sign-in on the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import daemon
from src._handlers._office_lifecycle import handle_office_released
from src.config import Config, OfficeConfig
from src.daemon import ProcessModelComponents
from src.handlers import _register_process_model_handlers

CONFIG = Config(platform_url="https://platform.example", security_token="test-token")


def _discovery(monkeypatch: pytest.MonkeyPatch, result) -> AsyncMock:
    """Make token-filtered discovery return ``result`` (or raise it)."""
    fetch = AsyncMock(
        side_effect=result if isinstance(result, Exception) else None,
        return_value=None if isinstance(result, Exception) else result,
    )
    monkeypatch.setattr(daemon, "fetch_offices", fetch)
    monkeypatch.setattr(daemon, "_mark_token_revoked", MagicMock())
    monkeypatch.setattr(daemon, "_clear_token_revoked", MagicMock())
    return fetch


async def _run_until(consumer, done) -> None:
    """Run a lifecycle consumer until ``done()``, then let it finish its item.

    The consumers only notice shutdown between queue reads (a 5 second
    wait), so the test cancels the task instead of waiting that out.
    """
    task = asyncio.create_task(consumer)
    try:
        async with asyncio.timeout(5):
            while not done():
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _consume(queue: asyncio.Queue, connected: dict, *, done=None, **overrides) -> None:
    """Feed the queued hints to the daemon's lifecycle consumer."""
    await _run_until(
        daemon._consume_office_creates(
            queue,
            config=CONFIG,
            containers=overrides.get("containers"),
            redis_client=overrides.get("redis_client"),
            connected=connected,
            connecting=set(),
            background_tasks=[],
            shutdown_event=asyncio.Event(),
        ),
        done or queue.empty,
    )


# ── the router handler ──────────────────────────────────────────────


async def test_handler_enqueues_a_release_hint_for_its_own_office() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    await handle_office_released(
        {"type": "office_released", "office_id": "ignored-payload-id"},
        create_queue=queue,
        office=SimpleNamespace(id="office-1"),
    )
    assert queue.get_nowait() == {"office_id": "office-1", "released": True}


async def test_handler_without_a_queue_leaves_release_to_the_poll_loop() -> None:
    await handle_office_released({}, create_queue=None, office=SimpleNamespace(id="o"))


async def test_handler_survives_a_full_queue() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait({"office_id": "other"})
    await handle_office_released({}, create_queue=queue, office=SimpleNamespace(id="o"))
    assert queue.qsize() == 1


async def test_router_registers_the_release_handler() -> None:
    router = MagicMock()
    queue: asyncio.Queue = asyncio.Queue()
    _register_process_model_handlers(
        router,
        config_store=MagicMock(),
        script_syncer=MagicMock(),
        claude_md_writer=MagicMock(),
        mgr=MagicMock(),
        supervisor=MagicMock(),
        dispatcher=MagicMock(),
        script_runner=MagicMock(),
        secrets_store=MagicMock(),
        queue_manager=MagicMock(),
        office=SimpleNamespace(id="office-1"),
        create_queue=queue,
    )
    handlers = {call.args[0]: call.args[1] for call in router.on.call_args_list}
    await handlers["office_released"]({"type": "office_released"})
    assert queue.get_nowait() == {"office_id": "office-1", "released": True}


# ── the daemon-level consumer ───────────────────────────────────────


async def test_release_disconnects_an_office_discovery_no_longer_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = _discovery(monkeypatch, [OfficeConfig(id="stays", name="Stays")])
    disconnect = AsyncMock()
    monkeypatch.setattr(daemon, "_disconnect_office_process_model", disconnect)
    connected = {"moved": object(), "stays": object()}
    containers, redis_client = MagicMock(), MagicMock()
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"office_id": "moved", "released": True})

    await _consume(queue, connected, containers=containers, redis_client=redis_client)

    fetch.assert_awaited_once()
    # Positional-only call: ``delete_workspace`` keeps its non-destructive
    # default, exactly like the poll loop's reconcile pass.
    disconnect.assert_awaited_once_with("moved", connected, containers, redis_client)


async def test_release_is_ignored_while_discovery_still_lists_the_office(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint reached the wrong daemon, or the office was assigned back."""
    _discovery(monkeypatch, [OfficeConfig(id="mine", name="Mine")])
    disconnect = AsyncMock()
    monkeypatch.setattr(daemon, "_disconnect_office_process_model", disconnect)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"office_id": "mine", "released": True})

    await _consume(queue, {"mine": object()})

    disconnect.assert_not_awaited()


async def test_release_is_ignored_when_discovery_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No authoritative answer authorizes no teardown; the poll retries."""
    _discovery(monkeypatch, RuntimeError("platform unreachable"))
    disconnect = AsyncMock()
    monkeypatch.setattr(daemon, "_disconnect_office_process_model", disconnect)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"office_id": "mine", "released": True})

    await _consume(queue, {"mine": object()})

    disconnect.assert_not_awaited()


async def test_release_of_an_office_this_daemon_does_not_serve_asks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = _discovery(monkeypatch, [])
    disconnect = AsyncMock()
    monkeypatch.setattr(daemon, "_disconnect_office_process_model", disconnect)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"office_id": "elsewhere", "released": True})

    await _consume(queue, {})

    fetch.assert_not_awaited()
    disconnect.assert_not_awaited()


async def test_release_spares_an_instance_connected_while_discovery_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = {"office": object()}
    newer_instance = object()

    async def _discovered(*_args):
        connected["office"] = newer_instance
        return []

    monkeypatch.setattr(daemon, "_discover_offices", _discovered)
    disconnect = AsyncMock()
    monkeypatch.setattr(daemon, "_disconnect_office_process_model", disconnect)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"office_id": "office", "released": True})

    await _consume(queue, connected)

    disconnect.assert_not_awaited()
    assert connected["office"] is newer_instance


async def test_a_failed_release_does_not_stop_the_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _discovery(monkeypatch, [])
    disconnect = AsyncMock(side_effect=[RuntimeError("docker down"), None])
    monkeypatch.setattr(daemon, "_disconnect_office_process_model", disconnect)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"office_id": "first", "released": True})
    queue.put_nowait({"office_id": "second", "released": True})

    await _consume(queue, {"first": object(), "second": object()})

    assert [call.args[0] for call in disconnect.await_args_list] == ["first", "second"]


# ── both paths against the real teardown ────────────────────────────


def _components(office_name: str) -> ProcessModelComponents:
    script_runner = MagicMock()
    script_runner._cron_scheduler = None
    script_runner.shutdown = AsyncMock()
    return ProcessModelComponents(
        supervisor=MagicMock(shutdown=AsyncMock()),
        dispatcher=MagicMock(stop=AsyncMock()),
        router=MagicMock(stop=AsyncMock()),
        reporter=MagicMock(),
        script_runner=script_runner,
        watchdog_task=None,
        queue_manager=None,
        tool_proxy=None,
        office_name=office_name,
    )


@pytest.fixture
def office_on_disk(tmp_path, monkeypatch):
    """One served office with a workspace and an Office Secrets file."""
    from src import paths

    home = tmp_path / ".cubicle"
    monkeypatch.setattr(paths, "CUBICLE_HOME", home)
    monkeypatch.setattr(paths, "get_runtime_state_path", lambda: home / "runtime" / "control.sqlite3")
    workspace = home / "workspaces" / "moving-office"
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outputs" / "report.md").write_text("kept work")
    secrets_file = home / "office-secrets" / "moving-office.json"
    secrets_file.parent.mkdir(parents=True)
    secrets_file.write_text('{"API_TOKEN": "value"}')
    connected = {"office-1": _components("Moving Office")}
    containers = MagicMock(stop_office=AsyncMock(), delete_private_storage=AsyncMock())
    return SimpleNamespace(
        workspace=workspace, secrets_file=secrets_file,
        connected=connected, containers=containers,
    )


async def test_released_office_keeps_its_files_and_private_storage(
    office_on_disk, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fakeredis.aioredis import FakeRedis

    _discovery(monkeypatch, [])
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"office_id": "office-1", "released": True})

    await _consume(
        queue, office_on_disk.connected,
        containers=office_on_disk.containers, redis_client=FakeRedis(),
        done=lambda: office_on_disk.containers.stop_office.await_count > 0,
    )

    assert "office-1" not in office_on_disk.connected
    office_on_disk.containers.stop_office.assert_awaited_once_with("office-1")
    office_on_disk.containers.delete_private_storage.assert_not_awaited()
    assert (office_on_disk.workspace / "outputs" / "report.md").read_text() == "kept work"
    assert office_on_disk.secrets_file.exists()


async def test_deleted_office_is_still_wiped(office_on_disk) -> None:
    from fakeredis.aioredis import FakeRedis

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait("office-1")

    await _run_until(
        daemon._consume_office_deletes(
            queue, office_on_disk.connected, office_on_disk.containers,
            FakeRedis(), asyncio.Event(),
        ),
        lambda: not office_on_disk.workspace.exists(),
    )

    office_on_disk.containers.delete_private_storage.assert_awaited_once_with("office-1")
    assert not office_on_disk.workspace.exists()
    assert not office_on_disk.secrets_file.exists()
