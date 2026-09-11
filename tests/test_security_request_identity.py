from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src._handlers import _requests
from src.office_runtime import RuntimeStorageError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", [*_requests._GENERATION_ACTIONS, "auth_status", "cli_upgrade"]
)
@pytest.mark.parametrize("available", [True, False])
async def test_protected_request_resolves_office_identity_before_execution(
    monkeypatch, action, available
):
    office_id = "d4ff6b75-4e82-4a72-88dd-c82478c1d815"
    container_id = "a" * 64
    resolver = AsyncMock(return_value=container_id)
    if not available:
        resolver.side_effect = RuntimeStorageError("Synthetic private runtime mismatch")
    implementation = AsyncMock()
    monkeypatch.setattr("src.office_runtime.resolve_office_container_id", resolver)
    monkeypatch.setattr(_requests, "_dispatch_backend_request_impl", implementation)
    sender = AsyncMock()
    await _requests.dispatch_backend_request(
        {"action": action, "request_id": "synthetic-request"},
        router=SimpleNamespace(ws_client=SimpleNamespace(send=sender)),
        fs_handler=None,
        office=SimpleNamespace(id=office_id),
        redis_client=None,
        container_name="synthetic-office",
    )
    resolver.assert_awaited_once_with(office_id, "synthetic-office")
    if available:
        assert implementation.await_args.kwargs["container_name"] == container_id
        sender.assert_not_awaited()
    else:
        implementation.assert_not_awaited()
        sender.assert_awaited_once()
        assert sender.await_args.args[0]["data"]["status"] == 503


@pytest.mark.asyncio
async def test_auth_policy_failure_is_not_reported_as_invalid_credentials(monkeypatch):
    from src._setup_cli import GenerationPolicyError

    monkeypatch.setattr(
        "src.office_runtime.resolve_office_container_id",
        AsyncMock(return_value="a" * 64),
    )
    monkeypatch.setattr(
        _requests,
        "_dispatch_backend_request_impl",
        AsyncMock(side_effect=GenerationPolicyError("Upgrade the office image.")),
    )
    sender = AsyncMock()
    await _requests.dispatch_backend_request(
        {"action": "auth_status", "request_id": "synthetic-request"},
        router=SimpleNamespace(ws_client=SimpleNamespace(send=sender)),
        fs_handler=None,
        office=SimpleNamespace(id="d4ff6b75-4e82-4a72-88dd-c82478c1d815"),
        redis_client=None,
        container_name="synthetic-office",
    )
    result = sender.await_args.args[0]["data"]
    assert result == {"error": "Upgrade the office image.", "status": 503}
