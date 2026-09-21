"""Authoritative empty discovery and unavailable discovery are different states."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from src import config as config_module, daemon
from src.config import Config


class PollWindow:
    """Give the real poll loop a finite number of ticks without waiting."""

    def __init__(self, ticks):
        self.ticks = ticks

    def is_set(self):
        return False

    async def wait(self):
        if self.ticks:
            self.ticks -= 1
            raise asyncio.TimeoutError


def discovery_http(monkeypatch, responses):
    responses = iter(responses)
    calls = []
    original = httpx.AsyncClient

    def respond(request):
        calls.append(request)
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(daemon, "_mark_token_revoked", Mock())
    monkeypatch.setattr(daemon, "_clear_token_revoked", Mock())
    return calls


async def poll(monkeypatch, responses, *, connected=None):
    connected = {"last-office": object()} if connected is None else connected
    calls = discovery_http(monkeypatch, responses)

    async def disconnect(office_id, current, *_args, **kwargs):
        assert kwargs.get("delete_workspace", False) is False
        current.pop(office_id)

    disconnect_mock = AsyncMock(side_effect=disconnect)
    connect = Mock()
    monkeypatch.setattr(daemon, "_disconnect_office_process_model", disconnect_mock)
    monkeypatch.setattr(daemon, "_spawn_office_connect", connect)
    await daemon._poll_for_new_offices_process_model(
        Config(platform_url="https://platform.example", security_token="synthetic"),
        None, None, connected, set(), [], PollWindow(len(responses)),
    )
    return connected, calls, disconnect_mock, connect


async def test_successful_empty_snapshot_disconnects_last_office_once(monkeypatch):
    connected, calls, disconnect, connect = await poll(
        monkeypatch, [httpx.Response(200, json=[]) for _ in range(3)],
    )
    assert len(calls) == 3
    assert connected == {}, "Repeated authoritative emptiness must not retain the last office forever"
    disconnect.assert_awaited_once()
    connect.assert_not_called()


@pytest.mark.parametrize("response", [
    httpx.Response(401), httpx.Response(503), httpx.ReadTimeout("synthetic outage"),
    httpx.Response(200, json={}), httpx.Response(200, json=""),
    httpx.Response(200, json=None), httpx.Response(200, json=[{"id": "", "name": "Invalid"}]),
    httpx.Response(200, json=[{"id": "office", "name": None}]),
    httpx.Response(200, json=[{"id": "office", "name": "A"}, {"id": "office", "name": "B"}]),
])
async def test_unavailable_or_invalid_discovery_preserves_connected_offices(monkeypatch, response):
    connected, _, disconnect, connect = await poll(monkeypatch, [response, response])
    assert set(connected) == {"last-office"}
    disconnect.assert_not_awaited()
    connect.assert_not_called()


async def test_empty_after_outage_disconnects_and_subsequent_assignment_connects(monkeypatch):
    connected, calls, disconnect, connect = await poll(monkeypatch, [
        httpx.Response(503),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[{
            "id": "new-office", "name": "New office", "workspace_slug": "pinned",
            "extra_mounts": [], "container_cpus": 4, "unknown_future_field": True,
        }]),
    ])
    assert connected == {}
    disconnect.assert_awaited_once()
    connect.assert_called_once()
    office = connect.call_args.args[0]
    assert office.id == "new-office"
    assert office.slug == "pinned"
    assert office.container_cpus == 4
    assert all(call.headers["Authorization"] == "Bearer synthetic" for call in calls)


@pytest.mark.parametrize("already_present", [False, True])
async def test_older_snapshot_does_not_disconnect_an_office_connected_during_fetch(monkeypatch, already_present):
    connected = {"new-office": object()} if already_present else {}
    newer_instance = object()

    async def discovered(*_args):
        # The create/delete consumer finishes a newer connection while the
        # earlier HTTP inventory request is still awaiting its response.
        connected["new-office"] = newer_instance
        return []

    monkeypatch.setattr(daemon, "_discover_offices", discovered)
    disconnect = AsyncMock()
    monkeypatch.setattr(daemon, "_disconnect_office_process_model", disconnect)
    await daemon._poll_for_new_offices_process_model(
        Config(platform_url="https://platform.example", security_token="synthetic"),
        None, None, connected, set(), [], PollWindow(1),
    )
    disconnect.assert_not_awaited()
    assert connected["new-office"] is newer_instance


@pytest.mark.parametrize("payload", [{}, "", None, [None], [{"id": "x", "name": ""}]])
def test_sync_discovery_rejects_incomplete_snapshot(monkeypatch, payload):
    response = httpx.Response(200, content=json.dumps(payload), request=httpx.Request("GET", "https://platform.example"))
    monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    with pytest.raises(ValueError, match="discovery"):
        config_module.fetch_offices_sync("https://platform.example", "synthetic")
