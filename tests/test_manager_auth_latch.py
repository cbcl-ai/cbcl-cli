"""Auth-expired handling in the ManagerController (owner incident 2026-08).

The container CLI's OAuth login dies every few weeks with
"Failed to authenticate: OAuth session expired and could not be
refreshed". Contract under test:

* the string classifies AUTH_FAILED (account outage), so the Manager
  NEVER clears the stored session — no transcript wipe, no false
  "conversation was reset" bubble — regardless of how many turns fail;
* the published copy is the actionable auth explainer carrying a
  markdown deep-link to Settings → Connection (``?tab=connection&
  check=auth`` auto-runs the auth check on landing);
* the office-level latch shows the FULL explainer once, then short
  "still expired" notices; a clean turn or a successful keepalive
  probe (``note_auth_probe(True)``) re-arms the full explainer;
* ``note_auth_probe(False)`` (the keepalive's auth-down verdict) makes
  even a bare UNKNOWN_FATAL turn surface as the auth outage, without
  consuming the consecutive-error session-reset backstop.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.orchestrator.manager_controller import ManagerController

OAUTH_ERROR = (
    "Failed to authenticate: OAuth session expired and could not "
    "be refreshed"
)
SETTINGS_LINK = "/offices/test-office/settings?tab=connection&check=auth"


@pytest.fixture
def mock_router():
    router = MagicMock()
    router.publish_event = AsyncMock(return_value="stream-entry-id")
    return router


@pytest.fixture
def mock_sessions():
    sessions = MagicMock()
    sessions.switch_context = MagicMock(return_value="session-123")
    sessions.save_session = AsyncMock()
    sessions.clear_session = AsyncMock()
    sessions.manager_sessions = {}
    return sessions


@pytest.fixture
def controller(mock_router, mock_sessions):
    supervisor = MagicMock()
    supervisor.spawn_manager = AsyncMock(return_value=True)
    supervisor.send_chat_to_manager = AsyncMock()
    supervisor._send_to_agent = AsyncMock()
    supervisor.get_agent_state = MagicMock(return_value="idle")
    config = MagicMock()
    config.office_config = {"manager_model": "claude-sonnet-4-6"}
    config.get_workstream_list = MagicMock(return_value=[])
    config.get_team_roster = MagicMock(return_value="## Agents\n- Analyst")
    config.agents = []
    return ManagerController(
        supervisor=supervisor,
        router=mock_router,
        session_manager=mock_sessions,
        config_store=config,
        office_id="test-office",
        workspace_path="/tmp/test-workspace",
    )


def _wire_error(controller, error_text: str) -> None:
    async def fake_send(msg):
        await asyncio.sleep(0.01)
        await controller._on_error({"message": error_text, "fatal": False})

    controller._supervisor.send_chat_to_manager = fake_send


def _wire_success(controller, conversation_id: str) -> None:
    async def fake_send(msg):
        await asyncio.sleep(0.01)
        await controller._on_response_final({
            "conversation_id": conversation_id,
            "context_key": "general_chat",
            "token_cost": 0.01,
            "session_id": "sess-ok",
        })

    controller._supervisor.send_chat_to_manager = fake_send


async def _turn(controller, conv: str) -> None:
    await controller.handle_chat_message({
        "context_key": "general_chat",
        "user_message": "Status?",
        "context_data": {},
        "conversation_id": conv,
    })


def _published_errors(mock_router) -> list[str]:
    return [
        c[0][0].get("content") or ""
        for c in mock_router.publish_event.call_args_list
        if c[0][0].get("type") == "manager_response"
    ]


class TestOAuthExpiryNeverWipesSession:
    @pytest.mark.asyncio
    async def test_single_oauth_failure_keeps_session_and_links_settings(
        self, controller, mock_sessions, mock_router,
    ):
        _wire_error(controller, OAUTH_ERROR)
        await _turn(controller, "conv-a1")

        # No transcript wipe, no false "conversation was reset" copy.
        mock_sessions.clear_session.assert_not_called()
        contents = _published_errors(mock_router)
        assert contents, "no error bubble published"
        bubble = contents[-1]
        assert "reset" not in bubble.lower()
        assert "authentication expired" in bubble.lower()
        assert SETTINGS_LINK in bubble
        assert "not lost" in bubble

    @pytest.mark.asyncio
    async def test_repeated_oauth_failures_never_trip_reset_backstop(
        self, controller, mock_sessions,
    ):
        """Account outages are exempt from the consecutive-error
        session-reset backstop — three failing turns, zero wipes."""
        _wire_error(controller, OAUTH_ERROR)
        for conv in ("conv-b1", "conv-b2", "conv-b3"):
            await _turn(controller, conv)
        mock_sessions.clear_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_oauth_failure_with_cli_exit_prefix(
        self, controller, mock_sessions, mock_router,
    ):
        """The session bridge folds stderr under the synthetic exit
        line — the combined shape still routes to the auth copy."""
        _wire_error(
            controller, "Claude CLI exited with code 1\n" + OAUTH_ERROR,
        )
        await _turn(controller, "conv-c1")
        mock_sessions.clear_session.assert_not_called()
        assert "authentication expired" in _published_errors(mock_router)[-1].lower()


class TestAuthExpiredLatch:
    @pytest.mark.asyncio
    async def test_one_full_bubble_then_short_notices(
        self, controller, mock_router,
    ):
        _wire_error(controller, OAUTH_ERROR)
        await _turn(controller, "conv-d1")
        await _turn(controller, "conv-d2")

        contents = _published_errors(mock_router)
        assert len(contents) == 2
        full, short = contents
        assert "could not be refreshed" in full
        assert "still expired" in short
        assert len(short) < len(full)
        # Both carry the Settings deep-link — the fix is one click away
        # from either bubble.
        assert SETTINGS_LINK in full and SETTINGS_LINK in short

    @pytest.mark.asyncio
    async def test_clean_turn_rearms_the_full_bubble(
        self, controller, mock_router,
    ):
        _wire_error(controller, OAUTH_ERROR)
        await _turn(controller, "conv-e1")
        _wire_success(controller, "conv-e2")
        await _turn(controller, "conv-e2")
        _wire_error(controller, OAUTH_ERROR)
        await _turn(controller, "conv-e3")

        contents = _published_errors(mock_router)
        # First and third turns both get the FULL explainer.
        assert "could not be refreshed" in contents[0]
        assert "could not be refreshed" in contents[-1]

    @pytest.mark.asyncio
    async def test_successful_probe_rearms_the_full_bubble(
        self, controller, mock_router,
    ):
        _wire_error(controller, OAUTH_ERROR)
        await _turn(controller, "conv-f1")
        controller.note_auth_probe(True)  # keepalive saw auth working
        await _turn(controller, "conv-f2")

        contents = _published_errors(mock_router)
        assert "could not be refreshed" in contents[0]
        assert "could not be refreshed" in contents[1]


class TestKeepaliveAuthDown:
    @pytest.mark.asyncio
    async def test_auth_down_surfaces_bare_exit_as_auth(
        self, controller, mock_sessions, mock_router,
    ):
        """With the keepalive's auth-down verdict set, even the useless
        synthetic exit line (UNKNOWN_FATAL) is surfaced as the auth
        outage — and it never consumes the session-reset backstop."""
        controller.note_auth_probe(False)
        _wire_error(controller, "Claude CLI exited with code 1")
        await _turn(controller, "conv-g1")
        await _turn(controller, "conv-g2")
        await _turn(controller, "conv-g3")

        mock_sessions.clear_session.assert_not_called()
        contents = _published_errors(mock_router)
        assert "authentication expired" in contents[0].lower()
        assert "still expired" in contents[1]

    @pytest.mark.asyncio
    async def test_without_auth_down_bare_exit_keeps_old_backstop(
        self, controller, mock_sessions,
    ):
        """Sanity: with NO auth-down verdict, the UNKNOWN_FATAL
        consecutive-error backstop still self-heals wedged sessions
        (the pre-existing contract is untouched)."""
        _wire_error(controller, "Claude CLI exited with code 1")
        await _turn(controller, "conv-h1")
        await _turn(controller, "conv-h2")
        mock_sessions.clear_session.assert_called_with("general_chat")
