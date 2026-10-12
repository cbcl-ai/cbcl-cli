"""A queued auto-decide turn is dropped once its request is decided.

When the daemon's WebSocket reconnects while a block-notice turn presents a
deferred card, the backend re-sends that card's auto-decide poke: the claim
was made on the earlier connection, so the backend cannot know the turn
survived. The poke waited behind the notice turn and then asked the Manager
about a card it had just decided (blk-delivery follow-up).

The daemon now reads the request as the queued turn would start, inside the
chat lock, and drops the turn when the request is no longer pending. A
failed or inconclusive read runs the turn, as before the check existed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import src.orchestrator._manager_action_requests as mar
from src.backend_client import action_request_is_pending
from src.orchestrator.manager_controller import ManagerController

WORKSTREAM = "4f1d2c3b-0000-4000-8000-000000000001"


# ── The lookup ─────────────────────────────────────────────────────────────


def _mock_list(monkeypatch, response):
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, int):
            return httpx.Response(response)
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    return calls


def _row(request_id: str) -> dict:
    return {"id": request_id, "status": "pending"}


async def _lookup(workstream_id: str | None = WORKSTREAM) -> bool | None:
    return await action_request_is_pending(
        "http://backend",
        "office",
        "req-1",
        request_type="escalate_blocker",
        workstream_id=workstream_id,
        security_token="synthetic-token",
    )


@pytest.mark.asyncio
async def test_a_listed_request_is_pending(monkeypatch):
    calls = _mock_list(
        monkeypatch, {"items": [_row("other"), _row("req-1")], "total": 2}
    )
    assert await _lookup() is True
    (request,) = calls
    assert request.url.path == "/api/offices/office/action-requests"
    assert dict(request.url.params) == {
        "status": "pending",
        "request_type": "escalate_blocker",
        "limit": "500",
        "workstream_id": WORKSTREAM,
    }
    assert request.headers["authorization"] == "Bearer synthetic-token"


@pytest.mark.asyncio
async def test_a_complete_page_without_the_request_means_it_was_decided(monkeypatch):
    _mock_list(monkeypatch, {"items": [_row("other")], "total": 1})
    assert await _lookup() is False


@pytest.mark.asyncio
async def test_without_a_workstream_the_lookup_reads_the_whole_office(monkeypatch):
    calls = _mock_list(monkeypatch, {"items": [], "total": 0})
    assert await _lookup(workstream_id=None) is False
    assert "workstream_id" not in dict(calls[0].url.params)


@pytest.mark.parametrize(
    "response",
    [
        {"items": [_row("other")], "total": 2},  # a partial page
        {"items": [_row(f"r{n}") for n in range(500)], "total": 500},  # a full page
        {"items": [_row("req-1") | {"status": "approved"}], "total": 1},
        {"items": "nope", "total": 1},
        {"items": [], "total": "1"},
        {"items": ["nope"], "total": 1},
        ["nope"],
        401,
        500,
        httpx.ConnectError("synthetic outage"),
    ],
)
@pytest.mark.asyncio
async def test_any_doubtful_read_is_unknown(monkeypatch, response):
    _mock_list(monkeypatch, response)
    assert await _lookup() is None


# ── The controller drops a turn nothing is owed for ─────────────────────────


@pytest.fixture
def controller():
    router = MagicMock()
    router.ws_client.request = AsyncMock()
    instance = ManagerController(MagicMock(), router, MagicMock(), MagicMock())
    instance._handle_chat_message_locked = AsyncMock(return_value=True)
    return instance


def _poke(still_owed) -> dict:
    return {
        "context_key": f"workstream:{WORKSTREAM}",
        "user_message": "[Action Request — Auto-Decide: escalate_blocker]",
        "conversation_id": "auto-decide-req-1-attempt-1",
        "still_owed": still_owed,
        "_turn_outcome": {},
    }


@pytest.mark.asyncio
async def test_a_turn_whose_request_was_decided_does_not_run(controller):
    check = AsyncMock(return_value=False)
    assert await controller.handle_chat_message(_poke(check), source="script") is True
    check.assert_awaited_once()
    controller._handle_chat_message_locked.assert_not_awaited()


@pytest.mark.parametrize("answer", [True, None])
@pytest.mark.asyncio
async def test_a_pending_or_unknown_request_runs_the_turn(controller, answer):
    check = AsyncMock(return_value=answer)
    assert await controller.handle_chat_message(_poke(check), source="script") is True
    controller._handle_chat_message_locked.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_check_runs_the_turn(controller):
    check = AsyncMock(side_effect=RuntimeError("boom"))
    assert await controller.handle_chat_message(_poke(check), source="script") is True
    controller._handle_chat_message_locked.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_check_runs_once_the_turn_holds_the_chat_lock(controller):
    """Read before the lock, the check would miss what the turn ahead settles."""
    check = AsyncMock(return_value=True)
    await controller._chat_lock.acquire()
    turn = asyncio.create_task(
        controller.handle_chat_message(_poke(check), source="script")
    )
    try:
        await asyncio.sleep(0.05)
        check.assert_not_awaited()
    finally:
        controller._chat_lock.release()
    assert await asyncio.wait_for(turn, timeout=5) is True
    check.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_user_turn_is_never_checked(controller):
    check = AsyncMock(return_value=False)
    message = _poke(check) | {"turn_id": None}
    assert await controller.handle_chat_message(message, source="user") is True
    check.assert_not_awaited()
    controller._handle_chat_message_locked.assert_awaited_once()


# ── The auto-decide ingest end to end ───────────────────────────────────────


def _auto_decide_event() -> dict:
    return {
        "context_key": f"workstream:{WORKSTREAM}",
        "request_id": "req-1",
        "request_type": "escalate_blocker",
        "severity": "medium",
        "category": "workstream",
        "source_task_id": "task-1",
        "payload": {"blocker_summary": "Waiting on the export."},
        "justification": "Blocked.",
        "conv_suffix": "reconnect",
    }


@pytest.fixture
def office_controller(controller):
    controller._backend_url = "http://backend"
    controller._security_token = "synthetic-token"
    controller._office_id = "office"
    controller._config.get_workstream = MagicMock(return_value=None)
    controller._workspace_path = ""
    return controller


@pytest.mark.parametrize("decided_by_the_turn_ahead", [True, False])
@pytest.mark.asyncio
async def test_a_poke_queued_behind_the_turn_that_decides_its_card(
    office_controller, decided_by_the_turn_ahead
):
    """The notice turn holds the poke queue while it runs. The re-sent poke
    waits for it, then reads the card: decided, nothing runs; still pending,
    the Manager is asked as before."""
    decided = False
    notice_running = asyncio.Event()
    finish_notice = asyncio.Event()

    async def turn(message):
        nonlocal decided
        if message["conversation_id"].startswith("task-blocked"):
            notice_running.set()
            await finish_notice.wait()
            decided = decided_by_the_turn_ahead
        return True

    office_controller._handle_chat_message_locked = AsyncMock(side_effect=turn)

    async def lookup(*_args, **_kwargs):
        return not decided

    pending = AsyncMock(side_effect=lookup)
    with patch("src.backend_client.action_request_is_pending", pending):
        notice = asyncio.create_task(
            mar._dispatch_poke(
                office_controller,
                {
                    "context_key": f"workstream:{WORKSTREAM}",
                    "user_message": "[Task Blocked — 1 task]",
                    "conversation_id": "task-blocked-1",
                },
                mark_on_success=False,
            )
        )
        await asyncio.wait_for(notice_running.wait(), timeout=5)
        poke = asyncio.create_task(
            mar.ingest_action_request_auto_decide(
                office_controller, _auto_decide_event()
            )
        )
        await asyncio.sleep(0.05)
        pending.assert_not_awaited()  # still queued behind the notice turn
        finish_notice.set()
        await asyncio.wait_for(asyncio.gather(notice, poke), timeout=5)

    ran = [
        call.args[0]["conversation_id"]
        for call in office_controller._handle_chat_message_locked.await_args_list
    ]
    assert len(ran) == (1 if decided_by_the_turn_ahead else 2)
    assert ran[0].startswith("task-blocked-1-attempt-")
    assert all(name.startswith("auto-decide-req-1-") for name in ran[1:])
    pending.assert_awaited_once()
    assert pending.await_args.args == ("http://backend", "office", "req-1")
    assert pending.await_args.kwargs == {
        "request_type": "escalate_blocker",
        "workstream_id": WORKSTREAM,
        "security_token": "synthetic-token",
    }


@pytest.mark.asyncio
async def test_a_poke_the_lookup_cannot_serve_runs_without_asking(office_controller):
    """No backend URL or token (older wiring), or a poke without a type."""
    pending = AsyncMock(return_value=False)
    office_controller._security_token = ""
    with patch("src.backend_client.action_request_is_pending", pending):
        await mar.ingest_action_request_auto_decide(
            office_controller, _auto_decide_event()
        )
        office_controller._security_token = "synthetic-token"
        untyped = _auto_decide_event() | {"conv_suffix": "repoke"}
        del untyped["request_type"]
        await mar.ingest_action_request_auto_decide(office_controller, untyped)
    pending.assert_not_awaited()
    assert office_controller._handle_chat_message_locked.await_count == 2
