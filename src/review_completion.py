"""Review completion requires a persisted verdict, never a clean process exit."""

from __future__ import annotations

import uuid

import httpx

from src.backend_client import auth_headers
from src.review_routing import default_reviewer


REVIEW_RECOVERY_LIMIT = 2


async def _publish_review_hold(
    task_id: str, payload: dict, *, platform_url: str, office_id: str, security_token: str,
) -> dict | None:
    async with httpx.AsyncClient(timeout=10.0, headers=auth_headers(security_token)) as client:
        response = await client.post(
            f"{platform_url.rstrip('/')}/api/offices/{office_id}/tasks/{task_id}/review-hold",
            json=payload,
        )
    result = response.json()
    if response.status_code in (400, 409) and isinstance(result, dict) and result.get("code") == "stale_execution":
        return None
    response.raise_for_status()
    if (
        not isinstance(result, dict) or result.get("status") != "pending"
        or not isinstance(result.get("action_request_id"), str) or not result["action_request_id"]
        or result.get("review_retry_epoch", 0) != payload["review_retry_epoch"]
    ):
        raise RuntimeError("Review hold receipt was not accepted by the platform")
    return result


async def upgrade_legacy_review_hold(
    task_id: str, task: dict, *, runtime_state, platform_url: str, office_id: str, security_token: str,
) -> bool:
    cycle = task.get("execution_cycle", 0)
    epoch = task.get("review_retry_epoch", 0)
    reviewer = task.get("reviewer") or default_reviewer(task)
    state = runtime_state.review_state(task_id, cycle, reviewer, epoch=epoch)
    if not state["request_id"] or state["hold_kind"] == "review_hold":
        return True
    attempt_id = runtime_state.latest_review_attempt(task_id, cycle, reviewer, epoch=epoch)
    try:
        legacy_id = str(uuid.UUID(state["request_id"]))
    except (ValueError, TypeError, AttributeError):
        return False
    async def mark_reconciliation() -> None:
        if state["hold_kind"] == "legacy_reconciliation_reported":
            return
        async with httpx.AsyncClient(timeout=10.0, headers=auth_headers(security_token)) as client:
            response = await client.post(
                f"{platform_url.rstrip('/')}/api/offices/{office_id}/tasks/{task_id}/review-hold/reconciliation",
                json={"legacy_request_id": legacy_id, "execution_cycle": cycle, "review_retry_epoch": epoch, "reviewer": reviewer},
            )
        response.raise_for_status()
        receipt = response.json()
        if not isinstance(receipt, dict) or receipt.get("status") != "operator_reconciliation_required" or receipt.get("action_request_id") != legacy_id:
            raise RuntimeError("Legacy review reconciliation notice was not confirmed")
        runtime_state.hold_review(task_id, cycle, reviewer, legacy_id, epoch=epoch, hold_kind="legacy_reconciliation_reported")

    if not attempt_id or not isinstance(task.get("execution_generation"), int):
        await mark_reconciliation()
        return False
    result = await _publish_review_hold(
        task_id, {
            "attempt_id": attempt_id, "execution_cycle": cycle,
            "execution_generation": task["execution_generation"], "review_retry_epoch": epoch,
            "reviewer": reviewer, "reason": "infrastructure_exhausted" if state["failures"] > REVIEW_RECOVERY_LIMIT else "missing_verdict",
            "legacy_request_id": legacy_id,
        }, platform_url=platform_url, office_id=office_id, security_token=security_token,
    )
    if result is None:
        await mark_reconciliation()
        return False
    runtime_state.hold_review(task_id, cycle, reviewer, result["action_request_id"], epoch=epoch, hold_kind="review_hold")
    return True


async def reconcile_review_completion(
    task: dict, event: dict, agent_name: str, *, runtime_state,
    platform_url: str, office_id: str, security_token: str,
) -> str:
    if task.get("status") != "review":
        return "already_transitioned"
    task_id = str(event.get("task_id") or task.get("id") or "")
    cycle = task.get("execution_cycle", 0)
    epoch = task.get("review_retry_epoch", 0)
    reviewer = task.get("reviewer") or default_reviewer(task)
    caller = event.get("_caller") or {}
    if reviewer != agent_name or (
        caller.get("execution_cycle") != cycle
        or caller.get("execution_generation") != task.get("execution_generation")
        or caller.get("review_retry_epoch", 0) != epoch
    ):
        return "superseded"
    state = runtime_state.review_state(task_id, cycle, reviewer, epoch=epoch)
    if state["request_id"]:
        return "held"
    try:
        attempt_id = str(uuid.UUID(caller.get("attempt_id")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("Review completion identity requires reconciliation") from exc
    details = event.get("details") or {}
    if (event.get("error_class") or details.get("error_class")) == "usage_limit_exceeded":
        from src.orchestrator._model_defaults import FALLBACK_WORKER_MODEL
        from src.quota_recovery import defer_quota_task

        runtime_state.pause_for_quota(
            details.get("usage_limit_error") or event.get("comment") or "Claude usage limit reached",
            details.get("quota_model") or FALLBACK_WORKER_MODEL,
        )
        defer_quota_task(runtime_state, {**task, "id": task_id}, event)
        return "quota_paused"
    attempts = runtime_state.record_review_attempt(task_id, cycle, reviewer, attempt_id, epoch=epoch)
    infra_error = event.get("error_class") or (event.get("details") or {}).get("error_class")
    if infra_error and attempts <= REVIEW_RECOVERY_LIMIT:
        return "retry"
    result = await _publish_review_hold(
        task_id, {
            "attempt_id": attempt_id, "execution_cycle": cycle,
            "execution_generation": caller["execution_generation"], "review_retry_epoch": epoch,
            "reviewer": reviewer, "reason": "infrastructure_exhausted" if infra_error else "missing_verdict",
        }, platform_url=platform_url, office_id=office_id, security_token=security_token,
    )
    if result is None:
        return "superseded"
    runtime_state.hold_review(task_id, cycle, reviewer, result["action_request_id"], epoch=epoch, hold_kind="review_hold")
    return "held"
