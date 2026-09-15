"""One logical backend invocation retains its identity across transport retries."""

from unittest.mock import AsyncMock, MagicMock
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
async def test_proxy_retries_share_invocation_identity_without_direct_fallback(monkeypatch):
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
    assert all(url == "http://proxy.invalid/tool-call" for url, _payload in session.requests)
    assert str(UUID(identities[0])) == identities[0]
    await backend._call_backend("create_task", {"title": "Build"})
    assert session.requests[-1][1]["_caller"]["invocation_id"] != identities[0]


@pytest.mark.parametrize("status", [401, 403, 409, 423])
async def test_proxy_refusal_never_falls_back_or_retries(monkeypatch, status):
    session = Session([status, 200])
    monkeypatch.setattr(backend, "TOOL_PROXY_URL", "http://proxy.invalid")
    monkeypatch.setattr(backend, "OFFICE_TOOL_SECRET", "must-not-use")
    monkeypatch.setattr(backend, "_get_session", AsyncMock(return_value=session))
    result = await backend._call_backend("create_task", {"title": "Build"})
    assert result["error"] is True
    assert len(session.requests) == 1
    assert session.requests[0][0] == "http://proxy.invalid/tool-call"


async def test_unreachable_proxy_never_uses_direct_backend(monkeypatch):
    session = MagicMock()
    session.post.side_effect = ConnectionError("proxy unavailable")
    monkeypatch.setattr(backend, "TOOL_PROXY_URL", "http://proxy.invalid")
    monkeypatch.setattr(backend, "OFFICE_TOOL_SECRET", "must-not-use")
    monkeypatch.setattr(backend, "_get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(backend.asyncio, "sleep", AsyncMock())
    result = await backend._call_backend("create_task", {"title": "Build"})
    assert result["error"] is True
    assert session.post.call_count == 3
    assert all(call.args[0] == "http://proxy.invalid/tool-call" for call in session.post.call_args_list)


def test_unclaimed_consult_does_not_advertise_a_nonexistent_task_attempt(monkeypatch):
    monkeypatch.setenv("CUBICLE_EXECUTION_ATTEMPT_ID", "host-session")
    monkeypatch.setenv("CUBICLE_EXECUTION_GENERATION", "0")
    monkeypatch.setattr(backend, "AGENT_NAME", "planner")
    monkeypatch.setattr(backend, "TASK_MODE", "execute")
    caller = backend._caller_envelope()
    assert caller["agent_name"] == "planner"
    assert caller["role"] == "worker"
    assert "attempt_id" not in caller


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
