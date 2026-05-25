"""Tests for the rewritten ManagerController (process model).

Covers: message forwarding, timeout handling, tool proxy, crash recovery,
circuit breaker, is_busy, auto_orchestrate, build_dynamic_context.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.manager_controller import (
    MANAGER_AGENT_NAME,
    MANAGER_HARD_TIMEOUT,
    MANAGER_INACTIVITY_TIMEOUT,
    MANAGER_MAX_CONSECUTIVE_CRASHES,
    ManagerController,
    build_dynamic_context,
)


# ---------------------------------------------------------------------------
# Fixtures
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


# ---------------------------------------------------------------------------
# Tests: Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Tests for Manager process lifecycle."""

    @pytest.mark.asyncio
    async def test_start_spawns_manager(self, controller, mock_supervisor):
        """start() spawns the Manager process via supervisor."""
        await controller.start()
        mock_supervisor.spawn_manager.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_resets_crash_counter(self, controller, mock_supervisor):
        """Successful start resets the consecutive crash counter."""
        controller._consecutive_crashes = 3
        await controller.start()
        assert controller._consecutive_crashes == 0

    # P5-V (review): test_start_mock_mode_skips_spawn removed.
    # The `self._mock` field + MOCK_LLM env var were dead code (never
    # adapted for process-per-agent) and were deleted from
    # manager_controller.py — the test asserted on a removed branch.

    @pytest.mark.asyncio
    async def test_stop_sends_shutdown_to_manager(self, controller, mock_supervisor):
        """stop() sends shutdown message to the Manager process only."""
        await controller.stop()
        mock_supervisor._send_to_agent.assert_called_once()
        call_args = mock_supervisor._send_to_agent.call_args[0]
        assert call_args[0] == MANAGER_AGENT_NAME
        assert call_args[1]["type"] == "shutdown"


# ---------------------------------------------------------------------------
# Tests: handle_chat_message
# ---------------------------------------------------------------------------


class TestHandleChatMessage:
    """Tests for handle_chat_message()."""

    @pytest.mark.asyncio
    async def test_sends_message_to_supervisor(self, controller, mock_supervisor):
        """Chat message is forwarded to the Manager subprocess."""
        # Simulate the Manager responding with response_final
        async def fake_send(msg):
            # Trigger the response done event after a short delay
            await asyncio.sleep(0.01)
            await controller._on_response_final({
                "conversation_id": "conv-1",
                "context_key": "general_chat",
                "token_cost": 0.05,
                "session_id": "sess-new",
            })

        mock_supervisor.send_chat_to_manager = fake_send

        await controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "Hello",
            "context_data": {},
            "conversation_id": "conv-1",
        })

        # Verify the session was saved
        controller._sessions.save_session.assert_called_with(
            "general_chat", "sess-new",
        )

    @pytest.mark.asyncio
    async def test_inactivity_timeout_sends_error_response(
        self, controller, mock_router,
    ):
        """Silent Manager → inactivity watchdog fires → error response.

        The timeout is now an INACTIVITY timer, not a total-turn cap.
        With both thresholds set to 0.1s and no events emitted, the
        watchdog decides the Manager is silent and bails.
        """
        controller._supervisor.send_chat_to_manager = AsyncMock()

        with patch(
            "src.orchestrator.manager_controller.MANAGER_INACTIVITY_TIMEOUT",
            0.1,
        ), patch(
            "src.orchestrator.manager_controller.MANAGER_HARD_TIMEOUT",
            0.5,
        ):
            await controller.handle_chat_message({
                "context_key": "general_chat",
                "user_message": "Hello",
                "context_data": {},
                "conversation_id": "conv-timeout",
            })

        # Verify error response was published. The turn-end now ALSO
        # publishes manager_state(idle) from the defense-in-depth
        # finally block, so the error response is no longer the last
        # frame — filter by type instead of indexing the tail.
        mock_router.publish_event.assert_called()
        response_payloads = [
            c[0][0] for c in mock_router.publish_event.call_args_list
            if c[0][0].get("type") == "manager_response"
        ]
        assert response_payloads, "no manager_response frame published"
        payload = response_payloads[-1]
        # New user-facing copy explains the failure mode without
        # blaming the request complexity (the old "simpler request"
        # message was misleading — 18-task scopes are legitimate).
        assert "several minutes" in payload["content"]
        assert "cancelled" in payload["content"]

    @pytest.mark.asyncio
    async def test_activity_resets_inactivity_watchdog(
        self, controller, mock_router,
    ):
        """Events during the turn keep the watchdog from firing.

        This is the core regression: an 18-task scope takes 15+ minutes,
        but each task creation emits a tool_call event that should reset
        the clock. We simulate it by firing progress events at a cadence
        faster than the inactivity threshold, then completing.
        """
        import time as _time

        controller._supervisor.send_chat_to_manager = AsyncMock()

        async def simulate_active_work():
            # 5 bursts of "tool_call" events 0.05s apart, then finalize.
            # Each event refreshes _last_activity_ts via handle_manager_event.
            await asyncio.sleep(0.02)
            for _ in range(5):
                controller._last_activity_ts = _time.monotonic()
                await asyncio.sleep(0.05)
            # Signal completion
            controller._response_done.set()

        with patch(
            "src.orchestrator.manager_controller.MANAGER_INACTIVITY_TIMEOUT",
            0.1,
        ), patch(
            "src.orchestrator.manager_controller.MANAGER_HARD_TIMEOUT",
            5.0,
        ):
            task = asyncio.create_task(simulate_active_work())
            await controller.handle_chat_message({
                "context_key": "general_chat",
                "user_message": "Big request",
                "context_data": {},
                "conversation_id": "conv-active",
            })
            await task

        # No timeout error should have been sent.
        sent_payloads = [
            c[0][0] for c in mock_router.publish_event.call_args_list
        ]
        timeout_payloads = [
            p for p in sent_payloads
            if p.get("type") == "manager_response"
            and "cancelled" in (p.get("content") or "")
        ]
        assert not timeout_payloads, (
            f"Watchdog fired despite continuous activity: {timeout_payloads}"
        )

    @pytest.mark.asyncio
    async def test_exception_clears_session_and_publishes_error(
        self, controller, mock_router, mock_sessions,
    ):
        """Exception during send clears session and publishes error."""
        controller._supervisor.send_chat_to_manager = AsyncMock(
            side_effect=RuntimeError("Manager process is not running"),
        )

        await controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "Hello",
            "context_data": {},
            "conversation_id": "conv-err",
        })

        mock_sessions.clear_session.assert_called_with("general_chat")
        mock_router.publish_event.assert_called()
        # Filter to the error manager_response — the defense-in-depth
        # finally now publishes manager_state(idle) at the tail.
        response_payloads = [
            c[0][0] for c in mock_router.publish_event.call_args_list
            if c[0][0].get("type") == "manager_response"
        ]
        assert response_payloads, "no manager_response frame published"
        payload = response_payloads[-1]
        assert "error" in payload["content"].lower()

    @pytest.mark.asyncio
    async def test_active_conversation_cleared_after_completion(
        self, controller, mock_supervisor,
    ):
        """_active_conversation_id is None after handle_chat_message returns."""
        async def fake_send(msg):
            await asyncio.sleep(0.01)
            await controller._on_response_final({
                "conversation_id": "conv-2",
                "context_key": "general_chat",
                "session_id": "s1",
            })

        mock_supervisor.send_chat_to_manager = fake_send

        await controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "Hello",
            "context_data": {},
            "conversation_id": "conv-2",
        })

        assert controller._active_conversation_id is None

    # P5-V (review): test_mock_mode_delegates_to_mock removed for the
    # same reason as test_start_mock_mode_skips_spawn above —
    # MockManager scaffolding was deleted as dead code.


# ---------------------------------------------------------------------------
# Tests: Response chunk forwarding
# ---------------------------------------------------------------------------


class TestResponseChunkForwarding:
    """Tests for response chunk routing."""

    @pytest.mark.asyncio
    async def test_chunk_forwarded_to_router(self, controller, mock_router):
        """Response chunks from subprocess are published via router."""
        controller._active_conversation_id = "conv-1"

        await controller._on_response_chunk({
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "content": "Hello, I can help with that.",
        })

        mock_router.publish_event.assert_called_once_with({
            "type": "manager_response",
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "content": "Hello, I can help with that.",
            "is_streaming": True,
            "is_final": False,
        })

    @pytest.mark.asyncio
    async def test_empty_chunk_ignored(self, controller, mock_router):
        """Empty response chunks are not forwarded."""
        await controller._on_response_chunk({
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "content": "",
        })
        mock_router.publish_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_response_final_sets_done_event(self, controller, mock_router):
        """response_final sets the _response_done event."""
        controller._active_conversation_id = "conv-1"
        controller._response_done.clear()

        await controller._on_response_final({
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "token_cost": 0.05,
            "session_id": "sess-123",
        })

        assert controller._response_done.is_set()

    @pytest.mark.asyncio
    async def test_response_final_saves_session(self, controller, mock_sessions):
        """response_final saves the session_id via session manager."""
        await controller._on_response_final({
            "conversation_id": "conv-1",
            "context_key": "workstream:ws-1",
            "session_id": "sess-abc",
        })

        mock_sessions.save_session.assert_called_with(
            "workstream:ws-1", "sess-abc",
        )

    @pytest.mark.asyncio
    async def test_response_final_publishes_final_marker(self, controller, mock_router):
        """response_final publishes the final marker event AND the
        terminal manager_state(idle) pill-clear (see
        ``test_routes_response_final`` for the full contract)."""
        controller._active_conversation_id = "conv-1"

        await controller._on_response_final({
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "token_cost": 0.12,
            "session_id": "s1",
        })

        # Two events: the final response marker + the idle pill-clear.
        assert mock_router.publish_event.await_count == 2
        response_event = next(
            c.args[0]
            for c in mock_router.publish_event.await_args_list
            if c.args[0].get("type") == "manager_response"
        )
        assert response_event["is_final"] is True
        assert response_event["is_streaming"] is False
        assert response_event["token_cost"] == 0.12

    @pytest.mark.asyncio
    async def test_response_final_no_session_id_skips_save(
        self, controller, mock_sessions,
    ):
        """response_final with no session_id does not call save_session."""
        await controller._on_response_final({
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "session_id": "",
        })
        mock_sessions.save_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_response_final_sets_done_even_on_publish_failure(
        self, controller, mock_router,
    ):
        """response_done is set even if publish_event fails."""
        mock_router.publish_event = AsyncMock(
            side_effect=RuntimeError("Redis down"),
        )
        controller._response_done.clear()

        await controller._on_response_final({
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "session_id": "",
        })

        # Must still set done to avoid hanging
        assert controller._response_done.is_set()

    @pytest.mark.asyncio
    async def test_response_chunk_publish_failure_does_not_raise(
        self, controller, mock_router,
    ):
        """Chunk publish failure is caught and logged, not raised."""
        mock_router.publish_event = AsyncMock(
            side_effect=RuntimeError("Redis down"),
        )
        controller._active_conversation_id = "conv-1"

        # Should not raise
        await controller._on_response_chunk({
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "content": "Hello",
        })

    @pytest.mark.asyncio
    async def test_response_chunk_skipped_when_no_active_conversation(
        self, controller, mock_router,
    ):
        """Chunks are dropped if no active conversation (exchange already resolved)."""
        controller._active_conversation_id = None

        await controller._on_response_chunk({
            "conversation_id": "conv-stale",
            "context_key": "general_chat",
            "content": "Late chunk",
        })
        mock_router.publish_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_response_final_skipped_when_already_done(
        self, controller, mock_router,
    ):
        """Duplicate response_final is skipped if exchange was already completed."""
        # Simulate an error having already resolved the exchange
        controller._response_done.set()
        controller._active_conversation_id = "conv-1"

        await controller._on_response_final({
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "session_id": "s1",
            "token_cost": 0.05,
        })

        # Should NOT publish a second is_final=True
        mock_router.publish_event.assert_not_called()


class TestCrashRecovery:
    """Tests for Manager process crash handling."""

    @pytest.mark.asyncio
    async def test_crash_triggers_restart(self, controller):
        """Manager crash triggers automatic restart."""
        controller._spawn_manager = AsyncMock()

        with patch("src.orchestrator.manager_controller.MANAGER_RESTART_DELAY", 0):
            await controller.handle_manager_crash(exit_code=1)

        controller._spawn_manager.assert_called_once()
        assert controller._consecutive_crashes == 1

    @pytest.mark.asyncio
    async def test_crash_unblocks_active_conversation(self, controller):
        """Crash during active conversation signals error."""
        controller._active_conversation_id = "conv-active"
        controller._spawn_manager = AsyncMock()

        with patch("src.orchestrator.manager_controller.MANAGER_RESTART_DELAY", 0):
            await controller.handle_manager_crash(exit_code=137)

        assert controller._response_error is not None
        assert "crashed" in controller._response_error.lower()
        assert controller._response_done.is_set()

    @pytest.mark.asyncio
    async def test_circuit_breaker_stops_restarts(self, controller):
        """After max crashes, auto-restart stops."""
        controller._spawn_manager = AsyncMock()
        controller._consecutive_crashes = MANAGER_MAX_CONSECUTIVE_CRASHES

        await controller.handle_manager_crash(exit_code=1)

        controller._spawn_manager.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_spawn_resets_crash_counter(self, controller):
        """Successful spawn resets the consecutive crash counter."""
        controller._consecutive_crashes = 3
        await controller._spawn_manager()
        assert controller._consecutive_crashes == 0

    @pytest.mark.asyncio
    async def test_crash_clears_session(self, controller, mock_sessions):
        """Crash clears the session for the active context."""
        controller._spawn_manager = AsyncMock()
        controller._active_context_key = "workstream:ws-1"

        with patch("src.orchestrator.manager_controller.MANAGER_RESTART_DELAY", 0):
            await controller.handle_manager_crash(exit_code=1)

        mock_sessions.clear_session.assert_called_with("workstream:ws-1")

    @pytest.mark.asyncio
    async def test_fatal_error_event_triggers_restart(self, controller):
        """Fatal error from subprocess triggers restart via event handler."""
        controller._spawn_manager = AsyncMock()

        with patch("src.orchestrator.manager_controller.MANAGER_RESTART_DELAY", 0):
            await controller.handle_manager_event(MANAGER_AGENT_NAME, {
                "type": "error",
                "message": "OOM killed",
                "fatal": True,
            })

        assert controller._response_done.is_set()
        # Fatal error triggers _restart_manager which calls _spawn_manager
        controller._spawn_manager.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonfatal_error_does_not_restart(self, controller):
        """Non-fatal error sets response done but does not restart."""
        controller._spawn_manager = AsyncMock()

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "error",
            "message": "Transient failure",
            "fatal": False,
        })

        assert controller._response_done.is_set()
        controller._spawn_manager.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_failure_is_logged_not_raised(self, controller):
        """Failed restart during crash recovery does not propagate."""
        controller._spawn_manager = AsyncMock(
            side_effect=RuntimeError("Cannot spawn"),
        )

        with patch("src.orchestrator.manager_controller.MANAGER_RESTART_DELAY", 0):
            # Should not raise
            await controller.handle_manager_crash(exit_code=1)

        assert controller._consecutive_crashes == 1


# ---------------------------------------------------------------------------
# Tests: is_busy property
# ---------------------------------------------------------------------------


class TestIsBusy:
    """Tests for the is_busy property."""

    def test_busy_when_conversation_active(self, controller):
        """is_busy returns True when a conversation is being processed."""
        controller._active_conversation_id = "conv-1"
        assert controller.is_busy is True

    def test_not_busy_when_idle(self, controller):
        """is_busy returns False when no conversation is active."""
        controller._active_conversation_id = None
        controller._supervisor.get_agent_state = MagicMock(return_value="idle")
        assert controller.is_busy is False

    def test_busy_when_supervisor_reports_working(self, controller):
        """is_busy returns True when supervisor reports WORKING state."""
        from src.orchestrator.agent_supervisor import AgentState

        controller._active_conversation_id = None
        controller._supervisor.get_agent_state = MagicMock(
            return_value=AgentState.WORKING,
        )
        assert controller.is_busy is True

    # P6 review fix: test_mock_mode_always_not_busy removed.
    # MOCK_LLM + self._mock were deleted as dead code in v2.10.2;
    # the test was setting a removed attribute and the assertion
    # passed coincidentally only when _active_conversation_id was
    # None (which it always is in this fixture).


# ---------------------------------------------------------------------------
# Tests: C3 — script drops defer during user streams
# ---------------------------------------------------------------------------


class TestScriptDropUserStreamDefer:
    """Regression guard for C3/H8: a script-origin chat turn must
    wait for any in-flight user turn to complete before acquiring
    the chat lock. Without this, a burst of outbox drops could
    inject into the middle of a user's response stream and corrupt
    the session's active-conversation tracking.
    """

    @pytest.mark.asyncio
    async def test_script_defers_while_user_streaming(
        self, controller, mock_supervisor
    ):
        # Start a user turn that holds the lock open.
        user_started = asyncio.Event()
        user_release = asyncio.Event()

        async def slow_user_send(msg):
            user_started.set()
            await user_release.wait()
            await controller._on_response_final({
                "conversation_id": "user-1",
                "context_key": "general_chat",
                "token_cost": 0.0,
                "session_id": "s",
            })

        mock_supervisor.send_chat_to_manager = slow_user_send

        user_task = asyncio.create_task(controller.handle_chat_message({
            "context_key": "general_chat",
            "user_message": "hi",
            "context_data": {},
            "conversation_id": "user-1",
        }, source="user"))
        await user_started.wait()
        # At this point the user turn is in flight and holding both
        # _user_streaming AND the chat lock.
        assert controller._user_streaming is True

        # Script drop should park on _user_turn_done, NOT start.
        script_started_flag = {"started": False}

        async def track_script_send(msg):
            script_started_flag["started"] = True
            await controller._on_response_final({
                "conversation_id": "script-1",
                "context_key": "general_chat",
                "token_cost": 0.0,
                "session_id": "s",
            })

        # Replace the mock for the next call. We pass source="script"
        # via ingest_script_message which wraps handle_chat_message.
        mock_supervisor.send_chat_to_manager = track_script_send

        script_task = asyncio.create_task(
            controller.handle_chat_message({
                "context_key": "general_chat",
                "user_message": "[Script: s] drop",
                "context_data": {},
                "conversation_id": "script-1",
            }, source="script")
        )
        # Give the script task a chance to run and park. Spin the
        # loop a few times — plenty for a ready-to-await coroutine
        # to advance to its wait point.
        for _ in range(5):
            await asyncio.sleep(0)
        assert script_started_flag["started"] is False

        # Release the user turn. The script turn should unpark and
        # complete right after.
        user_release.set()
        await asyncio.wait_for(user_task, timeout=1.0)
        await asyncio.wait_for(script_task, timeout=1.0)
        assert script_started_flag["started"] is True
        # User-streaming flag is back to False.
        assert controller._user_streaming is False


# ---------------------------------------------------------------------------
# Tests: Event handler routing
# ---------------------------------------------------------------------------


class TestEventHandlerRouting:
    """Tests for handle_manager_event() dispatch."""

    @pytest.mark.asyncio
    async def test_ignores_non_manager_events(self, controller, mock_router):
        """Events from non-manager agents are ignored."""
        await controller.handle_manager_event("analyst", {
            "type": "response_chunk",
            "content": "Should be ignored",
        })
        mock_router.publish_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_routes_response_chunk(self, controller, mock_router):
        """response_chunk events are forwarded to the router."""
        controller._active_conversation_id = "conv-1"
        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "response_chunk",
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "content": "Hello",
        })
        mock_router.publish_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_response_final(self, controller, mock_router):
        """response_final events are forwarded and set done.

        TWO router publications are expected:
          1. ``manager_response`` with is_final=True (the chat
             completion signal the frontend uses to finalise the
             streaming bubble).
          2. ``manager_state(idle, "")`` — clears the
             "Manager working — Xs elapsed" status pill. Without
             this, the pill stays stuck on the last heartbeat
             message until the next turn starts. The empty
             ``message`` gates ChatPanel rendering off.
        """
        controller._active_conversation_id = "conv-1"
        controller._response_done.clear()

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "response_final",
            "conversation_id": "conv-1",
            "context_key": "general_chat",
            "session_id": "s-1",
        })

        assert controller._response_done.is_set()
        # Both manager_response(final) and manager_state(idle) fire.
        assert mock_router.publish_event.await_count == 2
        emitted_types = [
            c.args[0].get("type")
            for c in mock_router.publish_event.await_args_list
        ]
        assert "manager_response" in emitted_types
        assert "manager_state" in emitted_types

        # The state event clears the pill (state=idle + empty msg).
        state_event = next(
            c.args[0]
            for c in mock_router.publish_event.await_args_list
            if c.args[0].get("type") == "manager_state"
        )
        assert state_event["state"] == "idle"
        assert state_event["message"] == ""

    @pytest.mark.asyncio
    async def test_response_final_after_cancel_does_not_overwrite_state(
        self, controller, mock_router,
    ):
        """The cancel path publishes ``manager_state(cancelled)`` and
        sets ``_response_done``. A late response_final from the
        subprocess (which follows shortly after cancel) must NOT
        overwrite the cancelled pill with idle. The early-return on
        ``_response_done.is_set()`` is the guard."""
        controller._active_conversation_id = "conv-cancelled"
        # Simulate that cancel already ran.
        controller._response_done.set()

        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "response_final",
            "conversation_id": "conv-cancelled",
            "context_key": "general_chat",
            "session_id": "",
        })

        # No publish — the early-return short-circuited everything.
        idle_events = [
            c for c in mock_router.publish_event.await_args_list
            if c.args[0].get("type") == "manager_state"
            and c.args[0].get("state") == "idle"
        ]
        assert idle_events == [], (
            "Late response_final after cancel must NOT publish "
            "manager_state(idle) — that would overwrite the "
            "cancelled pill with an empty message."
        )

    @pytest.mark.asyncio
    async def test_tool_call_event_logs_warning(self, controller, mock_supervisor):
        """T1.11 (review): tool_call IPC frames are no longer proxied —
        the in-container MCP server handles tool dispatch directly.
        Verify the controller logs and ignores the frame instead of
        sending a tool_response."""
        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "tool_call",
            "request_id": "req-1",
            "tool": "create_task",
            "params": {},
        })
        # The retired path sent a response back via the supervisor —
        # we explicitly do NOT do that anymore.
        mock_supervisor._send_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_routes_progress(self, controller, mock_router):
        """progress events are handled without error."""
        await controller.handle_manager_event(MANAGER_AGENT_NAME, {
            "type": "progress",
            "event_type": "checkpoint",
            "content": "Processing step 1",
        })
        # Progress events only log, they don't publish to the router
        mock_router.publish_event.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Context switching
# ---------------------------------------------------------------------------


class TestIngestScopeCompleted:
    """Coverage for the scope-completion Manager-poke (CHAT-2026-05).

    When the backend fires ``scope_completed`` (executing → done with
    no next scope queued), the controller must:

      1. Build a ``[Scope Completed: ...]``-prefixed chat message.
      2. Route it through ``handle_chat_message(source="script")`` so
         it serialises behind any in-flight user turn and shares the
         script-drop deferral guarantees.
      3. Use a deterministic conversation id derived from the scope
         readable id so duplicate deliveries don't double-prompt.
    """

    @pytest.mark.asyncio
    async def test_dispatches_chat_to_manager_subprocess(
        self, controller, mock_supervisor, mock_config,
    ):
        """End-to-end: an ingest call eventually hands a ``chat_message``
        IPC frame to the supervisor's send-to-Manager method. The
        controller awaits the chat handler, so we drive it to completion
        with a synthetic response_final."""

        # _build_script_context_data calls config.get_workstream — return
        # None so the helper short-circuits and yields a plain {} dict
        # (the prompt builder downstream rejects MagicMock-valued dicts).
        mock_config.get_workstream = MagicMock(return_value=None)

        # Wire up an immediate completion so handle_chat_message doesn't
        # block on the inactivity watchdog.
        async def fake_send_chat(msg):
            await asyncio.sleep(0)
            await controller._on_response_final({
                "conversation_id": msg.get("conversation_id"),
                "context_key": msg.get("context_key"),
                "session_id": "sess-scope-1",
            })
        mock_supervisor.send_chat_to_manager.side_effect = fake_send_chat

        await controller.ingest_scope_completed({
            "context_key": "workstream:ws-1",
            "scope_readable_id": "WR-003.S01",
            "scope_name": "Authentication",
            "task_count": 5,
        })

        mock_supervisor.send_chat_to_manager.assert_awaited()
        sent_msg = mock_supervisor.send_chat_to_manager.await_args.args[0]
        assert sent_msg["context_key"] == "workstream:ws-1"
        assert "[Scope Completed: WR-003.S01]" in sent_msg["content"]
        assert "Authentication" in sent_msg["content"]
        assert "5 tasks done" in sent_msg["content"]
        # Deterministic conv id helps dedup retries upstream.
        assert sent_msg["conversation_id"] == "scope-WR-003.S01"

    @pytest.mark.asyncio
    async def test_singular_task_grammar(
        self, controller, mock_supervisor, mock_config,
    ):
        """One task → "1 task done", not "1 tasks done"."""
        mock_config.get_workstream = MagicMock(return_value=None)

        async def fake_send_chat(msg):
            await asyncio.sleep(0)
            await controller._on_response_final({
                "conversation_id": msg.get("conversation_id"),
                "context_key": msg.get("context_key"),
                "session_id": "",
            })
        mock_supervisor.send_chat_to_manager.side_effect = fake_send_chat

        await controller.ingest_scope_completed({
            "context_key": "workstream:ws-1",
            "scope_readable_id": "WR-003.S02",
            "scope_name": "Tiny",
            "task_count": 1,
        })

        sent_msg = mock_supervisor.send_chat_to_manager.await_args.args[0]
        assert "1 task done" in sent_msg["content"]
        assert "1 tasks done" not in sent_msg["content"]

    @pytest.mark.asyncio
    async def test_general_chat_skips_workstream_context_data(
        self, controller, mock_supervisor,
    ):
        """For general-chat scopes there's no workstream metadata to
        attach; the context-data builder returns an empty dict."""
        async def fake_send_chat(msg):
            await asyncio.sleep(0)
            await controller._on_response_final({
                "conversation_id": msg.get("conversation_id"),
                "context_key": msg.get("context_key"),
                "session_id": "",
            })
        mock_supervisor.send_chat_to_manager.side_effect = fake_send_chat

        await controller.ingest_scope_completed({
            "context_key": "general_chat",
            "scope_readable_id": "GC-001.S01",
            "scope_name": "Solo",
            "task_count": 2,
        })

        sent_msg = mock_supervisor.send_chat_to_manager.await_args.args[0]
        assert sent_msg["context_key"] == "general_chat"
        assert sent_msg["context_data"] == {}


class TestIngestActionRequestDecided:
    """The user-decision proactivity loop. When the user approves /
    rejects an action_request in the Inbox panel, the backend pokes
    the Manager so it can re-plan without polling. Mirrors
    ``ingest_scope_completed``.

    Three things to lock in:
      1. End-to-end: the synthetic chat turn lands on the Manager
         subprocess with the right context_key and a body that
         includes the decision, request_type, and any
         resulting_task_id.
      2. Deterministic conv_id derived from the request_id (dedup
         on retried deliveries).
      3. The body adapts to approved-with-new-task,
         approved-no-task, and rejected outcomes.
    """

    @pytest.mark.asyncio
    async def test_approved_with_resulting_task_dispatches_to_manager(
        self, controller, mock_supervisor, mock_config,
    ):
        mock_config.get_workstream = MagicMock(return_value=None)

        async def fake_send_chat(msg):
            await asyncio.sleep(0)
            await controller._on_response_final({
                "conversation_id": msg.get("conversation_id"),
                "context_key": msg.get("context_key"),
                "session_id": "sess-ar-1",
            })
        mock_supervisor.send_chat_to_manager.side_effect = fake_send_chat

        await controller.ingest_action_request_decided({
            "context_key": "workstream:ws-1",
            "request_id": "abc12345-6789-aaaa-bbbb-cccccccccccc",
            "request_type": "create_task",
            "decision": "approved",
            "decision_notes": "Looks good — go ahead",
            "resulting_task_id": "task-xyz",
            "source_task_id": "task-src",
            "requesting_agent": "research-agent",
        })

        mock_supervisor.send_chat_to_manager.assert_awaited()
        sent = mock_supervisor.send_chat_to_manager.await_args.args[0]
        assert sent["context_key"] == "workstream:ws-1"
        assert "[Action Request Approved: create_task]" in sent["content"]
        assert "research-agent" in sent["content"]
        assert "task-xyz" in sent["content"]
        assert "task-src" in sent["content"]
        # Deterministic conv id for dedup.
        assert sent["conversation_id"].startswith("action-req-")

    @pytest.mark.asyncio
    async def test_rejected_decision_includes_reject_framing(
        self, controller, mock_supervisor, mock_config,
    ):
        mock_config.get_workstream = MagicMock(return_value=None)

        async def fake_send_chat(msg):
            await asyncio.sleep(0)
            await controller._on_response_final({
                "conversation_id": msg.get("conversation_id"),
                "context_key": msg.get("context_key"),
                "session_id": "",
            })
        mock_supervisor.send_chat_to_manager.side_effect = fake_send_chat

        await controller.ingest_action_request_decided({
            "context_key": "general_chat",
            "request_id": "def67890-1234-aaaa-bbbb-cccccccccccc",
            "request_type": "request_clarification",
            "decision": "rejected",
            "decision_notes": "Not this time",
            "resulting_task_id": None,
            "source_task_id": "task-src-2",
            "requesting_agent": "manager-assistant",
        })

        sent = mock_supervisor.send_chat_to_manager.await_args.args[0]
        assert "[Action Request Rejected: request_clarification]" in sent["content"]
        # Don't claim a new task was created when none was.
        assert "new task was created" not in sent["content"]
        # The "do NOT take rejected action" reminder is present.
        assert "rejected" in sent["content"].lower()


class TestCancelCurrentTurn:
    """Chat-v2 (CHAT-005): user-initiated mid-turn cancel.

    Each case targets one of the four contracted side effects of
    :meth:`ManagerController.cancel_current_turn`:

      1. No-op when no turn is in flight.
      2. Sends a ``cancel_task`` IPC frame to the Manager subprocess.
      3. Publishes ``manager_state(cancelled)`` to the UI.
      4. Unblocks the chat handler via ``_response_done.set()``.
    """

    @pytest.mark.asyncio
    async def test_no_op_when_no_turn_in_flight(
        self, controller, mock_supervisor, mock_router,
    ):
        """Idle Manager → no IPC, no state broadcast, no error.

        Defends against a stale Cancel click that arrives a tick
        after the Manager already finished.
        """
        assert controller._active_conversation_id is None

        await controller.cancel_current_turn({"context_key": "general_chat"})

        mock_supervisor._send_to_agent.assert_not_called()
        mock_router.publish_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_cancel_task_to_manager_subprocess(
        self, controller, mock_supervisor,
    ):
        """Active turn → ``cancel_task`` is forwarded to the Manager
        subprocess via the supervisor."""
        controller._active_conversation_id = "conv-cancel-1"
        controller._active_context_key = "workstream:ws-1"

        await controller.cancel_current_turn(
            {"context_key": "workstream:ws-1"},
        )

        # Find the cancel_task call among the supervisor's sent frames.
        cancel_calls = [
            c for c in mock_supervisor._send_to_agent.await_args_list
            if c.args[1].get("type") == "cancel_task"
        ]
        assert len(cancel_calls) == 1
        assert cancel_calls[0].args[0] == MANAGER_AGENT_NAME
        assert cancel_calls[0].args[1].get("reason") == "user_cancel"

    @pytest.mark.asyncio
    async def test_publishes_cancelled_state_to_router(
        self, controller, mock_router,
    ):
        """Active turn → UI gets a ``manager_state(cancelled)`` event
        immediately, before the subprocess winds down."""
        controller._active_conversation_id = "conv-cancel-2"
        ctx = "workstream:ws-2"

        await controller.cancel_current_turn({"context_key": ctx})

        cancelled_events = [
            c for c in mock_router.publish_event.await_args_list
            if c.args[0].get("type") == "manager_state"
            and c.args[0].get("state") == "cancelled"
        ]
        assert len(cancelled_events) == 1
        payload = cancelled_events[0].args[0]
        assert payload["context_key"] == ctx
        assert "cancelled" in payload["message"].lower()

    @pytest.mark.asyncio
    async def test_unblocks_chat_handler(self, controller):
        """``_response_done`` is set so the in-flight chat handler
        exits its watchdog loop even if the subprocess never sends a
        clean response_final."""
        controller._active_conversation_id = "conv-cancel-3"
        controller._response_done.clear()
        assert not controller._response_done.is_set()

        await controller.cancel_current_turn({"context_key": "general_chat"})

        assert controller._response_done.is_set()
        # The error message is non-empty so the chat handler surfaces
        # a clean fallback message rather than "an unknown error".
        assert controller._response_error
        assert "cancel" in controller._response_error.lower()

    @pytest.mark.asyncio
    async def test_supervisor_send_failure_does_not_break_ui_notify(
        self, controller, mock_supervisor, mock_router,
    ):
        """A flaky IPC send must not block the state broadcast — the
        user MUST see "cancelled" even if the subprocess is wedged."""
        controller._active_conversation_id = "conv-cancel-4"

        async def _fail(*_args, **_kwargs):
            raise RuntimeError("subprocess pipe closed")

        mock_supervisor._send_to_agent.side_effect = _fail

        await controller.cancel_current_turn({"context_key": "general_chat"})

        cancelled_events = [
            c for c in mock_router.publish_event.await_args_list
            if c.args[0].get("type") == "manager_state"
            and c.args[0].get("state") == "cancelled"
        ]
        assert len(cancelled_events) == 1
        assert controller._response_done.is_set()


class TestContextSwitching:
    """Tests for context switching."""

    @pytest.mark.asyncio
    async def test_switch_context_updates_session_manager(
        self, controller, mock_sessions,
    ):
        """handle_switch_context delegates to session manager."""
        await controller.handle_switch_context({
            "context_key": "workstream:ws-abc",
        })
        mock_sessions.switch_context.assert_called_with("workstream:ws-abc")

    @pytest.mark.asyncio
    async def test_switch_context_updates_active_context(self, controller):
        """handle_switch_context updates _active_context_key."""
        await controller.handle_switch_context({
            "context_key": "workstream:ws-abc",
        })
        assert controller._active_context_key == "workstream:ws-abc"

    @pytest.mark.asyncio
    async def test_session_preserved_across_messages(
        self, controller, mock_supervisor, mock_sessions,
    ):
        """Session ID from response_final is used in subsequent messages."""
        # First message: subprocess returns a session_id
        async def fake_send_1(msg):
            await asyncio.sleep(0.01)
            await controller._on_response_final({
                "conversation_id": "conv-1",
                "context_key": "workstream:ws-1",
                "session_id": "sess-new-1",
            })

        mock_supervisor.send_chat_to_manager = fake_send_1

        await controller.handle_chat_message({
            "context_key": "workstream:ws-1",
            "user_message": "First message",
            "context_data": {},
            "conversation_id": "conv-1",
        })

        mock_sessions.save_session.assert_called_with(
            "workstream:ws-1", "sess-new-1",
        )


# ---------------------------------------------------------------------------
# Tests: Auto-orchestrate — REMOVED
# auto_orchestrate has been replaced by the Manager Assistant (Board Operator)
# which handles review and blocked tasks via per-agent queues.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: build_dynamic_context (module-level function)
# ---------------------------------------------------------------------------


class TestBuildDynamicContext:
    """Tests for the module-level build_dynamic_context()."""

    def test_general_chat_context(self, mock_config):
        """General chat includes workstream list."""
        mock_config.get_workstream_list = MagicMock(return_value=[
            {"name": "WR", "task_count": 3, "priority": "high"},
        ])
        result = build_dynamic_context("general_chat", {}, mock_config)
        assert "General Chat" in result
        assert "CANNOT create tasks" in result
        assert "WR" in result

    def test_workstream_context(self, mock_config):
        """Workstream context includes goals and UUID."""
        context_data = {
            "workstream_id": "ws-uuid",
            "workstream_name": "Website Redesign",
            "workstream_priority": "high",
            "workstream_goals": "Launch by Q2",
        }
        result = build_dynamic_context("workstream:ws-uuid", context_data, mock_config)
        assert "Website Redesign" in result
        assert "ws-uuid" in result
        assert "Launch by Q2" in result
        assert "CAN and SHOULD create tasks" in result

    def test_includes_team_roster(self, mock_config):
        """Dynamic context includes the team roster."""
        result = build_dynamic_context("general_chat", {}, mock_config)
        assert "Your Team" in result
        assert "Analyst" in result

    def test_includes_board_summary(self, mock_config):
        """Board summary is included when present in context_data."""
        context_data = {"task_summary": "in_progress: 3, review: 1"}
        result = build_dynamic_context("general_chat", context_data, mock_config)
        assert "Board Summary" in result
        assert "in_progress: 3" in result

    def test_includes_kb_summary(self, mock_config):
        """KB summary is included when present in context_data."""
        context_data = {"kb_summary": "42 documents indexed"}
        result = build_dynamic_context("general_chat", context_data, mock_config)
        assert "Knowledge Base" in result
        assert "42 documents" in result

    def test_includes_chat_history(self, mock_config):
        """Chat history is included when present in context_data."""
        context_data = {"chat_history": "User: Hello\nManager: Hi there"}
        result = build_dynamic_context("general_chat", context_data, mock_config)
        assert "Recent Conversation" in result
        assert "Hello" in result

    def test_workstream_without_optional_fields(self, mock_config):
        """Workstream context works without optional description/goals."""
        context_data = {
            "workstream_id": "ws-uuid",
            "workstream_name": "Minimal WS",
        }
        result = build_dynamic_context("workstream:ws-uuid", context_data, mock_config)
        assert "Minimal WS" in result
        assert "Goals" not in result

    def test_includes_recently_completed_section(self, mock_config):
        """Recently-completed tasks render as a dedicated section so the
        Manager can answer 'what's the latest?' questions and reference
        fresh deliverables when planning the next scope."""
        context_data = {
            "workstream_id": "ws-uuid",
            "workstream_name": "WR",
            "recently_completed": [
                {
                    "readable_id": "WR-001.T05",
                    "title": "Draft API spec",
                    "assigned_agent": "analyst",
                    "completed_at": "2026-05-15T08:00:00Z",
                },
                {
                    "readable_id": "WR-001.T06",
                    "title": "Build prototype",
                    "assigned_agent": "python-dev",
                    "completed_at": "2026-05-15T09:00:00Z",
                },
            ],
        }
        result = build_dynamic_context(
            "workstream:ws-uuid", context_data, mock_config
        )
        assert "Recently Completed" in result
        assert "WR-001.T05" in result
        assert "Draft API spec" in result
        assert "by `analyst`" in result
        # Must tell the Manager HOW to pull the deliverables — a list of
        # IDs without a follow-up hook is dead weight.
        assert "get_task_detail" in result

    def test_recently_completed_section_omitted_when_empty(
        self, mock_config
    ):
        """No section when the workstream has no fresh completions —
        otherwise the Manager wastes turns reading an empty header."""
        context_data = {
            "workstream_id": "ws-uuid",
            "workstream_name": "WR",
            "recently_completed": [],
        }
        result = build_dynamic_context(
            "workstream:ws-uuid", context_data, mock_config
        )
        assert "Recently Completed" not in result
