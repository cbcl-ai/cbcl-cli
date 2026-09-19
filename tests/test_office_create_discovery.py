"""Company-wide create pushes must not bypass token-scoped office discovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, call

import httpx
import pytest

from src import daemon
from src.config import Config, OfficeConfig


class _NotificationQueue(asyncio.Queue):
    """Stop the consumer once these finite test notifications are drained."""

    def __init__(self, shutdown: asyncio.Event, office_ids: list[str]) -> None:
        super().__init__()
        self.shutdown = shutdown
        for office_id in office_ids:
            self.put_nowait({"office_id": office_id, "name": "Untrusted old name"})

    async def get(self) -> dict:
        if self.empty():
            self.shutdown.set()
            return {}
        return await super().get()


@pytest.fixture
def config() -> Config:
    return Config(
        platform_url="https://platform.example/",
        security_token="cbcl_co_test_token_a",
    )


def _mock_http_discovery(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    client_class = httpx.AsyncClient

    def _client(**kwargs: object) -> httpx.AsyncClient:
        return client_class(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)


async def _consume(
    config: Config,
    office_ids: list[str],
    *,
    connected: dict | None = None,
    connecting: set[str] | None = None,
    shutdown: asyncio.Event | None = None,
) -> list[asyncio.Task]:
    shutdown = shutdown if shutdown is not None else asyncio.Event()
    background_tasks: list[asyncio.Task] = []
    await daemon._consume_office_creates(
        _NotificationQueue(shutdown, office_ids),
        config=config,
        containers=None,
        redis_client=None,
        connected=connected if connected is not None else {},
        connecting=connecting if connecting is not None else set(),
        background_tasks=background_tasks,
        shutdown_event=shutdown,
    )
    await asyncio.gather(*background_tasks)
    return background_tasks


@pytest.mark.asyncio
async def test_push_uses_token_discovery_and_complete_office_config(
    monkeypatch: pytest.MonkeyPatch, config: Config,
) -> None:
    requests: list[httpx.Request] = []
    mounts = [{
        "host_path": "/test/extra",
        "container_path": "/extra",
        "read_only": True,
    }]

    def _discovery(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "id": "new-office",
            "name": "Current office name",
            "workspace_slug": "original-stable-slug",
            "extra_mounts": mounts,
            "container_cpus": 6,
            "container_memory": "12g",
        }])

    _mock_http_discovery(monkeypatch, _discovery)
    connect = AsyncMock()
    monkeypatch.setattr(daemon, "_connect_office_process_model", connect)

    await _consume(config, ["new-office"])

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://platform.example/api/communicator/offices"
    assert requests[0].headers["Authorization"] == "Bearer cbcl_co_test_token_a"
    connect.assert_awaited_once()
    office = connect.await_args.args[0]
    assert office == OfficeConfig(
        id="new-office",
        name="Current office name",
        workspace_slug="original-stable-slug",
        extra_mounts=mounts,
        container_cpus=6,
        container_memory="12g",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("discovered", [[], [{"id": "assigned-to-a", "name": "A"}]])
async def test_push_for_unassigned_or_foreign_token_office_does_not_start_setup(
    monkeypatch: pytest.MonkeyPatch, config: Config, discovered: list[dict],
) -> None:
    _mock_http_discovery(
        monkeypatch, lambda request: httpx.Response(200, json=discovered),
    )
    connect = AsyncMock()
    monkeypatch.setattr(daemon, "_connect_office_process_model", connect)
    connecting: set[str] = set()

    background_tasks = await _consume(config, ["assigned-to-b"], connecting=connecting)

    connect.assert_not_awaited()
    assert background_tasks == []
    assert connecting == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 503, None])
async def test_failed_discovery_does_not_start_setup_or_block_subsequent_retry(
    monkeypatch: pytest.MonkeyPatch, config: Config, status_code: int | None,
) -> None:
    failed = OfficeConfig(id="retry-later", name="Retry later")
    good = OfficeConfig(id="good", name="Good")
    response = httpx.Response(
        status_code or 503, request=httpx.Request("GET", "https://platform.example"),
    )
    discovery_error = (
        httpx.ReadTimeout("Discovery timed out", request=response.request)
        if status_code is None else httpx.HTTPStatusError(
            "Discovery unavailable", request=response.request, response=response,
        )
    )
    discovery = AsyncMock(side_effect=[
        discovery_error,
        [failed, good],
        [failed, good],
    ])
    monkeypatch.setattr(daemon, "fetch_offices", discovery)
    connect = AsyncMock()
    monkeypatch.setattr(daemon, "_connect_office_process_model", connect)
    connecting: set[str] = set()

    await _consume(config, [failed.id, good.id, failed.id], connecting=connecting)

    assert discovery.await_args_list == [
        call(config.platform_url, config.security_token),
    ] * 3
    assert [args.args[0] for args in connect.await_args_list] == [good, failed]
    assert connecting == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_state", ["connected", "connecting"])
async def test_poll_connection_started_during_discovery_is_not_duplicated(
    monkeypatch: pytest.MonkeyPatch, config: Config, poll_state: str,
) -> None:
    office = OfficeConfig(id="new-office", name="New office")
    connected: dict = {}
    connecting: set[str] = set()

    async def _discovery(*args: object) -> list[OfficeConfig]:
        if poll_state == "connected":
            connected[office.id] = object()
        else:
            connecting.add(office.id)
        return [office]

    monkeypatch.setattr(daemon, "fetch_offices", _discovery)
    connect = AsyncMock()
    monkeypatch.setattr(daemon, "_connect_office_process_model", connect)

    background_tasks = await _consume(
        config, [office.id], connected=connected, connecting=connecting,
    )

    connect.assert_not_awaited()
    assert background_tasks == []


@pytest.mark.asyncio
async def test_shutdown_during_discovery_does_not_start_an_office(
    monkeypatch: pytest.MonkeyPatch, config: Config,
) -> None:
    shutdown = asyncio.Event()
    office = OfficeConfig(id="new-office", name="New office")

    async def _discovery(*args: object) -> list[OfficeConfig]:
        shutdown.set()
        return [office]

    monkeypatch.setattr(daemon, "fetch_offices", _discovery)
    connect = AsyncMock()
    monkeypatch.setattr(daemon, "_connect_office_process_model", connect)

    background_tasks = await _consume(config, [office.id], shutdown=shutdown)

    connect.assert_not_awaited()
    assert background_tasks == []
