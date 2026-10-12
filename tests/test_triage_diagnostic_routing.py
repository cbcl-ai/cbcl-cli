"""Pure health alerts must not prevent triage or weaken approval guards."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.backend_client import (
    task_has_pending_triage_decision,
    task_should_skip_ma_routing,
)


def diagnostic(**overrides):
    return {
        "id": "diagnostic",
        "office_id": "office",
        "source_task_id": "task",
        "status": "pending",
        "request_type": "escalate_blocker",
        "requesting_agent": "system-sweeper",
        "category": "workstream",
        "requires_user": True,
        "payload": {"sweeper_signals": {"stuck_ready": {}}},
        **overrides,
    }


def mock_api(monkeypatch, page, *, cooldown_at=None, task_extra=None):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/tasks/task"):
            return httpx.Response(
                200,
                json={"last_blocked_triage_at": cooldown_at, **(task_extra or {})},
            )
        if isinstance(page, Exception):
            raise page
        if isinstance(page, int):
            return httpx.Response(page)
        return httpx.Response(200, json=page)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    return calls


async def lookup():
    return await task_has_pending_triage_decision(
        "http://backend", "office", "task", "synthetic-token"
    )


async def skip_triage():
    return await task_should_skip_ma_routing(
        "http://backend", "office", "task", "synthetic-token"
    )


@pytest.mark.parametrize("signal", ["stuck_ready", "stuck_review", "workstream_stall"])
@pytest.mark.parametrize("requires_user", [True, False])
async def test_pure_pending_alert_does_not_skip_triage(monkeypatch, signal, requires_user):
    row = diagnostic(
        requires_user=requires_user,
        category="infrastructure" if signal == "workstream_stall" else "workstream",
        payload={"sweeper_signals": {signal: {}}},
    )
    calls = mock_api(monkeypatch, {"items": [row], "total": 1})
    assert await skip_triage() is False
    assert len(calls) == 2  # The cooldown check remains independent.
    assert calls[0].url.params["status"] == "pending"
    assert calls[0].url.params["source_task_id"] == "task"
    assert calls[0].url.params["limit"] == "500"
    assert "offset" not in calls[0].url.params
    assert calls[0].headers["Authorization"] == "Bearer synthetic-token"
    assert row["status"] == "pending"
    assert all(call.method == "GET" for call in calls)


@pytest.mark.parametrize("row", [
    diagnostic(category="credentials"),
    diagnostic(category="user_input"),
    diagnostic(requesting_agent="engineer"),
    diagnostic(request_type="request_user_action"),
    diagnostic(request_type="request_clarification", requires_user=False),
    diagnostic(request_type="create_subtask", requires_user=False),
    diagnostic(request_type="informational", requires_user=False),
    diagnostic(request_type="review_hold"),
    diagnostic(payload={"sweeper_signals": {"stuck_ready": {}, "auth_failure": {}}}),
    diagnostic(payload={"sweeper_signals": {"stuck_ready": {}}, "rework_cap": True}),
    diagnostic(payload={"sweeper_signals": {"stuck_ready": {}}, "review_recovery": {}}),
    diagnostic(payload=None),
])
async def test_every_non_diagnostic_pending_request_still_skips_triage(monkeypatch, row):
    calls = mock_api(monkeypatch, {"items": [row], "total": 1})
    assert await skip_triage() is True
    assert len(calls) == 1


async def test_full_snapshot_finds_real_proposal_after_many_diagnostics(monkeypatch):
    rows = [diagnostic(id=f"diagnostic-{index}") for index in range(150)]
    rows.append(diagnostic(id="proposal", request_type="create_subtask"))
    calls = mock_api(monkeypatch, {"items": rows, "total": len(rows)})
    assert await skip_triage() is True
    assert len(calls) == 1
    assert calls[0].url.params["limit"] == "500"


@pytest.mark.parametrize("page", [
    503,
    httpx.ConnectError("synthetic outage"),
    [],
    {},
    {"items": None, "total": 1},
    {"items": [], "total": True},
    {"items": [], "total": -1},
    {"items": [], "total": 1},
    {"items": [diagnostic()], "total": 2},
    {"items": [None], "total": 1},
    {"items": [diagnostic(office_id="other")], "total": 1},
    {"items": [diagnostic(source_task_id="other")], "total": 1},
    {"items": [diagnostic(status="superseded")], "total": 1},
    {"items": [diagnostic(id=None)], "total": 1},
    {"items": [diagnostic(), diagnostic()], "total": 2},
    {"items": [diagnostic(id=str(index)) for index in range(500)], "total": 500},
    {"items": [diagnostic(id=str(index)) for index in range(500)], "total": 501},
])
async def test_invalid_or_incomplete_snapshot_is_unknown(monkeypatch, page):
    mock_api(monkeypatch, page)
    assert await lookup() is None


@pytest.mark.parametrize("page", [
    {"items": [diagnostic()], "total": 1},
    {"items": [], "total": 0},
    {"items": [diagnostic()], "total": 2},
    503,
])
@pytest.mark.parametrize("recent", [True, False])
async def test_cooldown_remains_effective_for_diagnostics_and_unknown_reads(
    monkeypatch, page, recent
):
    monkeypatch.setenv("CUBICLE_BLOCKED_TRIAGE_COOLDOWN_SECONDS", "3600")
    cooldown_at = datetime.now(timezone.utc) - timedelta(seconds=30 if recent else 7200)
    calls = mock_api(monkeypatch, page, cooldown_at=cooldown_at.isoformat())
    # Unknown keeps the documented fail-open triage policy, but it still
    # cannot bypass a known recent MA-triage cooldown.
    assert await skip_triage() is recent
    assert calls[-1].url.path.endswith("/tasks/task")


@pytest.mark.parametrize(
    ("decision", "skips"), [("rejected", True), ("retried", False), (None, False)]
)
async def test_a_person_rejection_of_the_bounce_cap_card_skips_triage(
    monkeypatch, decision, skips
):
    # A person left the task blocked: the triage could only post a synthesis
    # comment, so it is not launched every cooldown. The field rides the task
    # GET the cooldown already reads; an older backend omits it.
    calls = mock_api(
        monkeypatch,
        {"items": [], "total": 0},
        task_extra={"bounce_cap_decision": decision},
    )
    assert await skip_triage() is skips
    assert len(calls) == 2
    assert calls[-1].url.path.endswith("/tasks/task")


_MISSING = object()


@pytest.mark.parametrize(
    ("waiting_on", "skips"),
    [
        ("person_declined", True),
        ("person_kept_blocked", True),
        ("triage", False),
        ("manager", False),
        ("dependency", False),
        (None, False),
        (_MISSING, False),
    ],
)
async def test_a_person_decision_about_the_task_skips_triage(
    monkeypatch, waiting_on, skips
):
    # The backend's wait classifier says a person already decided about this
    # Blocked task (rejected its bounce-cap card, or declined another blocker
    # card with nothing asked since): a triage would ask them the same thing
    # again. Every other state, and an older backend that omits the field,
    # routes as before.
    extra = {} if waiting_on is _MISSING else {"blocked_waiting_on": waiting_on}
    calls = mock_api(monkeypatch, {"items": [], "total": 0}, task_extra=extra)
    assert await skip_triage() is skips
    assert len(calls) == 2
    assert calls[-1].url.path.endswith("/tasks/task")


async def test_a_pending_request_after_a_decline_still_skips_triage(monkeypatch):
    # A request filed after the decline is the task's current question: the
    # backend then reports that request's owner, and the pending-request check
    # alone holds triage (the task read is not even needed).
    row = diagnostic(requesting_agent="engineer", category="user_input")
    calls = mock_api(
        monkeypatch,
        {"items": [row], "total": 1},
        task_extra={"blocked_waiting_on": "user"},
    )
    assert await skip_triage() is True
    assert len(calls) == 1
