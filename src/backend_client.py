"""Thin wrapper around httpx that always attaches the Company Token.

The cbcl daemon makes ~30 HTTP calls into the platform backend across
``handlers.py``, ``task_dispatcher.py``, ``cron_scheduler.py``,
``watchdog.py``, ``agent_worker.py``, etc. Before P3 they all ran as
unauthenticated requests; once cookie-session tenancy auth landed they
all started 401-ing silently (the cron poller is the loudest because
it fires every minute — see audit findings CLI-010 / SEC-008 / the
"Cron /due returned 401" log storm).

This helper centralises the fix:

* Reads ``config.security_token`` (the ``cbcl_co_...`` Company Token).
* Attaches ``Authorization: Bearer <token>`` to every request.
* Lets call sites stay short — same shape as raw ``httpx.AsyncClient``.

The token is read at construction so a single client instance keeps its
auth header even if the global config is mutated mid-request. Callers
either share a long-lived client (preferred — connection pool reuse)
or use the convenience module-level functions which open a one-shot
client per call.

Routes that don't accept Bearer (``/tool-call``, MCP-OAuth callback)
should keep their existing direct ``httpx.AsyncClient`` usage — passing
a Bearer header to an unauth route is harmless but the layering stays
clearer when the un-authed sites are explicit.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Modest default; callers can override per-request.
_DEFAULT_TIMEOUT_SECONDS: float = 30.0


class BackendClient:
    """A pre-authenticated ``httpx.AsyncClient`` wrapper.

    Usage:

    ```python
    async with BackendClient(platform_url, security_token) as client:
        resp = await client.get(f"/api/offices/{oid}/agents")
    ```

    ``platform_url`` becomes the client's base_url, so request paths can
    be relative.
    """

    def __init__(
        self,
        platform_url: str,
        security_token: str | None,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not platform_url:
            raise ValueError("platform_url is required")
        headers: dict[str, str] = {}
        if security_token:
            headers["Authorization"] = f"Bearer {security_token}"
        else:
            # Soft warning at construction so a misconfigured daemon
            # surfaces in logs once, not on every request.
            logger.warning(
                "BackendClient created without security_token; "
                "office-scoped endpoints will return 401",
            )
        self._client = httpx.AsyncClient(
            base_url=platform_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )

    async def __aenter__(self) -> "BackendClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self._client.get(url, **kw)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        return await self._client.post(url, **kw)

    async def put(self, url: str, **kw: Any) -> httpx.Response:
        return await self._client.put(url, **kw)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self._client.delete(url, **kw)


def _blocked_triage_cooldown_seconds() -> int:
    """Read the MA-triage cooldown window from the environment.

    Default 3600s (1 hour). Bounded to [60, 86400] so a typo can't
    deadlock the triage path forever or open the spam gate.
    """
    import os
    try:
        value = int(os.environ.get("CUBICLE_BLOCKED_TRIAGE_COOLDOWN_SECONDS", "3600"))
    except (TypeError, ValueError):
        value = 3600
    return max(60, min(value, 86400))


async def task_should_skip_ma_routing(
    platform_url: str,
    office_id: str,
    task_id: str,
    security_token: str | None,
) -> bool:
    """Combined "should the dispatcher skip routing this blocked task
    to the Manager Assistant?" check. Returns True when EITHER:

    * A pending action_request already exists for the task (fast
      single-row count via the action_requests GET endpoint), OR
    * The MA already triaged the task within the cooldown window
      (``last_blocked_triage_at`` set within
      ``CUBICLE_BLOCKED_TRIAGE_COOLDOWN_SECONDS``).

    The two checks overlap heavily — when MA proposed an action it
    also stamped the cooldown — but together they cover the corner
    cases (MA posted an `answer` and left without escalating, MA's
    process crashed mid-triage, action_request was already decided
    but the task is still blocked while waiting on the next step).

    Fail-OPEN on transport errors so a transient blip doesn't lock
    triage.
    """
    if await task_has_pending_action_request(
        platform_url=platform_url,
        office_id=office_id,
        task_id=task_id,
        security_token=security_token,
    ):
        return True
    cooldown = _blocked_triage_cooldown_seconds()
    return await task_blocked_triage_within_cooldown(
        platform_url=platform_url,
        office_id=office_id,
        task_id=task_id,
        security_token=security_token,
        cooldown_seconds=cooldown,
    )


def auth_headers(security_token: str | None) -> dict[str, str]:
    """Return the Authorization header dict for a one-shot call.

    Use when a caller already has its own ``httpx.AsyncClient`` open and
    just needs the Bearer header for one request. Returns an empty dict
    when no token is set so callers can splat unconditionally:
    ``await client.get(url, headers=auth_headers(token))``.
    """
    if not security_token:
        return {}
    return {"Authorization": f"Bearer {security_token}"}


async def task_has_pending_action_request(
    platform_url: str,
    office_id: str,
    task_id: str,
    security_token: str | None,
) -> bool:
    """Return True iff the task already has a PENDING action-request.

    Used by the dispatch path (both ``handlers.py:_on_agent_event`` and
    ``_handlers/_tasks.py:route_task_moved``) to detect "task is parked
    waiting on a human" and skip re-queuing to the Manager Assistant.
    Without this guard the MA picks up the same blocked task on every
    dispatch loop and proposes another escalation, flooding the inbox.

    Fail-OPEN on transport / 5xx errors — i.e. return ``False`` so the
    caller routes to MA as usual. Rationale: a transient backend blip
    must not deadlock the triage path. The dedup at create-time
    (``service.create_action_request``) still prevents duplicate inbox
    rows even if this check spuriously returns False.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{platform_url}/api/offices/{office_id}/action-requests",
                params={
                    "status": "pending",
                    "source_task_id": task_id,
                    "limit": 1,
                },
                headers=auth_headers(security_token),
            )
            if resp.status_code != 200:
                return False
            body = resp.json()
            return (body.get("total") or 0) > 0
    except Exception:
        # Network / parsing failure → fail-open. See docstring above.
        return False


async def task_blocked_triage_within_cooldown(
    platform_url: str,
    office_id: str,
    task_id: str,
    security_token: str | None,
    cooldown_seconds: int,
) -> bool:
    """Return True iff the task was triaged by the MA within the
    cooldown window — meaning the dispatcher must NOT re-route it.

    This is the more general cooldown lock backing the
    "no auto-execution from blocked" policy: regardless of how the
    MA triaged (posted a comment, created a helper task, proposed an
    action_request, or just left a synthesis note), the timestamp
    ``last_blocked_triage_at`` is stamped server-side and the lock
    holds for ``CUBICLE_BLOCKED_TRIAGE_COOLDOWN_SECONDS`` (default
    3600s).

    The flag is cleared automatically when the task transitions out
    of blocked, so a fresh block always starts a fresh triage cycle.

    Fail-OPEN on transport errors — same rationale as
    ``task_has_pending_action_request``.
    """
    import httpx
    from datetime import datetime, timezone

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{platform_url}/api/offices/{office_id}/tasks/{task_id}",
                headers=auth_headers(security_token),
            )
            if resp.status_code != 200:
                return False
            body = resp.json()
            raw = body.get("last_blocked_triage_at")
            if not raw:
                return False
            try:
                # API returns ISO 8601 with Z or +00:00; both fine.
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return False
            elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
            return elapsed < cooldown_seconds
    except Exception:
        return False
