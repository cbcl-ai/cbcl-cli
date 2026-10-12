"""Authenticated helpers for the daemon's HTTP calls into the platform backend.

Every call into an office-scoped backend route needs the Company Token
(``cbcl_co_...``) as ``Authorization: Bearer <token>``; before P3 those
calls ran unauthenticated and started 401-ing silently once cookie-session
tenancy auth landed (audit findings CLI-010 / SEC-008, the "Cron /due
returned 401" log storm). ``auth_headers`` builds that header, and the
module-level functions below wrap the lookups the dispatcher, the review
router and the triage router share.

Routes that don't accept Bearer (``/tool-call`` over the explicit legacy
path, MCP-OAuth callbacks) keep their own direct ``httpx.AsyncClient``
usage, so the un-authed sites stay explicit.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Task-detail ``blocked_waiting_on`` values (the backend's shared wait
# classifier, ``classify_blocked_wait``) that mean a person already decided
# about this Blocked task: they rejected its bounce-cap card, or rejected
# another blocker card and no request was filed since. The platform does not
# ask them again, so no triage is launched (a later request, or the person's
# approval or Resume task, ends the hold on the backend side).
_PERSON_HOLD_WAITS = frozenset({"person_kept_blocked", "person_declined"})


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
    to the Manager Assistant?" check. Returns True when ANY of:

    * A pending action_request other than a pure trusted dispatch-health
      diagnostic exists for the task, OR
    * The MA already triaged the task within the cooldown window
      (``last_blocked_triage_at`` set within
      ``CUBICLE_BLOCKED_TRIAGE_COOLDOWN_SECONDS``), OR
    * A person's own decision holds the task (task detail
      ``blocked_waiting_on`` is ``person_kept_blocked`` or ``person_declined``,
      or ``bounce_cap_decision == "rejected"`` from a backend that predates
      that field): they rejected its bounce-cap card, or rejected another
      blocker card and nothing asked again since. A triage could only post a
      synthesis comment or ask them the same thing again, so none is
      launched. Read from the same task GET as the cooldown; older backends
      omit the fields, which leaves routing as before.

    The first two checks overlap heavily — when MA proposed an action it
    also stamped the cooldown — but together they cover the corner
    cases (MA posted an `answer` and left without escalating, MA's
    process crashed mid-triage, action_request was already decided
    but the task is still blocked while waiting on the next step).

    Fail-OPEN on transport errors so a transient blip doesn't lock
    triage (``task_has_pending_triage_decision`` returns ``None`` on a
    failed/incomplete lookup, which falls through to the cooldown check).
    The generic approval helper remains separate and counts every request;
    its approval callers retain their fail-closed posture.
    """
    if await task_has_pending_triage_decision(
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


async def post_system_chat_notice(
    platform_url: str,
    office_id: str,
    context_key: str,
    content: str,
    security_token: str | None,
    action_payload: dict[str, Any] | None = None,
) -> bool:
    """Persist a ``role='system'`` chat bubble in a Manager context via
    ``POST /api/offices/{oid}/messages`` (HYBRID route — the Company Token
    bearer is accepted).

    Used by the planner heartbeat's LONG-VERIFY progress notices: a durable,
    chat-visible system row WITHOUT running a Manager turn (a poke would cost
    a full turn and paraphrase the copy) and WITHOUT any new backend surface.
    Two honesty caveats, deliberate and documented at the call site:

    * the REST create path persists but does NOT live-broadcast a
      ``chat_message`` frame — an already-open tab renders the row on its
      next ``/messages/since`` replay (context switch / reconnect / mount);
      the heartbeat's ``manager_state`` pill carries the same copy LIVE;
    * ``action_payload.kind`` must be one of the frontend's whitelisted
      inline system-row kinds (``isInlineSystemRow``) or the transcript
      filters the row out — callers reuse ``planner_consulted``.

    Returns ``True`` only on a 201. Best-effort: transport errors are logged
    and swallowed (a missed notice must never break the caller's loop).
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{platform_url}/api/offices/{office_id}/messages",
                json={
                    "context_key": context_key,
                    "role": "system",
                    "content": content,
                    "action_payload": action_payload or {},
                },
                headers=auth_headers(security_token),
            )
            if resp.status_code != 201:
                logger.warning(
                    "post_system_chat_notice: backend returned %s for "
                    "office %s ctx %s (non-fatal)",
                    resp.status_code, office_id, context_key,
                )
                return False
            return True
    except Exception:
        logger.warning(
            "post_system_chat_notice failed for office %s ctx %s "
            "(non-fatal)",
            office_id, context_key, exc_info=True,
        )
        return False


# The finding's own keys plus the stamps the backend action-request ager writes
# into ``payload`` (``ager_re_poked_at`` etc.). The stamps record what the ager
# did, not what the finding says, so a stamped finding is still pure. Mirrors
# backend ``_DISPATCH_DIAGNOSTIC_PAYLOAD_KEYS | AGER_PAYLOAD_STAMPS``.
_DISPATCH_DIAGNOSTIC_PAYLOAD_KEYS = frozenset({
    "blocker_summary", "suggested_unblock", "sweeper_signals",
    "ager_re_poked_at", "ager_user_escalated_at", "ager_reconnect_repoked_at",
})


def _is_dispatch_diagnostic(request: dict) -> bool:
    """Recognize only pure, trusted dispatch-health alerts.

    Keep this predicate aligned with backend blocker_requests.is_dispatch_diagnostic.
    ``requires_user`` controls Inbox routing, not whether a diagnostic can stop
    the very dispatch/recovery it reports. Mixed or unrecognized content remains
    a decision; no request is closed or otherwise mutated by this classification.
    """
    payload = request.get("payload")
    if (
        request.get("request_type") != "escalate_blocker"
        or request.get("requesting_agent") != "system-sweeper"
        or request.get("category") not in ("workstream", "infrastructure")
        or not isinstance(payload, dict)
        or not set(payload) <= _DISPATCH_DIAGNOSTIC_PAYLOAD_KEYS
    ):
        return False
    signals = payload.get("sweeper_signals")
    return (
        isinstance(signals, dict)
        and bool(signals)
        and set(signals) <= {"stuck_ready", "stuck_review", "workstream_stall"}
        and all(isinstance(value, dict) for value in signals.values())
    )


async def task_has_pending_triage_decision(
    platform_url: str,
    office_id: str,
    task_id: str,
    security_token: str | None,
) -> bool | None:
    """Pending triage decisions, excluding only pure dispatch diagnostics.

    Read one bounded snapshot, not a count or moving offset pages: a diagnostic
    at the front must not conceal a real proposal later in the response. Unknown
    or incomplete reads return None; MA routing retains its documented fail-open
    fallback to the independent cooldown check. This helper never approves work.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{platform_url}/api/offices/{office_id}/action-requests",
                params={"status": "pending", "source_task_id": task_id, "limit": 500},
                headers=auth_headers(security_token),
            )
        if response.status_code != 200:
            return None
        body = response.json()
        if not isinstance(body, dict):
            return None
        items, total = body.get("items"), body.get("total")
        if not isinstance(items, list) or type(total) is not int or total < 0:
            return None
        request_ids: set[str] = set()
        for item in items:
            if (
                not isinstance(item, dict) or item.get("status") != "pending"
                or item.get("office_id") != office_id
                or item.get("source_task_id") != task_id
                or not isinstance(item.get("id"), str) or not item["id"]
                or item["id"] in request_ids
            ):
                return None
            request_ids.add(item["id"])
            if not _is_dispatch_diagnostic(item):
                return True
        # Count/SELECT can race, and a full page may hide concurrent inserts.
        return False if len(items) == total and len(items) < 500 else None
    except Exception:
        return None


async def action_request_is_pending(
    platform_url: str,
    office_id: str,
    request_id: str,
    *,
    request_type: str,
    workstream_id: str | None,
    security_token: str | None,
) -> bool | None:
    """Whether ``request_id`` is still pending: True, False, or None if unknown.

    Reads one bounded page of the office's pending requests of that type,
    narrowed to the request's workstream when known (a request never changes
    workstream; its source task can be deleted). False only when that page is
    complete and does not list the request. A failed request, a body that is
    not JSON, an unexpected shape or a partial page is None, and callers then
    act as before the check.
    """
    params: dict[str, Any] = {
        "status": "pending",
        "request_type": request_type,
        "limit": 500,
    }
    if workstream_id:
        params["workstream_id"] = workstream_id
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{platform_url}/api/offices/{office_id}/action-requests",
                params=params,
                headers=auth_headers(security_token),
            )
        if response.status_code != 200:
            return None
        body = response.json()
        if not isinstance(body, dict):
            return None
        items, total = body.get("items"), body.get("total")
        if not isinstance(items, list) or type(total) is not int or total < 0:
            return None
        for item in items:
            if not isinstance(item, dict):
                return None
            if item.get("id") == request_id:
                return item.get("status") == "pending" or None
        # Count/SELECT can race, and a full page may hide concurrent inserts.
        return False if len(items) == total and len(items) < 500 else None
    except (httpx.HTTPError, ValueError):
        # A transport failure or a body that is not JSON. Anything else is a
        # bug: the caller logs it and runs the turn.
        return None


def _is_pending_spec_proposal(request: dict) -> bool:
    """A canonical spec proposal requests a decision, not a review hold.

    Mirror ProposeSpecUpdatePayload's fields/bounds without importing the
    private backend into the standalone communicator. Unknown or mixed payloads
    stay blocking. This never applies or approves the proposed requirements.
    """
    payload = request.get("payload")
    author = request.get("requesting_agent")
    if (
        request.get("request_type") != "propose_spec_update"
        or request.get("status") != "pending"
        or request.get("category") != "user_input"
        or request.get("requires_user") is not True
        or not isinstance(author, str) or not author.strip()
        or not isinstance(payload, dict)
        or not set(payload) <= {"proposed_text", "rationale", "spec_id", "target"}
    ):
        return False
    for field, limit in (("proposed_text", 8000), ("rationale", 4000)):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            return False
    for field, limit in (("spec_id", 64), ("target", 200)):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            return False
    return True


def _is_advisory_parent_followup(request: dict, task: dict) -> bool:
    """Mirror backend parent_followups, with a freshly read contract identity."""
    import re

    payload = request.get("payload")
    if (
        request.get("request_type") != "create_subtask" or request.get("status") != "pending"
        or request.get("category") != "workstream" or request.get("requires_user") is not False
        or not all(task.get(field) and request.get(field) == task[field] for field in ("office_id", "workstream_id"))
        or not task.get("id") or request.get("source_task_id") != task["id"]
        or not isinstance(payload, dict) or not set(payload) <= {
            "title", "brief_hints", "execution_resources", "parent_task_id", "parent_dependency",
            "advisory_parent", "creation_contract_version",
        }
        or payload.get("parent_dependency") != "advisory" or payload.get("parent_task_id") != task["id"]
        or type(payload.get("creation_contract_version")) is not int or payload["creation_contract_version"] != 1
    ):
        return False
    marker = payload.get("advisory_parent")
    resources = payload.get("execution_resources")
    if resources is not None and (
        not isinstance(resources, list) or len(resources) > 16
        or any(not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", key) is None for key in resources)
        or len(set(resources)) != len(resources)
    ):
        return False
    digest = task.get("task_contract_digest")
    if not isinstance(marker, dict) or set(marker) != {"version", "task_id", "execution_cycle", "contract_digest"}:
        return False
    return (
        type(marker["version"]) is int and marker["version"] == 1 and marker["task_id"] == task["id"]
        and type(marker["execution_cycle"]) is int and marker["execution_cycle"] >= 0
        and type(task.get("execution_cycle")) is int and marker["execution_cycle"] == task["execution_cycle"]
        and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) is not None
        and marker["contract_digest"] == digest
        and isinstance(payload.get("title"), str) and bool(payload["title"].strip()) and len(payload["title"]) <= 500
        and (payload.get("brief_hints") is None or (isinstance(payload["brief_hints"], dict) and set(payload["brief_hints"]) <= {
            "goal", "context", "inputs", "output_format", "acceptance_criteria",
            "allowed_tools", "required_skills", "reference_doc_ids", "risks_and_edge_cases",
            "verification_steps", "verification_plan",
        }))
        and isinstance(request.get("requesting_agent"), str) and bool(request["requesting_agent"].strip())
    )


def _review_request_blocks(request: dict, task: dict) -> bool:
    """Match review holds by execution identity; retain other real decisions.

    Mirrors backend review_retry.hold_matches / is_legacy_review_hold. The
    claim endpoint still decides admission under lock after this advisory read.
    """
    kind = request.get("request_type")
    payload = request.get("payload")
    if not isinstance(payload, dict):
        return True
    marker = payload.get("review_recovery")
    legacy_hold = (
        kind == "escalate_blocker" and isinstance(marker, dict)
        and marker.get("state") == "operator_reconciliation_required"
        and marker.get("evidence") == "communicator_legacy_hold"
        and marker.get("task_id") == request.get("source_task_id")
        and marker.get("reviewer") == request.get("requesting_agent")
    )
    if kind == "review_hold" or legacy_hold:
        identity = marker if legacy_hold else payload
        fields = ("execution_cycle", "execution_generation", "review_retry_epoch")
        # Missing/malformed identities are not evidence that a hold is obsolete.
        if (
            any(field not in data for data in (request, task) for field in ("office_id", "workstream_id"))
            or not task.get("id") or not task.get("reviewer") or not identity.get("reviewer")
            or any(
                type(data.get(field)) is not int or data[field] < 0
                for data in (task, identity) for field in fields
            )
        ):
            return True
        return (
            request.get("office_id") == task.get("office_id")
            and request.get("source_task_id") == task.get("id")
            and request.get("workstream_id") == task.get("workstream_id")
            and identity["reviewer"] == task["reviewer"]
            and all(identity[field] == task[field] for field in fields)
        )
    if "review_recovery" in payload or payload.get("rework_cap"):
        # Unattested legacy markers and human escalations remain decisions.
        return True
    if _is_pending_spec_proposal(request):
        # Review the delivered work against the approved brief/spec. A pending
        # proposal does not revise that contract or satisfy an unmet criterion.
        return False
    if _is_advisory_parent_followup(request, task):
        return False
    if kind in ("informational", "board_overview"):
        return False
    return not _is_dispatch_diagnostic(request)


async def task_has_pending_review_decision(
    platform_url: str, office_id: str, task_id: str,
    security_token: str | None, *, task: dict,
) -> bool | None:
    """True for a review-blocking decision, False for known nonblocking rows.

    Pure diagnostics and canonical spec proposals do not hold independent
    review. The proposals remain pending and never change approval criteria.

    Fetch one bounded snapshot of up to 500 rows. Offset pagination can skip
    a real decision when earlier diagnostics resolve between pages. Incomplete
    or failed reads return unknown and defer dispatch until reconciliation.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{platform_url}/api/offices/{office_id}/action-requests",
                params={"status": "pending", "source_task_id": task_id, "limit": 500},
                headers=auth_headers(security_token),
            )
        if response.status_code != 200:
            return None
        body = response.json()
        if not isinstance(body, dict):
            return None
        items, total = body.get("items"), body.get("total")
        if not isinstance(items, list) or type(total) is not int or total < 0:
            return None
        for item in items:
            if (
                not isinstance(item, dict) or item.get("status") != "pending"
                or item.get("office_id") != office_id
                or item.get("source_task_id") != task_id
            ):
                return None
            if _review_request_blocks(item, task):
                return True
        # Count and SELECT are separate queries. A full page may have been
        # truncated after concurrent inserts, even when the count says 500.
        return False if len(items) == total and len(items) < 500 else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None


async def task_blocked_triage_within_cooldown(
    platform_url: str,
    office_id: str,
    task_id: str,
    security_token: str | None,
    cooldown_seconds: int,
) -> bool:
    """Return True iff the task was triaged by the MA within the
    cooldown window, or a person's own decision holds it
    (``_PERSON_HOLD_WAITS``, ``bounce_cap_decision == "rejected"``) —
    meaning the dispatcher must NOT re-route it.

    This is the more general cooldown lock backing the
    "no auto-execution from blocked" policy: regardless of how the
    MA triaged (posted a comment, created a helper task, proposed an
    action_request, or just left a synthesis note), the timestamp
    ``last_blocked_triage_at`` is stamped server-side and the lock
    holds for ``CUBICLE_BLOCKED_TRIAGE_COOLDOWN_SECONDS`` (default
    3600s).

    The flag is cleared automatically when the task transitions out
    of blocked, so a fresh block always starts a fresh triage cycle.

    Fail-OPEN on transport errors — same posture as the separate triage
    decision lookup, without weakening generic approval guards.
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
            if (
                body.get("blocked_waiting_on") in _PERSON_HOLD_WAITS
                or body.get("bounce_cap_decision") == "rejected"
            ):
                return True
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
