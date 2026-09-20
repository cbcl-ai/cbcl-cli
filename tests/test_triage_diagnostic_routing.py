"""Pure health alerts must not prevent triage or weaken approval guards."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.backend_client import (
    task_has_pending_action_request,
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


def mock_api(monkeypatch, page, *, cooldown_at=None):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/tasks/task"):
            return httpx.Response(200, json={"last_blocked_triage_at": cooldown_at})
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


async def test_generic_approval_guard_still_counts_pure_diagnostics(monkeypatch):
    calls = mock_api(monkeypatch, {"items": [diagnostic()], "total": 1})
    assert await task_has_pending_action_request(
        "http://backend", "office", "task", "synthetic-token"
    ) is True
    assert calls[0].url.params["limit"] == "1"
    assert await skip_triage() is False
