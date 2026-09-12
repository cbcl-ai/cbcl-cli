"""Identity-safe text, cancellation, and compact Manager communication."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src._manager_text_stream import ManagerTextStream
from src.agent_worker import AgentWorker
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
from src.orchestrator.manager_controller import ManagerController
from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_worker import get_worker_subcatalog


def stream_text(stream, message_id, text):
    stream.incremental({"type": "message_start", "message": {"id": message_id}})
    stream.incremental(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}
    )
    return stream.incremental(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    )


def assistant(message_id, text):
    return {"id": message_id, "content": [{"type": "text", "text": text}]}


def test_complete_frame_after_partial_does_not_duplicate():
    stream = ManagerTextStream()
    assert stream_text(stream, "message-1", "Build ready.") == "Build ready."
    assert stream.complete(assistant("message-1", "Build ready.")) == []
    assert stream.complete(assistant("message-1", "Build ready.")) == []


def test_full_only_message_after_partial_is_not_lost():
    stream = ManagerTextStream()
    stream_text(stream, "message-1", "Checking.")
    stream.complete(assistant("message-1", "Checking."))
    assert stream.complete(assistant("message-2", "Ready.")) == ["\n\nReady."]


def test_identical_text_from_distinct_messages_is_preserved():
    stream = ManagerTextStream()
    assert stream.complete(assistant("message-1", "Ready.")) == ["Ready."]
    assert stream.complete(assistant("message-2", "Ready.")) == ["\n\nReady."]


def test_complete_frame_recovers_missing_tail():
    stream = ManagerTextStream()
    stream_text(stream, "message-1", "Build")
    assert stream.complete(assistant("message-1", "Build ready.")) == [" ready."]


def test_replayed_partial_message_only_adds_new_suffix():
    stream = ManagerTextStream()
    stream_text(stream, "message-1", "Build")
    assert stream_text(stream, "message-1", "Build ready.") == " ready."


def test_full_message_does_not_disappear_after_unidentified_partial():
    stream = ManagerTextStream()
    stream.incremental(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}
    )
    assert (
        stream.incremental(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Checking."},
            }
        )
        == "Checking."
    )
    assert stream.complete(assistant("message-next", "Ready.")) == ["\n\nReady."]


def test_partial_without_message_start_is_reconciled_with_full_frame():
    stream = ManagerTextStream()
    stream.incremental(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}
    )
    stream.incremental(
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Ready."},
        }
    )
    assert stream.complete(assistant("message-1", "Ready.")) == []


@pytest.fixture
def controller():
    supervisor = MagicMock(_send_to_agent=AsyncMock())
    router = MagicMock(publish_event=AsyncMock())
    sessions = MagicMock(save_session=AsyncMock(), clear_session=AsyncMock())
    instance = ManagerController(supervisor, router, sessions, MagicMock())
    instance._active_conversation_id = "conversation-a"
    instance._active_context_key = "workstream:a"
    instance._active_turn_id = "turn-a"
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {},
        {"context_key": "workstream:a"},
        {"context_key": "workstream:b", "conversation_id": "conversation-a"},
        {"context_key": "workstream:a", "conversation_id": "conversation-b"},
        {
            "context_key": "workstream:a",
            "conversation_id": "conversation-a",
            "turn_id": "turn-b",
        },
    ],
)
async def test_stale_cancel_cannot_stop_another_turn(controller, message):
    assert await controller.cancel_current_turn(message) == {"status": "stale"}
    controller._supervisor._send_to_agent.assert_not_awaited()
    assert not controller._response_done.is_set()


@pytest.mark.asyncio
async def test_stop_requires_worker_confirmation(controller):
    target = {
        "context_key": "workstream:a",
        "conversation_id": "conversation-a",
        "turn_id": "turn-a",
    }
    assert await controller.cancel_current_turn(target) == {"status": "requested"}
    assert not controller._response_done.is_set()
    assert not controller._turn_cancelled
    assert await controller.cancel_current_turn(target) == {"status": "requested"}
    controller._supervisor._send_to_agent.assert_awaited_once()
    await controller.handle_manager_event(
        "manager", {"type": "response_final", "cancelled": True, **target}
    )
    assert controller._response_done.is_set()
    assert controller._turn_cancelled
    state = controller._router.publish_event.call_args.args[0]
    assert state["state"] == "cancelled"
    assert state["conversation_id"] == "conversation-a"


@pytest.mark.asyncio
async def test_failed_cancel_remains_active_and_honest(controller):
    controller._supervisor._send_to_agent.side_effect = RuntimeError("pipe closed")
    result = await controller.cancel_current_turn(
        {"context_key": "workstream:a", "conversation_id": "conversation-a"}
    )
    assert result == {"status": "unavailable"}
    assert not controller._response_done.is_set()
    assert not controller._turn_cancelled
    assert controller._router.publish_event.call_args.args[0]["state"] == "stuck"


@pytest.mark.asyncio
async def test_late_cancel_completion_cannot_restart_thinking(controller):
    async def complete_during_send(*args):
        controller._response_done.set()

    controller._supervisor._send_to_agent.side_effect = complete_during_send
    result = await controller.cancel_current_turn(
        {"context_key": "workstream:a", "conversation_id": "conversation-a"}
    )
    assert result == {"status": "no_active"}
    controller._router.publish_event.assert_not_called()


@pytest.mark.asyncio
async def test_post_final_chunks_and_session_updates_are_rejected(controller):
    controller._response_done.set()
    await controller._on_response_chunk({"content": "late text"})
    await controller._on_response_final(
        {"session_id": "stale-session", "context_key": "workstream:a"}
    )
    controller._router.publish_event.assert_not_called()
    controller._sessions.save_session.assert_not_called()


@pytest.mark.asyncio
async def test_idle_controller_rejects_late_final(controller):
    controller._active_conversation_id = None
    await controller.handle_manager_event(
        "manager",
        {
            "type": "response_final",
            "conversation_id": "conversation-a",
            "session_id": "stale",
        },
    )
    controller._sessions.save_session.assert_not_called()
    assert not controller._response_done.is_set()


@pytest.mark.asyncio
async def test_pending_user_turns_keep_background_pokes_waiting(controller):
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    finish_first = asyncio.Event()
    finish_second = asyncio.Event()

    async def handle(message):
        if message["conversation_id"] == "first":
            first_started.set()
            await finish_first.wait()
        else:
            second_started.set()
            await finish_second.wait()
        return True

    controller._handle_chat_message_locked = handle
    first = asyncio.create_task(
        controller.handle_chat_message({"conversation_id": "first"})
    )
    await first_started.wait()
    second = asyncio.create_task(
        controller.handle_chat_message({"conversation_id": "second"})
    )
    await asyncio.sleep(0)
    finish_first.set()
    await first
    await second_started.wait()
    assert controller._user_streaming
    assert not controller._user_turn_done.is_set()
    finish_second.set()
    await second
    assert not controller._user_streaming
    assert controller._user_turn_done.is_set()


@pytest.mark.asyncio
async def test_worker_rejects_cancel_for_different_chat():
    worker = AgentWorker(
        role="manager",
        agent_name="manager",
        workspace_path="/workspace",
        office_id="office",
        backend_url="http://backend.invalid",
    )
    worker._current_chat_identity = {
        "context_key": "workstream:a",
        "conversation_id": "conversation-a",
        "turn_id": "turn-a",
    }
    task = asyncio.create_task(asyncio.Event().wait())
    worker._current_session_task = task
    worker._handle_cancel(
        {"context_key": "workstream:b", "conversation_id": "conversation-a"}
    )
    assert not task.cancelling()
    worker._handle_cancel(
        {"context_key": "workstream:a", "conversation_id": "conversation-a"}
    )
    with pytest.raises(asyncio.CancelledError):
        await task


def test_prompt_is_compact_truthful_and_reconciles_existing_work():
    normalized = " ".join(MANAGER_CLAUDE_MD.split())
    assert "Do not invent an ETA" in normalized
    assert "up to three bullets" in normalized
    assert "not permission to start a competing implementation" in normalized
    assert "requested` is NOT `stopped" in normalized
    assert "roughly how long" not in normalized
    assert (
        "immediately"
        not in normalized.split("### User-initiated cancel")[1].split(
            "## General Chat vs Workstream"
        )[0]
    )
    assert "short progress line every few tool calls" not in normalized
    assert "do not emit filler progress lines" in normalized
    MANAGER_CLAUDE_MD.format(office_name="Office", manager_tool_allowlist="stop_task")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_scope_cancellation_notice_is_not_completion(
    controller, monkeypatch, cancelled
):
    from src.orchestrator import _manager_action_requests

    dispatch = AsyncMock()
    monkeypatch.setattr(_manager_action_requests, "_dispatch_poke", dispatch)
    monkeypatch.setattr(
        _manager_action_requests, "build_script_context_data", lambda *args: {}
    )
    await controller.ingest_scope_completed(
        {
            "context_key": "workstream:a",
            "scope_readable_id": "WS-001.S1",
            "scope_name": "Build",
            "task_count": 0,
            "cancelled": cancelled,
        }
    )
    message = dispatch.call_args.args[1]
    if cancelled:
        assert "execution cleanup is confirmed" in message["user_message"]
        assert "not successful delivery" in message["user_message"]
        assert "reconcile the user's cancellation intent" in message["user_message"]
        assert "Scope Completed" not in message["user_message"]
        assert message["conversation_id"].endswith("-cancelled")
    else:
        assert "Scope Completed" in message["user_message"]
        assert "cancelled" not in message["conversation_id"]


def test_stop_tool_is_manager_only():
    manager = {tool["name"]: tool for tool in get_manager_tools()}
    assert manager["stop_task"]["action"] == "stop_task"
    assert manager["stop_task"]["inputSchema"]["required"] == ["task_id"]
    assert "get_task_detail" in manager["stop_task"]["description"]
    assert "get_task " not in manager["stop_task"]["description"]
    assert "never move to blocked" in manager["archive_task"]["description"]
    assert "stop_task" not in {tool["name"] for tool in get_planner_tools()}
    for mode in ("execute", "review", "triage"):
        assert "stop_task" not in {
            tool["name"] for tool in get_worker_subcatalog(mode, "manager-assistant")
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_safe", [False, None])
async def test_ambiguous_failed_poke_is_not_automatically_replayed(
    controller, retry_safe
):
    from src.orchestrator._manager_action_requests import _dispatch_poke

    async def fail(message, source):
        if retry_safe is not None:
            message["_turn_outcome"]["safe_to_retry"] = retry_safe
        return False

    controller.handle_chat_message = AsyncMock(side_effect=fail)
    message = {"conversation_id": "planner-consult-1", "context_key": "workstream:a"}
    assert not await _dispatch_poke(controller, message, retry_on_failure=True)
    assert not getattr(controller, "_pending_pokes", [])
    assert await _dispatch_poke(controller, message, retry_on_failure=True)
    assert controller.handle_chat_message.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_pokes_enter_manager_only_once(controller):
    from src.orchestrator._manager_action_requests import _dispatch_poke

    started = asyncio.Event()
    finish = asyncio.Event()

    async def handle(message, source):
        started.set()
        await finish.wait()
        return True

    controller.handle_chat_message = AsyncMock(side_effect=handle)
    message = {"conversation_id": "planner-consult-1", "context_key": "workstream:a"}
    first = asyncio.create_task(_dispatch_poke(controller, message))
    await started.wait()
    second = asyncio.create_task(_dispatch_poke(controller, message))
    finish.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert controller.handle_chat_message.await_count == 1


@pytest.mark.asyncio
async def test_tool_activity_overrides_false_safe_retry_receipt(controller):
    await controller._on_activity({"activity": "tool_use", "tool": "create_task"})
    await controller._on_error({"message": "lost response", "safe_to_retry": True})
    assert not controller._turn_retry_safe


@pytest.mark.asyncio
async def test_poke_error_explains_reconciliation_not_automatic_replay(controller):
    controller._active_is_poke = True
    await controller._publish_error_response(
        "conversation-a", "workstream:a", "Interrupted."
    )
    content = controller._router.publish_event.call_args.args[0]["content"]
    assert "Automatic replay is paused" in content
    assert "Check the live board" in content


@pytest.mark.asyncio
async def test_error_after_partial_reply_starts_a_separate_paragraph(controller):
    await controller._on_response_chunk({"content": "Review still needs checks."})
    await controller._publish_error_response(
        "conversation-a", "workstream:a", "Execution cleanup failed."
    )
    response = controller._router.publish_event.call_args.args[0]
    assert response["content"] == "\n\nExecution cleanup failed."
    assert response["error"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["scope-one", "action-request-" + "a" * 190])
async def test_background_attempt_identity_fits_legacy_event_column(controller, identifier):
    from src.orchestrator._manager_action_requests import _dispatch_poke

    async def fail_safely(message, source):
        message["_turn_outcome"]["safe_to_retry"] = True
        return False

    controller.handle_chat_message = AsyncMock(side_effect=fail_safely)
    message = {"conversation_id": identifier, "context_key": "workstream:a"}
    assert not await _dispatch_poke(controller, message)
    assert not await _dispatch_poke(controller, message)
    identities = [
        call.args[0]["conversation_id"]
        for call in controller.handle_chat_message.await_args_list
    ]
    assert len(set(identities)) == 2
    assert all(len(identity) <= 64 for identity in identities)
    assert message["conversation_id"] == identifier


@pytest.mark.asyncio
async def test_poke_replay_safety_receipt_is_bound_to_its_locked_turn(controller):
    async def failed_turn(message):
        controller._turn_retry_safe = True
        return False

    controller._handle_chat_message_locked = failed_turn
    outcome = {}
    assert not await controller.handle_chat_message(
        {"_turn_outcome": outcome}, source="script"
    )
    assert outcome == {"safe_to_retry": True}
