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

import httpx
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

        def _raise_for_status() -> None:
            # Mirror httpx: raise on 4xx/5xx, no-op on 2xx. The cron
            # fix (T8.3.1 hardening) relies on this so a 401/500 is
            # treated as a failure instead of a silent schedule-advance.
            if self.fired_status >= 400:
                raise httpx.HTTPStatusError(
                    f"{self.fired_status}", request=MagicMock(), response=resp,
                )

        resp.raise_for_status = _raise_for_status
        return resp


@pytest.mark.asyncio
async def test_tick_full_chain_due_dispatch_fired():
    """One tick: backend reports a due cron, scheduler dispatches it
    to the runner, then notifies the backend that it fired with the
    execution_id."""
    runner = MagicMock()
    runner.execute = AsyncMock(return_value="exec-2026-05-06T05-00-00-abc123")
    # Overlap-skip guard (cbcl 0.2.50+) — every cron tick first checks
    # whether a previous execution of the same script is still in
    # flight. Mock returns False so the dispatch proceeds.
    runner.has_active_script = MagicMock(return_value=False)

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
    runner.has_active_script = MagicMock(return_value=False)
    runner._router = AsyncMock()  # ADD-C5: capture the failed-event publish

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
    # ADD-C5: a failed run is now VISIBLE (history row + UI), not silent.
    runner._router.publish_event.assert_awaited()
    event = runner._router.publish_event.await_args[0][0]
    assert event["type"] == "script_status"
    assert event["status"] == "failed"
    assert event["cron_id"] == "cron-2"
    assert "not on" in event["error_message"]


@pytest.mark.asyncio
async def test_tick_runner_unexpected_exception_advances_next_run():
    """Behaviour CHANGED in cbcl 0.2.50: a runtime failure during
    dispatch (DepsInstallError on a broken requirements.txt, asyncio
    cancel mid-spawn, etc.) now ADVANCES next_run_at via /fired
    instead of leaving the cron stuck in the past. Without this
    retry-with-backoff, a persistently-broken script hammered the
    daemon every 60s indefinitely. The empty execution_id signals
    "no row was created"."""
    runner = MagicMock()
    runner.execute = AsyncMock(side_effect=RuntimeError("disk full"))
    runner.has_active_script = MagicMock(return_value=False)
    runner._router = AsyncMock()

    fake = _FakeAsyncClient(due_payload=_due_response("cron-3"))
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    with patch("src.scripts.cron_scheduler.httpx.AsyncClient",
               return_value=fake):
        await scheduler._tick()

    # POST IS made now — backend advances next_run_at so the broken
    # cron doesn't tick every 60s. Empty execution_id signals
    # "dispatch failed, no row created".
    post_calls = [c for c in fake.calls if c[0] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0][2] == {"execution_id": ""}
    # ADD-C5: failed run published + counts toward the broken-cron warn.
    runner._router.publish_event.assert_awaited()
    assert scheduler._consecutive_failures.get("cron-3") == 1


@pytest.mark.asyncio
async def test_tick_missing_office_secret_publishes_failed_not_counted():
    """ADD-C5: a missing office secret is a USER-fixable refusal — it
    must publish a failed run (so the user sees it + the actionable
    message) but NOT advance the broken-cron streak (it's parked on the
    user, not broken code). next_run_at still advances so it retries on
    the next slot once the secret is added."""
    from src.scripts.script_runner import MissingOfficeSecretError

    runner = MagicMock()
    runner.execute = AsyncMock(
        side_effect=MissingOfficeSecretError(
            ["UNIPILE_API_KEY"], script_name="source-profiles",
        ),
    )
    runner.has_active_script = MagicMock(return_value=False)
    runner._router = AsyncMock()

    fake = _FakeAsyncClient(due_payload=_due_response("cron-sec"))
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    with patch("src.scripts.cron_scheduler.httpx.AsyncClient",
               return_value=fake):
        await scheduler._tick()

    # Advanced (avoid 60s spam) + visible failed row with the fix hint.
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][2] == {"execution_id": ""}
    runner._router.publish_event.assert_awaited()
    event = runner._router.publish_event.await_args[0][0]
    assert event["status"] == "failed"
    assert "UNIPILE_API_KEY" in event["error_message"]
    assert "Settings" in event["error_message"]
    # NOT counted toward the broken-cron backoff (parked on the user).
    assert "cron-sec" not in scheduler._consecutive_failures


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
    runner.has_active_script = MagicMock(return_value=False)

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
    # BOTH crons now advance via /fired (cbcl 0.2.50+ retry-with-
    # backoff): cron-1 with empty exec_id (failed), cron-2 with its
    # success exec_id. Was previously only 1 POST (cron-2) — the new
    # behaviour prevents the broken cron from re-firing every tick.
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 2
    cron1_post = next(p for p in posts if "/cron/cron-1/fired" in p[1])
    cron2_post = next(p for p in posts if "/cron/cron-2/fired" in p[1])
    assert cron1_post[2] == {"execution_id": ""}
    assert cron2_post[2] == {"execution_id": "exec-cron-2-ok"}


@pytest.mark.asyncio
async def test_double_fire_suppressed_within_window():
    """T8.3.1 — when the /fired notify FAILS, the backend doesn't advance the
    schedule and re-reports the cron due; the second dispatch within the window
    must NOT launch the script again (the guard stays set on a failed notify)."""
    runner = MagicMock()
    runner.execute = AsyncMock(return_value="exec-1")
    runner.has_active_script = MagicMock(return_value=False)

    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )
    cron = {"id": "cron-1", "script_name": "s1", "name": "c1",
            "variable_overrides": {}}

    # fired_status=500 → the /fired notify FAILS → guard stays set → suppress.
    fake = _FakeAsyncClient(due_payload=_due_response("cron-1"), fired_status=500)
    with patch("src.scripts.cron_scheduler.httpx.AsyncClient", return_value=fake):
        await scheduler._dispatch(cron)   # first launch (notify fails)
        await scheduler._dispatch(cron)   # re-reported due → must be suppressed

    assert runner.execute.await_count == 1, "cron double-fired"


@pytest.mark.asyncio
async def test_legit_refire_after_successful_notify_not_suppressed():
    """T8.3.1 (re-review) — a SUCCESSFUL /fired notify advances next_run_at, so
    a subsequent due report is a LEGITIMATE fire (e.g. a sub-window-interval
    cron like ``* * * * *``). The guard must clear on success and NOT suppress
    the next launch — otherwise short-interval crons silently under-fire."""
    runner = MagicMock()
    runner.execute = AsyncMock(return_value="exec-1")
    runner.has_active_script = MagicMock(return_value=False)

    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )
    cron = {"id": "cron-1", "script_name": "s1", "name": "c1",
            "variable_overrides": {}}

    fake = _FakeAsyncClient(due_payload=_due_response("cron-1"), fired_status=200)
    with patch("src.scripts.cron_scheduler.httpx.AsyncClient", return_value=fake):
        await scheduler._dispatch(cron)   # first launch (notify succeeds → guard cleared)
        await scheduler._dispatch(cron)   # legitimately due again → must fire

    assert runner.execute.await_count == 2, (
        "legit re-fire after a successful notify was wrongly suppressed"
    )


@pytest.mark.asyncio
async def test_notify_fired_non_2xx_retries_not_silent_success():
    """A non-2xx /fired response (e.g. 401 revoked token, 500) is a
    successful HTTP round-trip but the backend did NOT advance the
    schedule — it must be treated as a failure (retried 3x, then logged),
    NOT a silent success. Otherwise the cron re-fires with only the
    process-local in-memory guard for protection."""
    runner = MagicMock()
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    fake = _FakeAsyncClient(fired_status=401)
    with patch("src.scripts.cron_scheduler.httpx.AsyncClient", return_value=fake):
        # Must not raise out of the notifier (it swallows after retries).
        await scheduler._notify_backend_fired("cron-1", "exec-1")

    post_calls = [c for c in fake.calls if c[0] == "POST"]
    # 3 attempts because raise_for_status() raised each time.
    assert len(post_calls) == 3, "non-2xx /fired should be retried, not accepted"


@pytest.mark.asyncio
async def test_notify_fired_2xx_single_attempt():
    """A 2xx /fired is accepted on the first try (no spurious retries)."""
    runner = MagicMock()
    scheduler = CronScheduler(
        office_id="office-uuid",
        backend_url="http://test-backend:8000",
        script_runner=runner,
    )

    fake = _FakeAsyncClient(fired_status=200)
    with patch("src.scripts.cron_scheduler.httpx.AsyncClient", return_value=fake):
        await scheduler._notify_backend_fired("cron-1", "exec-1")

    post_calls = [c for c in fake.calls if c[0] == "POST"]
    assert len(post_calls) == 1
