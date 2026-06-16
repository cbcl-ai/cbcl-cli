"""T8.1.3 / T8.1.6 — disconnect fails pending RPC futures with ConnectionError
(not CancelledError) and drains in-flight handler tasks."""
from __future__ import annotations

import asyncio
import pytest

from src.connection.ws_client import PlatformWSClient, _redact_token


def test_redact_token_strips_company_token_from_invalid_uri_text():
    # T8.3.6c: an InvalidURI (WebSocketException) embeds the full connect URI,
    # which carries the Company Token. The protocol-error WARNING branch logs
    # via _redact_token — assert the token never survives, incl. the websockets
    # InvalidURI message shape.
    raw = "ws://host:8000/ws/connector/off-1?token=cbcl_co_SECRET123 isn't a valid URI"
    redacted = _redact_token(raw)
    assert "cbcl_co_SECRET123" not in redacted
    assert "token=***" in redacted

    class _FakeInvalidURI(Exception):
        def __str__(self) -> str:
            return "ws://h?token=cbcl_co_LEAK invalid"

    assert "cbcl_co_LEAK" not in _redact_token(_FakeInvalidURI())


@pytest.mark.asyncio
async def test_mark_disconnected_fails_future_with_connection_error():
    c = PlatformWSClient("http://x", "off-1")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    c._pending_requests["r1"] = fut
    c._mark_disconnected()
    assert fut.done()
    with pytest.raises(ConnectionError):
        fut.result()  # NOT CancelledError — catchable by except Exception


@pytest.mark.asyncio
async def test_disconnect_drains_handler_tasks():
    c = PlatformWSClient("http://x", "off-1")

    async def _slow():
        await asyncio.sleep(3600)

    t = asyncio.create_task(_slow())
    c._handler_tasks.add(t)
    await c.disconnect()
    assert t.cancelled() or t.done()
    assert not c._handler_tasks


@pytest.mark.asyncio
async def test_disconnect_fails_futures_with_connection_error():
    c = PlatformWSClient("http://x", "off-1")
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    c._pending_requests["r1"] = fut
    await c.disconnect()
    with pytest.raises(ConnectionError):
        fut.result()
