"""Manager context recovery survives rotation, queues and background turns."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from src.config_sync.sync_service import ConfigStore
from src.orchestrator.manager_controller import ManagerController
from src.orchestrator.session_manager import SessionManager


@pytest.fixture
async def runtime(tmp_path):
    workstream = str(uuid.uuid4())
    key = "workstream:" + workstream
    context = {
        "workstream_id": workstream,
        "workstream_name": "Current project",
        "work_mode": "default",
        "chat_history": "[USER] We agreed on a blue website.\n[ASSISTANT] Blue is confirmed.",
        "task_summary": {"done": 3},
    }

    async def request(action, params, **kwargs):
        assert action == "get_manager_context"
        assert params["context_key"] == key
        data = dict(context)
        if not params["include_history"]:
            data.pop("chat_history")
        return {"context_key": key, "context_data": data}

    router = SimpleNamespace(
        ws_client=SimpleNamespace(request=AsyncMock(side_effect=request)),
        publish_event=AsyncMock(),
    )
    sessions = SessionManager(workspace_path=str(tmp_path))
    await sessions.init()
    config = ConfigStore()
    config.office_config = {"manager_model": "claude-opus-4-7"}
    supervisor = SimpleNamespace(
        send_chat_to_manager=AsyncMock(), _send_to_agent=AsyncMock(),
    )
    controller = ManagerController(supervisor, router, sessions, config)

    async def send(msg):
        await controller._on_response_final({
            "conversation_id": msg["conversation_id"], "context_key": key,
            "session_id": "session-blue",
        })

    supervisor.send_chat_to_manager.side_effect = send
    return SimpleNamespace(
        controller=controller, sessions=sessions, router=router,
        supervisor=supervisor, key=key, context=context,
    )


def message(runtime, **extras):
    return {
        "context_key": runtime.key, "conversation_id": "board-event-1",
        "user_message": "Check the current board.", "context_data": {},
        **extras,
    }


async def test_background_first_turn_bootstraps_once_then_human_resumes(runtime):
    first = message(runtime, _turn_outcome={})
    assert await runtime.controller.handle_chat_message(first, source="script")
    sent = runtime.supervisor.send_chat_to_manager.await_args.args[0]
    assert sent["content"].count("We agreed on a blue website") == 1
    assert sent["content"].endswith(first["user_message"])
    assert "blue website" not in sent["system_prompt"]
    assert "blue website" not in first["user_message"]

    assert await runtime.controller.handle_chat_message(message(
        runtime, user_message="Continue with our agreed color.",
        conversation_id=str(uuid.uuid4()),
    ))
    resumed = runtime.supervisor.send_chat_to_manager.await_args.args[0]
    assert resumed["session_id"] == "session-blue"
    assert resumed["content"] == "Continue with our agreed color."
    assert "blue website" not in resumed["system_prompt"]
    assert [c.args[1]["include_history"] for c in runtime.router.ws_client.request.await_args_list] == [True, False]


async def test_rotation_reloads_history_for_next_background_turn(runtime):
    async def rotate(msg):
        await runtime.controller._on_response_final({
            "conversation_id": msg["conversation_id"], "context_key": runtime.key,
            "session_id": "large-session", "rotate_session": True,
        })

    runtime.supervisor.send_chat_to_manager.side_effect = rotate
    assert await runtime.controller.handle_chat_message(message(runtime))
    assert runtime.sessions.get_session_id(runtime.key) is None
    runtime.context["chat_history"] += "\n[USER] Use the approved logo too."
    assert await runtime.controller.handle_chat_message(message(runtime), source="script")
    assert "approved logo" in runtime.supervisor.send_chat_to_manager.await_args.args[0]["content"]
    assert all(c.args[1]["include_history"] for c in runtime.router.ws_client.request.await_args_list)


@pytest.mark.parametrize("failure", ["rpc", "wrong_context", "wrong_workstream", "missing_history"])
async def test_unavailable_recovery_does_not_start_or_reset_session(runtime, failure):
    if failure == "rpc":
        runtime.router.ws_client.request.side_effect = RuntimeError("offline")
    else:
        response = {"context_key": runtime.key, "context_data": dict(runtime.context)}
        if failure == "wrong_context":
            response["context_key"] = "general_chat"
        elif failure == "wrong_workstream":
            response["context_data"]["workstream_id"] = str(uuid.uuid4())
        else:
            response["context_data"].pop("chat_history")
        runtime.router.ws_client.request.side_effect = None
        runtime.router.ws_client.request.return_value = response
    runtime.sessions.clear_session = AsyncMock()
    turn = message(runtime, _turn_outcome={})
    assert not await runtime.controller.handle_chat_message(turn, source="script")
    runtime.supervisor.send_chat_to_manager.assert_not_awaited()
    runtime.sessions.clear_session.assert_not_awaited()
    assert turn["_turn_outcome"]["safe_to_retry"] is True
    assert runtime.controller._active_conversation_id is None
    assert any("No new work was started" in c.args[0].get("content", "")
               for c in runtime.router.publish_event.await_args_list)


async def test_authoritative_context_replaces_snapshot_but_preserves_choice(runtime):
    turn = message(runtime, context_data={
        "work_mode": "program", "workstream_name": "Obsolete",
        "choice_superseded": True, "choice_handoff_note": "Accepted replacement",
    })
    assert await runtime.controller.handle_chat_message(turn)
    sent = runtime.supervisor.send_chat_to_manager.await_args.args[0]
    assert sent["context_data"]["work_mode"] == "default"
    assert sent["context_data"]["workstream_name"] == "Current project"
    assert sent["context_data"]["choice_superseded"] is True
    assert sent["context_data"]["choice_handoff_note"] == "Accepted replacement"
    assert turn["context_data"]["workstream_name"] == "Obsolete"


async def test_context_refresh_after_lock_observes_previous_turn_updates(runtime):
    original_send = runtime.supervisor.send_chat_to_manager.side_effect

    async def send(msg):
        runtime.context["task_summary"] = {"done": 4}
        await original_send(msg)

    runtime.supervisor.send_chat_to_manager.side_effect = send
    await asyncio.gather(
        runtime.controller.handle_chat_message(message(runtime)),
        runtime.controller.handle_chat_message(message(runtime, conversation_id="next")),
    )
    second = runtime.supervisor.send_chat_to_manager.await_args_list[1].args[0]
    assert second["context_data"]["task_summary"] == {"done": 4}


async def test_timed_out_cli_drains_before_context_and_session_selection(runtime):
    runtime.supervisor.manager_turn_active = True

    async def drain(timeout):
        assert timeout == 10
        runtime.router.ws_client.request.assert_not_awaited()
        runtime.supervisor.send_chat_to_manager.assert_not_awaited()
        # The previous turn finalizes while the queued turn waits.
        await runtime.sessions.save_session(runtime.key, "previous-finished")
        runtime.context["task_summary"] = {"done": 7}
        runtime.supervisor.manager_turn_active = False

    runtime.supervisor.wait_for_manager_turn = AsyncMock(side_effect=drain)
    assert await runtime.controller.handle_chat_message(message(runtime))
    sent = runtime.supervisor.send_chat_to_manager.await_args.args[0]
    assert sent["session_id"] == "previous-finished"
    assert sent["context_data"]["task_summary"] == {"done": 7}
    assert runtime.router.ws_client.request.await_args.args[1]["include_history"] is False


async def test_undrained_previous_cli_preserves_session_and_reports_input_not_started(runtime):
    await runtime.sessions.save_session(runtime.key, "healthy-session")
    runtime.supervisor.manager_turn_active = True
    runtime.supervisor.wait_for_manager_turn = AsyncMock(side_effect=asyncio.TimeoutError)
    turn = message(runtime, _turn_outcome={})
    assert not await runtime.controller.handle_chat_message(turn, source="script")
    runtime.router.ws_client.request.assert_not_awaited()
    runtime.supervisor.send_chat_to_manager.assert_not_awaited()
    assert runtime.sessions.get_session_id(runtime.key) == "healthy-session"
    assert turn["_turn_outcome"]["safe_to_retry"] is True
    assert runtime.controller._active_conversation_id is None
    assert any("new message has not started" in c.args[0].get("content", "")
               for c in runtime.router.publish_event.await_args_list)
    state = runtime.router.publish_event.await_args.args[0]
    assert state["type"] == "manager_state"
    assert state["state"] == "idle"
    assert state["conversation_id"] == turn["conversation_id"]


async def test_ambiguous_manager_send_is_not_automatically_replayed(runtime):
    runtime.supervisor.send_chat_to_manager.side_effect = RuntimeError(
        "Manager message delivery could not be confirmed. Earlier actions may have completed."
    )
    turn = message(runtime, _turn_outcome={})
    assert not await runtime.controller.handle_chat_message(turn, source="script")
    runtime.supervisor.send_chat_to_manager.assert_awaited_once()
    assert turn["_turn_outcome"]["safe_to_retry"] is False


async def test_cancel_during_context_refresh_never_starts_cli(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"context_key": runtime.key, "context_data": runtime.context}

    runtime.router.ws_client.request.side_effect = request
    turn = message(runtime)
    task = asyncio.create_task(runtime.controller.handle_chat_message(turn))
    await entered.wait()
    result = await runtime.controller.cancel_current_turn(turn)
    assert result["status"] == "requested"
    release.set()
    assert not await task
    runtime.supervisor.send_chat_to_manager.assert_not_awaited()
    assert runtime.controller._turn_cancelled


async def test_crash_does_not_clear_pending_destination_session(runtime, monkeypatch):
    destination = "workstream:" + str(uuid.uuid4())
    await runtime.sessions.save_session(runtime.key, "failed-session")
    await runtime.sessions.save_session(destination, "healthy-session")
    runtime.controller._active_context_key = runtime.key
    runtime.controller._pending_context_switch = destination
    runtime.controller._spawn_manager = AsyncMock(return_value=True)
    monkeypatch.setattr("src.orchestrator.manager_controller.MANAGER_RESTART_DELAY", 0)
    await runtime.controller.handle_manager_crash(1)
    assert runtime.sessions.get_session_id(runtime.key) is None
    assert runtime.sessions.get_session_id(destination) == "healthy-session"
    assert runtime.controller._active_context_key == destination


async def test_scheduled_restart_pins_failed_context_before_turn_finally(runtime, monkeypatch):
    destination = "workstream:" + str(uuid.uuid4())
    await runtime.sessions.save_session(runtime.key, "failed-session")
    await runtime.sessions.save_session(destination, "healthy-session")
    runtime.controller._active_context_key = runtime.key
    runtime.controller._spawn_manager = AsyncMock(return_value=True)
    monkeypatch.setattr("src.orchestrator.manager_controller.MANAGER_RESTART_DELAY", 0)
    runtime.controller._schedule_restart("fatal")
    # The completed turn can apply sidebar navigation before the background
    # restart task gets its first scheduling opportunity.
    runtime.controller._active_context_key = destination
    await runtime.controller._restart_task
    assert runtime.sessions.get_session_id(runtime.key) is None
    assert runtime.sessions.get_session_id(destination) == "healthy-session"


async def test_queued_user_history_cutoff_uses_durable_turn_id(runtime):
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    async def request(action, params, **kwargs):
        if action == "claim_chat_turn":
            return {"claimed": True}
        if action == "finish_chat_turn":
            return {}
        assert action == "get_manager_context"
        assert params["turn_id"] in (first, second)
        assert "exclude_message_id" not in params
        data = dict(runtime.context)
        if params["include_history"]:
            # Backend's strict cutoff excludes this and future queued messages.
            data["chat_history"] = "[USER] Use our existing repository."
        else:
            data.pop("chat_history")
        return {"context_key": runtime.key, "context_data": data}

    runtime.router.ws_client.request.side_effect = request
    await asyncio.gather(
        runtime.controller.handle_chat_message(message(
            runtime, turn_id=first, conversation_id=str(uuid.uuid4()),
            user_message="First: inspect the repository.",
        )),
        runtime.controller.handle_chat_message(message(
            runtime, turn_id=second, conversation_id=str(uuid.uuid4()),
            user_message="Second: draft the layout.",
        )),
    )
    calls = runtime.supervisor.send_chat_to_manager.await_args_list
    assert len(calls) == 2
    assert "Second:" not in calls[0].args[0]["content"]
    assert calls[0].args[0]["content"].count("First:") == 1
    assert calls[1].args[0]["content"] == "Second: draft the layout."
