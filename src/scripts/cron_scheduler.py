"""Script cron scheduler — wakes every minute, fires due crons.

Fetches the office's active crons from the backend and checks which
ones are due (``next_run_at <= now``). Each due cron is dispatched to
the ScriptRunner with its configured ``variable_overrides`` and a
``triggered_by='cron:<cron_name>'`` tag so the resulting
ScriptExecution can be traced back to the schedule.

After dispatch the backend is notified so it can advance
``last_run_at`` / ``next_run_at``. Backend is the source of truth for
schedule state — this module is a pure consumer that fires the
dispatch and reports back.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from src.scripts.script_runner import ScriptRunner

logger = logging.getLogger("cbcl.cron")

# How often to check the backend for due crons. 60s aligns with the
# coarsest cron resolution (one minute).
POLL_INTERVAL_SECONDS: float = 60.0

# T8.3.1 (03/#14): in-memory dedup window. After a cron's script is launched
# we record a monotonic timestamp; if the same cron shows up "due" again
# within this window (the backend's next_run_at wasn't advanced because the
# /fired notify failed), we SKIP the second fire. Wider than the poll interval
# so one missed notify is covered. Process-local (lost on restart) — the
# at-least-once-across-restarts contract is unchanged.
RECENT_FIRE_WINDOW_SECONDS: float = 2.5 * POLL_INTERVAL_SECONDS

# How long to wait between "no crons due" diagnostic log lines. The
# scheduler runs silently when due is empty (the common case), which
# is fine — but if a user creates a cron and it never fires, there's
# no visible signal pointing at the upstream cause (script
# bootstrap_status != complete, next_run_at in the past, is_active
# false). At this interval we emit a one-line summary of the
# scheduler's view of the world: how many active crons exist, how
# many would have been due, and a hint for the silent-skip case.
DIAGNOSTIC_LOG_INTERVAL_SECONDS: float = 15 * 60.0  # 15 minutes


class CronScheduler:
    """Polls the backend for due crons and dispatches them."""

    # Per-cron consecutive-failure threshold for the loud "consider
    # disabling" log line. Doesn't auto-disable — the user still owns
    # that decision — but surfaces the problem so it's spotted before
    # the next 60s tick re-fires.
    _BACKOFF_DISABLE_AT = 5

    def __init__(
        self,
        office_id: str,
        backend_url: str,
        script_runner: ScriptRunner,
        security_token: str | None = None,
    ) -> None:
        self._office_id = office_id
        self._backend_url = backend_url.rstrip("/")
        # Company Token — required since v2.4.0 tenancy auth. The
        # backend's /cron/due and /cron/{id}/fired endpoints sit under
        # the office-scoped tenancy gate; calls without the Bearer
        # header 401 silently and the cron loop stops working.
        self._security_token = security_token
        self._runner = script_runner
        self._running = False
        self._task: asyncio.Task | None = None
        # Tracks the last time we emitted the periodic "scheduler is
        # alive, here's what it sees" diagnostic. Initialised to 0 so
        # the first quiet tick after startup fires the diagnostic
        # within the first minute — gives the user a fast confirmation
        # signal that "cbcl is up, scheduler is polling".
        self._last_diagnostic_log_ts: float = 0.0
        # Per-cron consecutive-failure counter. Cleared on every
        # successful dispatch. Used by the retry-with-backoff path
        # to log loudly after _BACKOFF_DISABLE_AT consecutive fails
        # so a broken cron doesn't quietly hammer the daemon every
        # 60s indefinitely.
        self._consecutive_failures: dict[str, int] = {}
        # T8.3.1: cron_id → monotonic ts of last successful launch. Guards
        # against double-fire when a /fired notify fails and the backend keeps
        # reporting the cron due.
        self._recently_fired: dict[str, float] = {}

    def start(self) -> None:
        """Start the scheduler loop as a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Cron scheduler started for office %s", self._office_id)

    async def stop(self) -> None:
        """Stop the scheduler loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        # Sleep to the next minute boundary so our tick is aligned with
        # cron semantics (cron fires on minute boundaries).
        await self._sleep_to_next_minute()
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cron tick error")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _sleep_to_next_minute(self) -> None:
        now = datetime.now(timezone.utc)
        seconds_to_next = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(max(0.5, seconds_to_next))

    async def _tick(self) -> None:
        """Fetch due crons from backend, dispatch each."""
        now = datetime.now(timezone.utc)
        due = await self._fetch_due_crons(now)
        # ``_consecutive_failures`` entries can leak if a cron is
        # deleted mid-failure-streak (the success path pops the
        # entry, but a delete-while-failing doesn't). The leak is
        # bounded by the count of crons that EVER failed in this
        # daemon's lifetime — typically <10s of entries even in a
        # heavy-use office, and re-creating a cron with the same id
        # in PostgreSQL is essentially impossible (uuid4). We
        # intentionally do NOT prune by "missing from current due"
        # because failed crons are absent from /cron/due between
        # ticks (their next_run_at sits in the future after the
        # /cron/{id}/fired bump) — a naive prune would reset the
        # backoff counter and defeat ``_BACKOFF_DISABLE_AT``.
        if not due:
            # Quiet tick. Emit a periodic diagnostic so a user whose
            # cron isn't firing has a visible signal of "why" — the
            # scheduler is polling, but there are no crons due. The
            # extra ``/cron?active_only=true`` fetch surfaces the
            # gap between "registered crons" and "due crons" so the
            # user can correlate against next_run_at and
            # bootstrap_status in the script-detail UI.
            import time as _time
            mono = _time.monotonic()
            if mono - self._last_diagnostic_log_ts >= DIAGNOSTIC_LOG_INTERVAL_SECONDS:
                self._last_diagnostic_log_ts = mono
                await self._log_diagnostic_summary(now)
            return
        logger.info("Cron tick: %d due cron(s)", len(due))
        for cron in due:
            try:
                await self._dispatch(cron)
            except Exception:
                logger.exception(
                    "Failed to dispatch cron %s (script=%s)",
                    cron.get("id"), cron.get("script_name"),
                )

    async def _log_diagnostic_summary(self, now: datetime) -> None:
        """Periodic "scheduler view of the world" line.

        Fires every ``DIAGNOSTIC_LOG_INTERVAL_SECONDS`` of quiet
        ticks. Counts active crons (via ``/cron?active_only=true``)
        and contrasts with the empty ``/cron/due`` we just saw.

        When ``active > 0`` and ``due == 0``, the gap is the
        operator-visible signal that something upstream is
        suppressing the schedule (script bootstrap_status,
        next_run_at far in the future, etc.). The line names both
        suspects in the same log so the user can drill into the
        Script detail page from a single hint.

        Best-effort: any failure here is non-fatal and quiet — the
        scheduler keeps running on its main loop.
        """
        try:
            active_count = await self._count_active_crons()
        except Exception:
            # Don't let the diagnostic itself fail the tick. The
            # missing visibility is acceptable; the silent main
            # loop continues to do its job.
            logger.debug(
                "Cron diagnostic: could not fetch active count "
                "(non-fatal)", exc_info=True,
            )
            return
        if active_count <= 0:
            # No active crons → no surprise that nothing fires.
            # Emit at DEBUG so a healthy "no schedules registered"
            # office doesn't spam INFO every 15 min.
            logger.debug(
                "Cron diagnostic: 0 due / 0 active crons "
                "(office=%s)", self._office_id,
            )
            return
        logger.info(
            "Cron diagnostic: 0 due / %d active cron(s) (office=%s). "
            "If you expected one to fire, check (a) the cron's "
            "next_run_at via GET /scripts/{id}/crons (may be far in "
            "the future if the expression already-fired this period) "
            "and (b) the script's bootstrap_status (must be "
            "'complete'; pending/failed scripts are silently skipped "
            "by /cron/due).",
            active_count, self._office_id,
        )

    async def _count_active_crons(self) -> int:
        """Count active crons in the office. Single ``GET /cron`` with
        ``active_only=true``. Used only by the diagnostic summary;
        not called on every tick."""
        url = (
            f"{self._backend_url}/api/offices/{self._office_id}/cron"
        )
        from src.backend_client import auth_headers
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                params={"active_only": "true"},
                headers=auth_headers(self._security_token),
            )
            if resp.status_code != 200:
                logger.debug(
                    "Cron diagnostic: /cron returned %s",
                    resp.status_code,
                )
                return 0
            data = resp.json()
            # The endpoint returns a bare list (not {items: [...]}).
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict) and "items" in data:
                return len(data.get("items") or [])
            return 0

    async def _fetch_due_crons(self, now: datetime) -> list[dict]:
        """Fetch active crons whose next_run_at <= now from the backend."""
        url = (
            f"{self._backend_url}/api/offices/{self._office_id}"
            f"/cron/due"
        )
        from src.backend_client import auth_headers

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url,
                    params={"as_of": now.isoformat()},
                    headers=auth_headers(self._security_token),
                )
                if resp.status_code == 200:
                    return resp.json().get("items", [])
                logger.warning(
                    "Cron /due returned %s: %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception as exc:
            from src.utils import describe_exception
            logger.warning(
                "Failed to fetch due crons: %s", describe_exception(exc),
            )
        return []

    async def _dispatch(self, cron: dict) -> None:
        script_name = cron.get("script_name", "")
        cron_id = cron.get("id", "")
        cron_name = cron.get("name", "")
        overrides = cron.get("variable_overrides") or {}
        if not script_name or not cron_id:
            return

        # T8.3.1: double-fire guard. If we launched this cron within the recent
        # window, the backend re-reporting it "due" means a prior /fired notify
        # failed — do NOT launch the script again. Log so a persistent notify
        # failure is visible rather than silently absorbed.
        import time as _t
        # Opportunistic prune (review L1): evict stamps older than the window so
        # _recently_fired can't grow unbounded across the daemon's lifetime.
        _now_mono = _t.monotonic()
        if len(self._recently_fired) > 64:
            self._recently_fired = {
                cid: ts for cid, ts in self._recently_fired.items()
                if _now_mono - ts < RECENT_FIRE_WINDOW_SECONDS
            }
        last = self._recently_fired.get(cron_id)
        if last is not None and (_t.monotonic() - last) < RECENT_FIRE_WINDOW_SECONDS:
            logger.warning(
                "Cron %s re-reported due within %.0fs of its last launch — "
                "suppressing a double-fire (the /fired notify likely failed; "
                "retrying the schedule advance).",
                cron_id, RECENT_FIRE_WINDOW_SECONDS,
            )
            # Best-effort: retry advancing the schedule so it stops re-firing.
            # If it succeeds now, clear the guard so the NEXT legitimately-due
            # tick fires (don't keep suppressing once the schedule advanced).
            if await self._notify_backend_fired(cron_id, ""):
                self._recently_fired.pop(cron_id, None)
            return

        # Overlap-skip: if a previous execution of this script is
        # still running (host-tracked), DON'T fire a second one. Do
        # NOT advance ``next_run_at`` — pre-fix posture was to call
        # ``_notify_backend_fired(cron_id, "")`` which silently bumped
        # the schedule forward. A 5-min cron whose script takes 6 min
        # would lose the next scheduled fire every overlap cycle
        # (skip → bump → 5 min later still running → bump → ...) and
        # never catch up. Now we leave the schedule alone: the next
        # tick (60s later) re-evaluates ``next_run_at <= now``; if
        # the long-running execution has finished by then, the cron
        # fires; if not, we skip again. Worst case: a script that
        # runs longer than its interval skips every tick but never
        # silently loses ground.
        if self._runner.has_active_script(script_name):
            logger.info(
                "Cron %s: skipping — previous execution of '%s' still "
                "running. next_run_at NOT advanced; will retry on the "
                "next tick.",
                cron_id, script_name,
            )
            self._consecutive_failures.pop(cron_id, None)
            return

        try:
            exec_id = await self._runner.execute(
                script_name=script_name,
                variable_overrides=overrides,
                task_id=None,
                triggered_by=f"cron:{cron_name or cron_id[:8]}",
                cron_id=cron_id,
                # Backend resolves these from the script's
                # ``created_from_task_id`` so cron runs land their
                # outputs under the originating workstream's dir
                # rather than the legacy flat root.
                workstream_short_code=cron.get("workstream_short_code") or None,
                scope_readable_id=cron.get("scope_readable_id") or None,
            )
        except FileNotFoundError:
            logger.error(
                "Cron %s: script '%s' not found on disk — publishing a "
                "failed run and advancing next_run_at",
                cron_id, script_name,
            )
            await self._publish_cron_failure(
                cron_id=cron_id, script_name=script_name,
                cron_name=cron_name,
                reason=(
                    f"Cron run refused: script '{script_name}' is not on "
                    "the daemon's workspace (delete the schedule or "
                    "re-bootstrap the script)."
                ),
            )
            self._consecutive_failures[cron_id] = (
                self._consecutive_failures.get(cron_id, 0) + 1
            )
            self._maybe_warn_repeated_failures(cron_id)
            await self._notify_backend_fired(cron_id, "")
            return
        except Exception as exc:  # noqa: BLE001
            # ADD-C5: a cron dispatch refusal used to silently advance
            # next_run_at with NO failed event + NO history row, so a
            # broken/secret-missing schedule "fired on time" but
            # produced nothing the user could see. Now every refusal
            # publishes a ``failed`` execution row with an actionable
            # message. We still advance next_run_at to avoid a 60s
            # retry-storm — but the failure is no longer invisible.
            #
            # User-fixable refusals (a missing / corrupt office secret)
            # are NOT counted toward the "consistently broken" backoff
            # warning: they're parked waiting on the user, not broken
            # code. Their failed-row message points at Settings →
            # Security so the user knows exactly what to fix.
            from src.scripts.script_runner import (
                MissingOfficeSecretError,
                OfficeSecretsCorruptError,
            )

            if isinstance(exc, MissingOfficeSecretError):
                reason = (
                    "Cron run refused — missing office secret(s): "
                    f"{', '.join(exc.missing)}. Add them in Settings → "
                    "Security → Office Secrets; the next scheduled run "
                    "will pick them up automatically."
                )
                user_fixable = True
            elif isinstance(exc, OfficeSecretsCorruptError):
                reason = (
                    "Cron run refused — the office secrets file is "
                    f"corrupt: {exc}. Fix it in Settings → Security → "
                    "Office Secrets."
                )
                user_fixable = True
            else:
                reason = f"Cron run failed to dispatch: {exc}"
                user_fixable = False

            logger.warning(
                "Cron %s dispatch refused (%s) — publishing failed run, "
                "advancing next_run_at: %s",
                cron_id,
                "user-fixable" if user_fixable else "error",
                reason,
            )
            await self._publish_cron_failure(
                cron_id=cron_id, script_name=script_name,
                cron_name=cron_name, reason=reason,
            )
            if user_fixable:
                # Parked on the user — don't let it trip the broken-cron
                # warning, and don't accumulate a misleading streak.
                self._consecutive_failures.pop(cron_id, None)
            else:
                self._consecutive_failures[cron_id] = (
                    self._consecutive_failures.get(cron_id, 0) + 1
                )
                self._maybe_warn_repeated_failures(cron_id)
            await self._notify_backend_fired(cron_id, "")
            return

        # Success — clear the consecutive-failure counter and stamp the
        # recent-fire guard (T8.3.1) BEFORE the notify, so even if the notify
        # fails the next tick's double-fire guard suppresses a re-launch.
        self._consecutive_failures.pop(cron_id, None)
        import time as _t
        self._recently_fired[cron_id] = _t.monotonic()
        # If the notify SUCCEEDS the backend has advanced next_run_at past the
        # window, so the next "due" report is a LEGITIMATE fire — clear the
        # guard so a short-interval cron (interval < RECENT_FIRE_WINDOW_SECONDS,
        # e.g. ``* * * * *``) isn't wrongly suppressed. The guard only stays
        # set when the notify FAILED (schedule un-advanced → real double-fire).
        if await self._notify_backend_fired(cron_id, exec_id):
            self._recently_fired.pop(cron_id, None)

    def _maybe_warn_repeated_failures(self, cron_id: str) -> None:
        """Loud log when a cron has failed ``_BACKOFF_DISABLE_AT``
        consecutive dispatches. Doesn't auto-disable (the user owns that
        decision) — but combined with the per-run ``failed`` rows the
        problem is now visible instead of silent (ADD-C5)."""
        count = self._consecutive_failures.get(cron_id, 0)
        if count >= self._BACKOFF_DISABLE_AT:
            logger.error(
                "Cron %s has failed %d consecutive dispatches — consider "
                "disabling it from the UI until the underlying issue is "
                "fixed (each attempt now also shows as a failed run).",
                cron_id, count,
            )

    async def _publish_cron_failure(
        self,
        *,
        cron_id: str,
        script_name: str,
        cron_name: str | None,
        reason: str,
    ) -> None:
        """Publish a synthetic ``script_status: failed`` event so a cron
        dispatch refusal is VISIBLE in the Execution History + UI
        instead of silently advancing the schedule (ADD-C5). Mirrors the
        manual path's ``dispatch._publish_refusal``."""
        router = getattr(self._runner, "_router", None)
        if router is None:
            return
        from uuid import uuid4
        now = datetime.now(timezone.utc).isoformat()
        try:
            await router.publish_event({
                "type": "script_status",
                "script_name": script_name,
                "execution_id": f"cron-refused-{uuid4().hex[:8]}",
                "status": "failed",
                "task_id": None,
                "cron_id": cron_id,
                "triggered_by": f"cron:{cron_name or cron_id[:8]}",
                "started_at": now,
                "completed_at": now,
                "duration_seconds": 0,
                "error_message": reason,
                "progress": None,
            })
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to publish cron failure event for '%s'",
                script_name,
            )

    async def _notify_backend_fired(
        self, cron_id: str, execution_id: str
    ) -> bool:
        """Tell the backend we dispatched this cron so it advances
        last_run_at / next_run_at.

        Returns True iff the backend confirmed (2xx) — the caller clears the
        recent-fire guard on success so a legitimately-due short-interval cron
        (next_run_at now advanced past the window) can fire again. Returns
        False on failure-after-retries, leaving the guard in place to suppress
        a genuine double-fire while the un-advanced schedule keeps re-reporting.
        """
        url = (
            f"{self._backend_url}/api/offices/{self._office_id}"
            f"/cron/{cron_id}/fired"
        )
        from src.backend_client import auth_headers

        # T8.3.1: retry with short backoff (was single-shot). A failed notify
        # leaves next_run_at un-advanced → the cron re-reports due → the
        # in-memory recent-fire guard suppresses the double-launch, but we
        # still want to advance the schedule so it stops re-firing.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        url,
                        json={"execution_id": execution_id},
                        headers=auth_headers(self._security_token),
                    )
                # A non-2xx is a successful round-trip but the backend did
                # NOT advance next_run_at (rejected) — treat it as a failure
                # so it enters the retry loop and is logged loudly. Without
                # this, a 401 (revoked token) / 5xx silently "succeeds" and
                # the only thing preventing repeated double-fires is the
                # process-local in-memory guard (lost on restart).
                resp.raise_for_status()
                return True
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
        logger.warning(
            "Failed to notify backend of cron fire after 3 attempts: %s (%s)",
            cron_id, last_exc,
        )
        return False
