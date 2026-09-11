"""One logical backend invocation retains its identity across transport retries."""

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src._agent_image import _mcp_backend as backend


class Response:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self):
        return "response unavailable"

    async def json(self):
        return {"id": "task-a"}


class Session:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.requests = []

    def post(self, url, *, json, headers):
        self.requests.append((url, json))
        return Response(next(self.statuses))


@pytest.mark.asyncio
async def test_proxy_direct_retries_share_invocation_identity(monkeypatch):
    session = Session([502, 503, 200, 200])
    monkeypatch.setattr(backend, "TOOL_PROXY_URL", "http://proxy.invalid")
    monkeypatch.setattr(backend, "_get_session", AsyncMock(return_value=session))
    first = await backend._call_backend("create_task", {"title": "Build"})
    assert first == {"id": "task-a"}
    identities = [
        payload["_caller"]["invocation_id"] for _, payload in session.requests
    ]
    assert len(identities) == 3
    assert len(set(identities)) == 1
    assert str(UUID(identities[0])) == identities[0]
    await backend._call_backend("create_task", {"title": "Build"})
    assert session.requests[-1][1]["_caller"]["invocation_id"] != identities[0]


@pytest.mark.asyncio
async def test_direct_only_retries_keep_same_identity(monkeypatch):
    session = Session([502, 503, 200])
    monkeypatch.setattr(backend, "TOOL_PROXY_URL", "")
    monkeypatch.setattr(backend, "_get_session", AsyncMock(return_value=session))
    await backend._call_backend("create_task", {"title": "Build"})
    assert (
        len({payload["_caller"]["invocation_id"] for _, payload in session.requests})
        == 1
    )
