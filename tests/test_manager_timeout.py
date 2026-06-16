"""Tests for the Manager-timeout remediation cluster (T1.1.5).

Verified findings 07/G10, 03/§5.1, 03/#27: on inactivity timeout the
controller used to ABANDON the wait without killing the wedged
``docker exec claude --print`` child; stale frames from the zombie
turn could falsely terminate the next turn; timeout turns never
counted toward the consecutive-error session reset; and a post-
timeout Cancel click was a no-op.

Covers the four contracted fixes:

  (a) timeout → ``cancel_task`` IPC sent to the Manager subprocess;
  (b) a stale response_final carrying the OLD conversation_id during
      a NEW turn does NOT set ``_response_done`` (a matching-id final
      DOES), and stale frames don't refresh the watchdog;
  (c) N timeout turns trigger the session reset exactly like N error
      turns (including reset-on-success);
  (d) Cancel after a timeout reaches the stray-session kill path.

Mocking patterns mirror ``tests/test_manager_controller.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.manager_controller import (
    MANAGER_AGENT_NAME,
    ManagerController,
)


# ---------------------------------------------------------------------------
# Fixtures (mirroring test_manager_controller.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_supervisor():
    """Mock AgentSupervisor."""
    supervisor = MagicMock()
    supervisor.spawn_manager = AsyncMock(return_value=True)
    supervisor.send_chat_to_manager = AsyncMock()
    supervisor._send_to_agent = AsyncMock()
    supervisor.shutdown = AsyncMock()
    supervisor.get_agent_state = MagicMock(return_value="idle")
    supervisor.get_all_statuses = MagicMock(return_value={
        MANAGER_AGENT_NAME: {"pid": 12345, "status": "ready"},
    })
    return supervisor


@pytest.fixture
def mock_router():
    """Mock MessageRouter."""
    router = MagicMock()
    router.publish_event = AsyncMock(return_value="stream-entry-id")
    return router


@pytest.fixture
def mock_sessions():
    """Mock SessionManager."""
    sessions = MagicMock()
    sessions.switch_context = MagicMock(return_value="session-123")
    sessions.save_session = AsyncMock()
    sessions.clear_session = AsyncMock()
    sessions.manager_sessions = {}
    return sessions


@pytest.fixture
def mock_config():
    """Mock ConfigStore."""
    config = MagicMock()
    config.office_config = {"manager_model": "claude-sonnet-4-6"}
    config.get_workstream_list = MagicMock(return_value=[])
    config.get_team_roster = MagicMock(return_value="## Agents\n- Analyst")
    config.agents = []
    return config


@pytest.fixture
def controller(mock_supervisor, mock_router, mock_sessions, mock_config):
    """Create a ManagerController with mocked dependencies."""
    return ManagerController(
        supervisor=mock_supervisor,
        router=mock_router,
        session_manager=mock_sessions,
        config_store=mock_config,
        office_id="test-office",
        workspace_path="/tmp/test-workspace",
    )


def _short_timeouts():
    """Patch both watchdog thresholds down so a silent Manager times
    out in ~0.05s instead of 5 minutes."""
    return (
        patch(
            "src.orchestrator.manager_controller."
            "MANAGER_INACTIVITY_TIMEOUT",
            0.05,
        ),
        patch(
            "src.orchestrator.manager_controller.MANAGER_HARD_TIMEOUT",
            0.5,
        ),
    )


def _cancel_calls(mock_supervisor):
    """Extract the cancel_task IPC frames sent via the supervisor."""
    return [
        c for c in mock_supervisor._send_to_agent.await_args_list
        if c.args[1].get("type") == "cancel_task"
    ]


async def _run_timeout_turn(controller, conversation_id: str) -> None:
    """Drive one chat turn into the inactivity timeout."""
    p1, p2 = _short_timeouts()
    with p1, p2:
        await controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "Hello",
            "context_data": {},
            "conversation_id": conversation_id,
        })


# ---------------------------------------------------------------------------
# (a) Timeout sends cancel_task IPC to the Manager subprocess
# ---------------------------------------------------------------------------


class TestTimeoutSendsCancelIpc:
    """Fix 1: the timeout branch must kill the wedged CLI, not just
    abandon the wait (07/G10 — every later turn queued behind the
    zombie and the 1h hard cap was dead)."""

    @pytest.mark.asyncio
    async def test_timeout_sends_cancel_task_to_manager(
        self, controller, mock_supervisor,
    ):
        await _run_timeout_turn(controller, "conv-t1")

        cancel_calls = _cancel_calls(mock_supervisor)
        assert len(cancel_calls) == 1
        assert cancel_calls[0].args[0] == MANAGER_AGENT_NAME
        assert cancel_calls[0].args[1]["reason"] == "inactivity_timeout"

    @pytest.mark.asyncio
    async def test_timeout_cancel_send_failure_still_publishes_error(
        self, controller, mock_supervisor, mock_router,
    ):
        """A wedged stdin pipe (cancel IPC raises) must not mask the
        user-facing timeout message."""
        mock_supervisor._send_to_agent.side_effect = RuntimeError(
            "subprocess pipe closed",
        )

        await _run_timeout_turn(controller, "conv-t2")

        response_payloads = [
            c[0][0] for c in mock_router.publish_event.call_args_list
            if c[0][0].get("type") == "manager_response"
        ]
        assert response_payloads, "no manager_response frame published"
        assert "several minutes" in response_payloads[-1]["content"]

    @pytest.mark.asyncio
    async def test_timeout_copy_says_turn_was_cancelled(
        self, controller, mock_router,
    ):
        """Fix 5: with the cancel IPC actually sent, the copy claiming
        a cancellation is now truthful — and scoped to the TURN, not
        the whole conversation session."""
        await _run_timeout_turn(controller, "conv-t3")

        response_payloads = [
            c[0][0] for c in mock_router.publish_event.call_args_list
            if c[0][0].get("type") == "manager_response"
        ]
        content = response_payloads[-1]["content"]
        assert "cancelled" in content
        # First timeout: the conversation session is NOT reset yet, so
        # the copy must not claim it was.
        assert "session was also reset" not in content


# ---------------------------------------------------------------------------
# (b) Stale-frame gating by conversation_id
# ---------------------------------------------------------------------------


class TestStaleFrameGating:
    """Fix 2: frames carrying a conversation_id that differs from the
    active one are dropped (03/§5.1 — a zombie turn's final falsely
    terminated whichever turn was then in flight)."""

    @pytest.mark.asyncio
    async def test_stale_final_does_not_terminate_new_turn(
        self, controller, mock_router, mock_sessions,
    ):
        controller._active_conversation_id = "conv-NEW"
        controller._response_done.clear()

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "response_final",
            "conversation_id": "conv-OLD",
            "context_key": "general_chat",
            "session_id": "sess-zombie",
            "token_cost": 0.01,
        })

        assert not controller._response_done.is_set(), (
            "stale response_final must NOT terminate the active turn"
        )
        # Dropped wholesale: no session save, no publish.
        mock_sessions.save_session.assert_not_called()
        mock_router.publish_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_final_sets_done(self, controller):
        controller._active_conversation_id = "conv-NEW"
        controller._response_done.clear()

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "response_final",
            "conversation_id": "conv-NEW",
            "context_key": "general_chat",
            "session_id": "sess-1",
        })

        assert controller._response_done.is_set()

    @pytest.mark.asyncio
    async def test_stale_frame_does_not_refresh_watchdog(
        self, controller,
    ):
        """A zombie turn's activity pulse must not keep the NEW turn's
        inactivity watchdog alive."""
        controller._active_conversation_id = "conv-NEW"
        controller._last_activity_ts = 123.0

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "activity",
            "conversation_id": "conv-OLD",
            "context_key": "general_chat",
            "activity": "tool_use",
            "tool": "get_board",
        })

        assert controller._last_activity_ts == 123.0

    @pytest.mark.asyncio
    async def test_matching_frame_refreshes_watchdog(self, controller):
        controller._active_conversation_id = "conv-NEW"
        controller._last_activity_ts = 123.0

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "activity",
            "conversation_id": "conv-NEW",
            "context_key": "general_chat",
            "activity": "tool_use",
            "tool": "get_board",
        })

        assert controller._last_activity_ts != 123.0

    @pytest.mark.asyncio
    async def test_stale_error_frame_dropped(self, controller):
        """Event-hygiene fix: the worker now stamps conversation_id on
        chat-path ``error`` frames, so a zombie turn's late error is
        gated — it must NOT set ``_response_error``/``_response_done``
        for the active turn."""
        controller._active_conversation_id = "conv-NEW"
        controller._response_done.clear()
        controller._response_error = None

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "error",
            "message": "Claude CLI exited with code 1",
            "conversation_id": "conv-OLD",
            "fatal": False,
        })

        assert not controller._response_done.is_set(), (
            "stale error frame must NOT terminate the active turn"
        )
        assert controller._response_error is None, (
            "stale error frame must NOT poison the active turn's error"
        )

    @pytest.mark.asyncio
    async def test_matching_error_frame_still_processed(self, controller):
        """An error frame carrying the ACTIVE conversation_id keeps
        flowing — gating must only drop frames from a DIFFERENT turn."""
        controller._active_conversation_id = "conv-NEW"
        controller._response_done.clear()
        controller._response_error = None

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "error",
            "message": "Claude CLI exited with code 1",
            "conversation_id": "conv-NEW",
            "fatal": False,
        })

        assert controller._response_done.is_set()
        assert controller._response_error is not None

    @pytest.mark.asyncio
    async def test_stale_error_does_not_charge_error_streak(
        self, controller, mock_sessions,
    ):
        """End-to-end: a stale error frame mid-turn followed by a clean
        matching final → the turn ends CLEAN and the consecutive-error
        streak is NOT charged. (Pre-fix, the id-less error frame set
        ``_response_error`` and the streak ticked toward a session
        reset on every zombie flush.)"""

        async def fake_send(msg):
            await asyncio.sleep(0.01)
            # Zombie turn's late error flushes first...
            await controller.handle_manager_event(MANAGER_AGENT_NAME, {
                "type": "error",
                "message": "Claude CLI exited with code 1",
                "conversation_id": "conv-OLD",
                "fatal": False,
            })
            # ...then the ACTIVE turn finishes normally.
            await controller.handle_manager_event(MANAGER_AGENT_NAME, {
                "type": "response_final",
                "conversation_id": "conv-NEW",
                "context_key": "general_chat",
                "token_cost": 0.01,
                "session_id": "sess-ok",
            })

        controller._supervisor.send_chat_to_manager = fake_send
        await controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "Hello",
            "context_data": {},
            "conversation_id": "conv-NEW",
        })

        assert "general_chat" not in controller._consecutive_context_errors
        mock_sessions.clear_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_error_charges_error_streak(self, controller):
        """A matching-id error frame still ends the turn as an error
        and charges the streak — the gate must not over-drop."""

        async def fake_send(msg):
            await asyncio.sleep(0.01)
            await controller.handle_manager_event(MANAGER_AGENT_NAME, {
                "type": "error",
                "message": "Claude CLI exited with code 1",
                "conversation_id": "conv-NEW",
                "fatal": False,
            })

        controller._supervisor.send_chat_to_manager = fake_send
        await controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "Hello",
            "context_data": {},
            "conversation_id": "conv-NEW",
        })

        assert controller._consecutive_context_errors["general_chat"] == 1

    @pytest.mark.asyncio
    async def test_idless_error_frame_still_processed(self, controller):
        """Lifecycle frames and OLD agent_worker builds don't stamp
        conversation_id on ``error`` frames, so gating is impossible
        there — they must keep flowing to the active turn (dropping
        them would swallow legitimate failures)."""
        controller._active_conversation_id = "conv-NEW"
        controller._response_done.clear()

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "error",
            "message": "Claude CLI exited with code 1",
            "fatal": False,
        })

        assert controller._response_done.is_set()
        assert controller._response_error is not None

    @pytest.mark.asyncio
    async def test_stale_chunk_not_published(
        self, controller, mock_router,
    ):
        controller._active_conversation_id = "conv-NEW"

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "response_chunk",
            "conversation_id": "conv-OLD",
            "context_key": "general_chat",
            "content": "zombie text",
        })

        mock_router.publish_event.assert_not_called()


# ---------------------------------------------------------------------------
# (c) Timeout turns count toward the consecutive-error session reset
# ---------------------------------------------------------------------------


class TestTimeoutCountsTowardSessionReset:
    """Fix 3 (03/#27): a silently-wedging session must self-heal the
    same way an erroring one does."""

    @pytest.mark.asyncio
    async def test_single_timeout_does_not_reset_session(
        self, controller, mock_sessions,
    ):
        await _run_timeout_turn(controller, "conv-t1")

        mock_sessions.clear_session.assert_not_called()
        assert controller._consecutive_context_errors["general_chat"] == 1

    @pytest.mark.asyncio
    async def test_consecutive_timeouts_reset_session(
        self, controller, mock_sessions, mock_router,
    ):
        """Two timed-out turns on the same context drop the session —
        identical to two error turns (MANAGER_CONTEXT_RESET_AFTER_ERRORS
        defaults to 2)."""
        for conv in ("conv-t1", "conv-t2"):
            await _run_timeout_turn(controller, conv)

        mock_sessions.clear_session.assert_called_with("general_chat")
        # Counter cleared after the reset fires.
        assert "general_chat" not in controller._consecutive_context_errors
        # The user is told the conversation was reset.
        reset_msgs = [
            c[0][0] for c in mock_router.publish_event.call_args_list
            if c[0][0].get("type") == "manager_response"
            and "reset" in (c[0][0].get("content") or "").lower()
        ]
        assert reset_msgs, "no reset notice published to the user"

    @pytest.mark.asyncio
    async def test_timeout_then_error_shares_the_same_counter(
        self, controller, mock_sessions,
    ):
        """The counter is SHARED between timeout and error paths — one
        timeout + one error reaches the reset threshold."""
        await _run_timeout_turn(controller, "conv-t1")
        assert controller._consecutive_context_errors["general_chat"] == 1

        async def fake_send(msg):
            await asyncio.sleep(0.01)
            await controller._on_error({
                "message": "Claude CLI exited with code 1",
                "fatal": False,
            })

        controller._supervisor.send_chat_to_manager = fake_send
        await controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "Status?",
            "context_data": {},
            "conversation_id": "conv-e1",
        })

        mock_sessions.clear_session.assert_called_with("general_chat")
        assert "general_chat" not in controller._consecutive_context_errors

    @pytest.mark.asyncio
    async def test_clean_turn_after_timeout_clears_streak(
        self, controller, mock_sessions,
    ):
        """Reset-on-success: a successful turn after a timeout clears
        the streak so the session is not dropped later."""
        await _run_timeout_turn(controller, "conv-t1")
        assert controller._consecutive_context_errors["general_chat"] == 1

        async def fake_send(msg):
            await asyncio.sleep(0.01)
            await controller._on_response_final({
                "conversation_id": "conv-ok",
                "context_key": "general_chat",
                "token_cost": 0.01,
                "session_id": "sess-ok",
            })

        controller._supervisor.send_chat_to_manager = fake_send
        await controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "Hello",
            "context_data": {},
            "conversation_id": "conv-ok",
        })

        assert "general_chat" not in controller._consecutive_context_errors
        mock_sessions.clear_session.assert_not_called()


# ---------------------------------------------------------------------------
# (d) Cancel after a timeout reaches the kill path
# ---------------------------------------------------------------------------


class TestCancelAfterTimeout:
    """Fix 4 (03/§5.1): the finally block clears
    ``_active_conversation_id`` after a timeout, which used to make a
    post-timeout Cancel click a NO-OP while the zombie CLI kept
    running. ``cancel_current_turn`` now falls through to killing the
    stray session when the supervisor still reports WORKING."""

    @pytest.mark.asyncio
    async def test_cancel_after_timeout_kills_stray_session(
        self, controller, mock_supervisor, mock_router,
    ):
        """End-to-end: timeout turn (sends 1st cancel), then a user
        Cancel click — with no active turn but the Manager subprocess
        still WORKING — sends a 2nd cancel_task instead of no-opping."""
        from src.orchestrator.agent_supervisor import AgentState

        await _run_timeout_turn(controller, "conv-t1")
        assert controller._active_conversation_id is None
        assert len(_cancel_calls(mock_supervisor)) == 1

        # The wedged turn never produced a response_final, so the
        # supervisor still reports the Manager as WORKING.
        mock_supervisor.get_agent_state = MagicMock(
            return_value=AgentState.WORKING,
        )

        await controller.cancel_current_turn(
            {"context_key": "general_chat"},
        )

        cancel_calls = _cancel_calls(mock_supervisor)
        assert len(cancel_calls) == 2, (
            "post-timeout Cancel must reach the kill path, not no-op"
        )
        assert cancel_calls[-1].args[0] == MANAGER_AGENT_NAME
        # The user sees the cancelled pill.
        cancelled_events = [
            c for c in mock_router.publish_event.await_args_list
            if c.args[0].get("type") == "manager_state"
            and c.args[0].get("state") == "cancelled"
        ]
        assert len(cancelled_events) == 1

    @pytest.mark.asyncio
    async def test_cancel_with_idle_manager_stays_noop(
        self, controller, mock_supervisor, mock_router,
    ):
        """No active turn AND the Manager subprocess is not WORKING →
        the original no-op contract holds (stale Cancel click after a
        turn finished cleanly)."""
        from src.orchestrator.agent_supervisor import AgentState

        assert controller._active_conversation_id is None
        mock_supervisor.get_agent_state = MagicMock(
            return_value=AgentState.READY,
        )

        await controller.cancel_current_turn(
            {"context_key": "general_chat"},
        )

        mock_supervisor._send_to_agent.assert_not_called()
        mock_router.publish_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_stray_kill_ipc_failure_still_notifies_ui(
        self, controller, mock_supervisor, mock_router,
    ):
        """A flaky IPC send on the stray-kill path must not block the
        cancelled-state broadcast."""
        from src.orchestrator.agent_supervisor import AgentState

        mock_supervisor.get_agent_state = MagicMock(
            return_value=AgentState.WORKING,
        )
        mock_supervisor._send_to_agent.side_effect = RuntimeError(
            "subprocess pipe closed",
        )

        await controller.cancel_current_turn(
            {"context_key": "general_chat"},
        )

        cancelled_events = [
            c for c in mock_router.publish_event.await_args_list
            if c.args[0].get("type") == "manager_state"
            and c.args[0].get("state") == "cancelled"
        ]
        assert len(cancelled_events) == 1
