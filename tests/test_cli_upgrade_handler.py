"""Regression tests for the cli_upgrade RPC handler (Phase 1 slice 2).

Locks two things that bit us in review:

1. ``AgentSupervisor.active_count`` is a @property — the quiesce guard
   must READ it, not CALL it. Calling an int raises TypeError, which the
   dispatch wrapper swallows WITHOUT a response frame, hanging the RPC.
2. Every path must emit exactly one ``response`` frame (busy, success,
   no-container, and the defensive upgrade-errored path).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src._handlers._requests import dispatch_backend_request
from src.office_runtime import RuntimeStorageError

_OFFICE = SimpleNamespace(id="d4ff6b75-4e82-4a72-88dd-c82478c1d815")
_CONTAINER = "a" * 64


@pytest.fixture(autouse=True)
def office_identity(monkeypatch):
    async def resolve(office_id, container_name):
        assert office_id == _OFFICE.id
        if not container_name:
            raise RuntimeStorageError("Synthetic unavailable container")
        return _CONTAINER

    monkeypatch.setattr("src.office_runtime.resolve_office_container_id", resolve)


class _FakeWsClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)


class _FakeRouter:
    def __init__(self) -> None:
        self.ws_client = _FakeWsClient()


class _FakeSupervisor:
    """Mirrors AgentSupervisor.active_count being a @property, not a method."""

    def __init__(self, active: int) -> None:
        self._active = active

    @property
    def active_count(self) -> int:
        return self._active


def _msg() -> dict:
    return {"action": "cli_upgrade", "request_id": "req-1"}


@pytest.mark.asyncio
async def test_cli_upgrade_refuses_when_busy() -> None:
    router = _FakeRouter()
    await dispatch_backend_request(
        _msg(),
        router=router,
        fs_handler=None,
        office=_OFFICE,
        redis_client=None,
        container_name="cbcl-office-x",
        supervisor=_FakeSupervisor(active=2),
    )
    assert len(router.ws_client.sent) == 1
    data = router.ws_client.sent[0]["data"]
    assert data["ok"] is False
    assert data["busy"] is True
    assert "in progress" in data["message"]


@pytest.mark.asyncio
async def test_cli_upgrade_runs_when_idle() -> None:
    router = _FakeRouter()
    with patch(
        "src.docker.session_bridge.upgrade_cli",
        new=AsyncMock(
            return_value={
                "ok": True,
                "cli_version": "v2",
                "sdk_version": "0.2.0",
                "message": "upgraded",
            }
        ),
    ):
        await dispatch_backend_request(
            _msg(),
            router=router,
            fs_handler=None,
            office=_OFFICE,
            redis_client=None,
            container_name="cbcl-office-x",
            supervisor=_FakeSupervisor(active=0),
        )
    assert len(router.ws_client.sent) == 1
    data = router.ws_client.sent[0]["data"]
    assert data["ok"] is True
    assert data["sdk_version"] == "0.2.0"
    assert data["container_name"] == _CONTAINER


@pytest.mark.asyncio
async def test_cli_upgrade_emits_response_even_when_upgrade_raises() -> None:
    """Defensive: an exception in upgrade_cli must still produce a
    response frame, not hang the RPC."""
    router = _FakeRouter()
    with patch(
        "src.docker.session_bridge.upgrade_cli",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await dispatch_backend_request(
            _msg(),
            router=router,
            fs_handler=None,
            office=_OFFICE,
            redis_client=None,
            container_name="cbcl-office-x",
            supervisor=_FakeSupervisor(active=0),
        )
    assert len(router.ws_client.sent) == 1
    data = router.ws_client.sent[0]["data"]
    assert data["ok"] is False
    assert "errored" in data["message"]


@pytest.mark.asyncio
async def test_cli_upgrade_no_container() -> None:
    router = _FakeRouter()
    await dispatch_backend_request(
        _msg(),
        router=router,
        fs_handler=None,
        office=_OFFICE,
        redis_client=None,
        container_name="",
        supervisor=_FakeSupervisor(active=0),
    )
    assert len(router.ws_client.sent) == 1
    assert router.ws_client.sent[0]["data"]["status"] == 503
