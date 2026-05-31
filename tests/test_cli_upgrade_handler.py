"""Regression tests for the cli_upgrade RPC handler (Phase 1 slice 2).

Locks two things that bit us in review:

1. ``AgentSupervisor.active_count`` is a @property — the quiesce guard
   must READ it, not CALL it. Calling an int raises TypeError, which the
   dispatch wrapper swallows WITHOUT a response frame, hanging the RPC.
2. Every path must emit exactly one ``response`` frame (busy, success,
   no-container, and the defensive upgrade-errored path).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src._handlers._requests import dispatch_backend_request


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
        office=None,
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
        new=AsyncMock(return_value={"ok": True, "cli_version": "v2", "sdk_version": "0.2.0", "message": "upgraded"}),
    ):
        await dispatch_backend_request(
            _msg(),
            router=router,
            fs_handler=None,
            office=None,
            redis_client=None,
            container_name="cbcl-office-x",
            supervisor=_FakeSupervisor(active=0),
        )
    assert len(router.ws_client.sent) == 1
    data = router.ws_client.sent[0]["data"]
    assert data["ok"] is True
    assert data["sdk_version"] == "0.2.0"
    assert data["container_name"] == "cbcl-office-x"


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
            office=None,
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
        office=None,
        redis_client=None,
        container_name="",
        supervisor=_FakeSupervisor(active=0),
    )
    assert len(router.ws_client.sent) == 1
    assert router.ws_client.sent[0]["data"]["ok"] is False
