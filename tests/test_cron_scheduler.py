"""End-to-end tick coverage for the communicator-side cron scheduler.

The cron loop is the user-facing automation surface ("schedule a
script to run at @daily"). It depends on three contracts holding at
once:

    1. Backend ``/cron/due`` returns active crons with ``next_run_at <=
       now`` and includes ``script_name`` + ``cron_id``.
    2. ``script_runner.execute`` accepts the cron's variable_overrides
       + cron_id and returns an ``execution_id`` synchronously (the
       script keeps running in the background).
    3. Backend ``/cron/{id}/fired`` accepts the execution_id, advances
       ``last_run_at``, and recomputes ``next_run_at`` from the
       expression.

QA round 4 flagged this chain as untested at the integration level.
This file exercises ``CronScheduler._tick`` directly with mocked
``httpx`` + a mocked ``ScriptRunner``, asserting every hop of the
chain happens in order with the correct payloads.

Backend-side test of the same chain (DB write + next_run_at advance)
lives at ``backend/tests/test_script_crons.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scripts.cron_scheduler import CronScheduler


def _due_response(cron_id: str = "cron-1") -> dict:
    """Synthesised ``/cron/due`` body — minimum fields the dispatch
    path actually reads."""
    return {
        "items": [
            {
                "id": cron_id,
                "script_id": "script-uuid",
                "script_name": "source-profiles",
                "name": "morning",
                "cron_expression": "@daily",
                "variable_overrides": {"COUNT": 50},
                "next_run_at": "2026-05-06T05:00:00+00:00",
                # Per-workstream output context — the runner uses
                # these to compute CUBICLE_OUTPUT_DIR. See QA #10.
                "workstream_short_code": "RC",
                "scope_readable_id": None,
            }
        ]
    }


class _FakeAsyncClient:
    """Minimal aiohttp/httpx stand-in. Records every request the
    scheduler issues so individual tests can assert on URL + body."""

    def __init__(self, due_payload: dict | None = None,
                 fired_status: int = 200) -> None:
        self.due_payload = due_payload or {"items": []}
        self.fired_status = fired_status
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args) -> None:
        pass

    async def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> MagicMock:
        # ``headers`` was added after CronScheduler started attaching
        # the CompanyToken Bearer header (CLI-010 / cron-401 fix).
        self.calls.append(("GET", url, params or {}, headers or {}))
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value=self.due_payload)
        resp.text = ""
        return resp

    async def post(
        self,
        url: str,
        json: dict | None = None,
        headers: dict | None = None,
    ) -> MagicMock:
        self.calls.append(("POST", url, json or {}, headers or {}))
        resp = MagicMock()
        resp.status_code = self.fired_status
        return resp


@pytest.mark.asyncio
async def test_tick_full_chain_due_dispatch_fired():
    """One tick: backend reports a due cron, scheduler dispatches it
    to the runner, then notifies the backend that it fired with the
    execution_id."""
    runner = MagicMock()
    runner.execute = AsyncMock(return_value="exec-2026-05-06T05-00-00-abc123")

    fake = _FakeAsyncClient(due_payload=_due_response("cron-1"))
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    with patch("src.scripts.cron_scheduler.httpx.AsyncClient",
               return_value=fake):
        await scheduler._tick()

    # 1. Fetch hits /cron/due with as_of param.
    get_calls = [c for c in fake.calls if c[0] == "GET"]
    assert len(get_calls) == 1
    assert get_calls[0][1].endswith("/api/offices/office-uuid/cron/due")
    assert "as_of" in get_calls[0][2]

    # 2. runner.execute was called with the cron's data — including
    # the workstream_short_code so CUBICLE_OUTPUT_DIR lands under
    # the right per-workstream subdirectory.
    runner.execute.assert_awaited_once()
    kwargs = runner.execute.call_args.kwargs
    assert kwargs["script_name"] == "source-profiles"
    assert kwargs["variable_overrides"] == {"COUNT": 50}
    assert kwargs["cron_id"] == "cron-1"
    assert kwargs["task_id"] is None
    assert kwargs["triggered_by"].startswith("cron:")
    assert kwargs["workstream_short_code"] == "RC"
    assert kwargs["scope_readable_id"] is None

    # 3. /cron/{id}/fired is POSTed with the execution_id from runner.
    post_calls = [c for c in fake.calls if c[0] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0][1].endswith(
        "/api/offices/office-uuid/cron/cron-1/fired"
    )
    assert post_calls[0][2] == {
        "execution_id": "exec-2026-05-06T05-00-00-abc123",
    }


@pytest.mark.asyncio
async def test_tick_with_no_due_crons_skips_dispatch_and_fired():
    """When the backend returns an empty list, the scheduler does
    not invoke the runner and does not POST /fired. The loop just
    waits for the next tick."""
    runner = MagicMock()
    runner.execute = AsyncMock()

    fake = _FakeAsyncClient(due_payload={"items": []})
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    with patch("src.scripts.cron_scheduler.httpx.AsyncClient",
               return_value=fake):
        await scheduler._tick()

    assert any(c[0] == "GET" for c in fake.calls)
    assert all(c[0] != "POST" for c in fake.calls)
    runner.execute.assert_not_called()


@pytest.mark.asyncio
async def test_tick_runner_filenotfound_still_notifies_backend():
    """If the runner can't find the script on disk, the scheduler
    must STILL notify /fired with an empty execution_id so the
    backend advances next_run_at — otherwise a permanently broken
    script would re-fire every minute, jamming the schedule. The
    backend's bootstrap_status filter (QA C1) is the long-term fix
    for the half-bootstrapped case; this is the safety net."""
    runner = MagicMock()
    runner.execute = AsyncMock(side_effect=FileNotFoundError("missing"))

    fake = _FakeAsyncClient(due_payload=_due_response("cron-2"))
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    with patch("src.scripts.cron_scheduler.httpx.AsyncClient",
               return_value=fake):
        await scheduler._tick()

    post_calls = [c for c in fake.calls if c[0] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0][1].endswith(
        "/api/offices/office-uuid/cron/cron-2/fired"
    )
    # execution_id is empty when the runner couldn't start.
    assert post_calls[0][2] == {"execution_id": ""}


@pytest.mark.asyncio
async def test_tick_runner_unexpected_exception_does_not_notify():
    """Distinct from FileNotFoundError: a runtime failure in the
    runner (asyncio cancel mid-spawn, deps install crash, etc.)
    should NOT advance next_run_at, because the schedule wasn't
    actually fired. The exception is caught by the per-cron try
    in _tick so other crons in the same batch keep dispatching."""
    runner = MagicMock()
    runner.execute = AsyncMock(side_effect=RuntimeError("disk full"))

    fake = _FakeAsyncClient(due_payload=_due_response("cron-3"))
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    with patch("src.scripts.cron_scheduler.httpx.AsyncClient",
               return_value=fake):
        await scheduler._tick()

    # No POST → backend didn't get told to advance the schedule.
    assert all(c[0] != "POST" for c in fake.calls)


@pytest.mark.asyncio
async def test_tick_dispatches_all_crons_when_one_fails():
    """Multiple due crons in one batch: a failure on cron #1 must
    not prevent cron #2 from running. Lock the per-cron isolation
    so one bad script doesn't jam the office's whole schedule."""
    runner = MagicMock()
    # First call raises, second returns an exec_id.
    runner.execute = AsyncMock(
        side_effect=[
            RuntimeError("cron-1 failed"),
            "exec-cron-2-ok",
        ],
    )

    payload = {
        "items": [
            _due_response("cron-1")["items"][0],
            {
                **_due_response("cron-2")["items"][0],
                "id": "cron-2",
                "name": "afternoon",
            },
        ]
    }
    fake = _FakeAsyncClient(due_payload=payload)
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    with patch("src.scripts.cron_scheduler.httpx.AsyncClient",
               return_value=fake):
        await scheduler._tick()

    # Both crons attempted.
    assert runner.execute.await_count == 2
    # Only cron-2 advanced (cron-1 raised before notify).
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert "/cron/cron-2/fired" in posts[0][1]
