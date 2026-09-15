import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.manager_controller import ManagerController


@pytest.fixture
def controller():
    router = MagicMock()
    router.ws_client.request = AsyncMock(return_value={"claimed": True})
    instance = ManagerController(MagicMock(), router, MagicMock(), MagicMock())
    instance._handle_chat_message_locked = AsyncMock(return_value=True)
    return instance


@pytest.mark.asyncio
async def test_durable_turn_requires_claim_and_records_completion(controller):
    turn_id = str(uuid.uuid4())
    assert await controller.handle_chat_message({"turn_id": turn_id}) is True
    requests = controller._router.ws_client.request.call_args_list
    assert requests[0].args[0] == "claim_chat_turn"
    assert requests[1].args[0] == "finish_chat_turn"
    assert requests[0].args[1]["claim_token"] == requests[1].args[1]["claim_token"]
    assert requests[1].args[1]["success"] is True
    controller._handle_chat_message_locked.assert_awaited_once()
    assert not controller._claimed_chat_turns


@pytest.mark.asyncio
async def test_rejected_duplicate_never_starts_manager(controller):
    controller._router.ws_client.request.return_value = {
        "claimed": False,
        "state": "completed",
    }
    assert await controller.handle_chat_message({"turn_id": str(uuid.uuid4())}) is False
    controller._handle_chat_message_locked.assert_not_awaited()
    assert controller._router.ws_client.request.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_redelivery_is_not_queued_twice(controller):
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(message):
        started.set()
        await release.wait()
        return True

    controller._handle_chat_message_locked.side_effect = execute
    message = {"turn_id": str(uuid.uuid4())}
    first = asyncio.create_task(controller.handle_chat_message(message))
    await started.wait()
    assert await controller.handle_chat_message(message) is False
    release.set()
    assert await first is True
    controller._handle_chat_message_locked.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_ack_retry_preserves_claim_identity(controller):
    controller._router.ws_client.request.side_effect = [
        TimeoutError(),
        {"claimed": True},
        {"finished": True},
    ]
    with patch("src.orchestrator.manager_controller.asyncio.sleep", new=AsyncMock()):
        assert (
            await controller.handle_chat_message({"turn_id": str(uuid.uuid4())}) is True
        )
    requests = controller._router.ws_client.request.call_args_list
    assert requests[0].args[1] == requests[1].args[1]
    controller._handle_chat_message_locked.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_claim_outcome_never_executes(controller):
    controller._router.ws_client.request.side_effect = TimeoutError()
    with patch("src.orchestrator.manager_controller.asyncio.sleep", new=AsyncMock()):
        assert (
            await controller.handle_chat_message({"turn_id": str(uuid.uuid4())})
            is False
        )
    controller._handle_chat_message_locked.assert_not_awaited()
    assert not controller._claimed_chat_turns


@pytest.mark.asyncio
async def test_execution_error_records_failure_without_replay(controller):
    controller._handle_chat_message_locked.side_effect = RuntimeError(
        "execution failed"
    )
    with pytest.raises(RuntimeError):
        await controller.handle_chat_message({"turn_id": str(uuid.uuid4())})
    assert (
        controller._router.ws_client.request.call_args_list[-1].args[1]["success"]
        is False
    )
    assert not controller._claimed_chat_turns


@pytest.mark.asyncio
async def test_legacy_and_script_messages_keep_existing_contract(controller):
    assert await controller.handle_chat_message({"conversation_id": "legacy"}) is True
    assert (
        await controller.handle_chat_message(
            {"turn_id": "not-a-user-turn"}, source="script"
        )
        is True
    )
    controller._router.ws_client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_turn_is_not_claimed_until_execution_lock_available(controller):
    await controller._chat_lock.acquire()
    message = {"turn_id": str(uuid.uuid4())}
    waiting = asyncio.create_task(controller.handle_chat_message(message))
    await asyncio.sleep(0)
    controller._router.ws_client.request.assert_not_awaited()
    assert await controller.handle_chat_message(message) is False
    controller._chat_lock.release()
    assert await waiting is True
    controller._handle_chat_message_locked.assert_awaited_once()


def test_ws_client_advertises_durable_turn_protocol():
    from src.connection.ws_client import PlatformWSClient
    from urllib.parse import parse_qs, urlparse

    client = PlatformWSClient("https://example.test", "office", "synthetic-token")
    assert parse_qs(urlparse(client.url).query) == {
        "token": ["synthetic-token"],
        "chat_turns_v1": ["1"],
        "task_stop_v1": ["1"],
        "task_stop_v2": ["1"],
        "manager_turn_control_v1": ["1"],
        "flow_activations_v1": ["1"],
        "office_files_v1": ["1"],
        "generation_readonly_v1": ["1"],
        "transient_inputs_v1": ["1"],
    }
    legacy_auth = PlatformWSClient("http://example.test", "office")
    assert "token" not in parse_qs(urlparse(legacy_auth.url).query)
