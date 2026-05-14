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


class CronScheduler:
    """Polls the backend for due crons and dispatches them."""

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
        if not due:
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
            logger.warning("Failed to fetch due crons: %s", exc)
        return []

    async def _dispatch(self, cron: dict) -> None:
        script_name = cron.get("script_name", "")
        cron_id = cron.get("id", "")
        cron_name = cron.get("name", "")
        overrides = cron.get("variable_overrides") or {}
        if not script_name or not cron_id:
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
                "Cron %s: script '%s' not found on disk — marking fired anyway "
                "so backend advances next_run_at",
                cron_id, script_name,
            )
            exec_id = ""
        except Exception:
            logger.exception("Cron %s dispatch failed", cron_id)
            return

        await self._notify_backend_fired(cron_id, exec_id)

    async def _notify_backend_fired(
        self, cron_id: str, execution_id: str
    ) -> None:
        """Tell the backend we dispatched this cron so it advances
        last_run_at / next_run_at."""
        url = (
            f"{self._backend_url}/api/offices/{self._office_id}"
            f"/cron/{cron_id}/fired"
        )
        from src.backend_client import auth_headers

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    url,
                    json={"execution_id": execution_id},
                    headers=auth_headers(self._security_token),
                )
        except Exception:
            logger.warning(
                "Failed to notify backend of cron fire: %s", cron_id,
            )
