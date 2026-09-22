"""Claim a backend-fenced attempt only after prior local execution cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import uuid

import httpx

from src.backend_client import auth_headers


class ExecutionClaimError(RuntimeError):
    pass


class ExecutionClaimDeferred(ExecutionClaimError):
    """Authoritative capacity/resource wait; no worker crashed."""


def execution_owner_headers(secret: str | Callable[[], str]) -> dict[str, str]:
    """Resolve current socket ownership for each POST; never journal it."""
    value = secret() if callable(secret) else secret
    return {"X-Office-Secret": value} if isinstance(value, str) and value else {}


async def validate_worker_execution(
    task_id: str,
    caller: dict,
    *,
    platform_url: str,
    office_id: str,
    security_token: str,
    office_tool_secret: str | Callable[[], str] = "",
) -> None:
    generation = caller.get("execution_generation")
    cycle = caller.get("execution_cycle")
    epoch = caller.get("review_retry_epoch", 0)
    if (
        caller.get("role") != "worker"
        or caller.get("task_id") != task_id
        or caller.get("task_mode") not in {"execute", "review", "triage"}
        or not isinstance(caller.get("expected_assigned_agent"), str)
        or not caller.get("attempt_id")
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or isinstance(cycle, bool)
        or not isinstance(cycle, int)
        or cycle < 0
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 0
    ):
        raise ExecutionClaimError(
            "Task script launch requires a complete execution identity"
        )
    request = {
        "attempt_id": caller["attempt_id"],
        "execution_cycle": cycle,
        "expected_execution_generation": generation - 1,
        "expected_assigned_agent": caller["expected_assigned_agent"],
        "execution_mode": caller["task_mode"],
        "expected_execution_owner": caller["agent_name"],
        "expected_review_retry_epoch": epoch,
        **(
            {
                "agent_instance_id": caller["agent_instance_id"],
                "profile_id": caller["profile_id"],
                "runtime_release_required": True,
            }
            if caller.get("agent_instance_id")
            else {}
        ),
    }
    async with httpx.AsyncClient(
        timeout=10.0, headers=auth_headers(security_token)
    ) as client:
        response = await client.post(
            f"{platform_url.rstrip('/')}/api/offices/{office_id}/tasks/{task_id}/execution-attempts/claim",
            json=request,
            headers=execution_owner_headers(office_tool_secret),
        )
        response.raise_for_status()
        receipt = response.json()
    if (
        not isinstance(receipt, dict)
        or receipt.get("runtime_released_at") is not None
        or any(
            receipt.get(key) != caller.get(key)
            for key in (
                "attempt_id",
                "execution_cycle",
                "execution_generation",
                "agent_name",
                "agent_instance_id",
                "profile_id",
            )
        )
        or receipt.get("review_retry_epoch", 0) != epoch
    ):
        raise ExecutionClaimError(
            "Task script launch refused: execution receipt is stale"
        )


async def claim_worker_execution(
    agent_name: str,
    task_data: dict,
    attempt_id: str,
    *,
    platform_url: str,
    office_id: str,
    security_token: str,
    runtime_state=None,
    office_tool_secret: str | Callable[[], str] = "",
) -> dict:
    task_id = str(task_data.get("task_id") or task_data.get("id") or "")
    mode = (
        "review"
        if task_data.get("status") == "review"
        else "triage" if task_data.get("status") == "blocked" else "execute"
    )
    expected_status = {
        "execute": "in_progress",
        "review": "review",
        "triage": "blocked",
    }[mode]
    task_url = f"{platform_url.rstrip('/')}/api/offices/{office_id}/tasks/{task_id}"
    async with httpx.AsyncClient(
        timeout=10.0, headers=auth_headers(security_token)
    ) as client:
        response = await client.get(task_url)
        response.raise_for_status()
        task = response.json()
        if (
            not isinstance(task, dict)
            or task.get("status") != expected_status
            or task.get("execution_blocked")
        ):
            raise ExecutionClaimError(
                "Task execution admission changed; reconcile before retrying"
            )
        cycle = task.get("execution_cycle")
        generation = task.get("execution_generation")
        epoch = task.get("review_retry_epoch", 0)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (cycle, generation, epoch)
        ):
            raise ExecutionClaimError(
                "Backend execution fencing is unavailable; upgrade the platform before dispatch"
            )
        request = {
            "attempt_id": attempt_id,
            "execution_cycle": cycle,
            "expected_execution_generation": generation,
            "expected_assigned_agent": task.get("assigned_agent") or "",
            "execution_mode": mode,
            "expected_execution_owner": agent_name,
            "expected_review_retry_epoch": epoch,
            "runtime_release_required": True,
            **(
                {"expected_execution_resources": task["effective_execution_resources"]}
                if "effective_execution_resources" in task
                else {}
            ),
        }
        if runtime_state is not None:
            runtime_state.begin_worker_claim(agent_name, task_id, request)
        for retry_index in range(3):
            try:
                response = await client.post(
                    f"{task_url}/execution-attempts/claim",
                    json=request,
                    headers=execution_owner_headers(office_tool_secret),
                )
                if response.status_code >= 500 and retry_index < 2:
                    await asyncio.sleep(0.25 * (retry_index + 1))
                    continue
                if response.status_code in (400, 409):
                    try:
                        refusal = response.json()
                    except ValueError:
                        refusal = None
                    if isinstance(refusal, dict) and refusal.get("code") in {
                        "execution_capacity_busy",
                        "execution_resource_busy",
                    }:
                        raise ExecutionClaimDeferred(refusal["code"])
                response.raise_for_status()
                claim = response.json()
                if (
                    not isinstance(claim, dict)
                    or claim.get("attempt_id") != attempt_id
                    or claim.get("agent_name") != agent_name
                    or claim.get("execution_cycle") != cycle
                    or isinstance(claim.get("execution_generation"), bool)
                    or not isinstance(claim.get("execution_generation"), int)
                    or claim["execution_generation"] != generation + 1
                    or claim.get("review_retry_epoch", 0) != epoch
                ):
                    raise ExecutionClaimError(
                        "Backend returned an inconsistent execution claim"
                    )
                for field in ("agent_instance_id", "profile_id"):
                    try:
                        uuid.UUID(str(claim[field]))
                    except (KeyError, ValueError, TypeError) as exc:
                        raise ExecutionClaimError(
                            "Backend dynamic execution identity is unavailable; upgrade the platform"
                        ) from exc
                if runtime_state is not None:
                    runtime_state.record_worker_claim(attempt_id, claim)
                return {
                    **claim,
                    "expected_assigned_agent": request["expected_assigned_agent"],
                    "review_retry_epoch": epoch,
                }
            except httpx.TransportError:
                if retry_index == 2:
                    raise
                await asyncio.sleep(0.25 * (retry_index + 1))
    raise ExecutionClaimError("Execution claim was not confirmed")


async def recover_worker_claim(
    record: dict, *, platform_url: str, office_id: str, security_token: str,
    office_tool_secret: str | Callable[[], str] = "",
) -> dict | None:
    """Resolve an uncertain claim using its original idempotency identity."""
    url = f"{platform_url.rstrip('/')}/api/offices/{office_id}/tasks/{record['task_id']}/execution-attempts"
    async with httpx.AsyncClient(
        timeout=10.0, headers=auth_headers(security_token)
    ) as client:
        response = await client.get(f"{url}/{record['attempt_id']}")
        if response.status_code == 404:
            retry = await client.post(
                f"{url}/claim", json=record["request"],
                headers=execution_owner_headers(office_tool_secret),
            )
            if retry.status_code in (400, 404, 409):
                # The claim transaction has settled; check again because its
                # current phase can reject a retry of an already-committed run.
                response = await client.get(f"{url}/{record['attempt_id']}")
                if response.status_code == 404:
                    return None
            else:
                response = retry
        response.raise_for_status()
        receipt = response.json()
        if (
            receipt.get("attempt_id") != record["attempt_id"]
            or receipt.get("agent_name") != record["agent_name"]
        ):
            raise ExecutionClaimError(
                "Recovered claim identity does not match its durable intent"
            )
        return receipt


async def release_worker_execution(
    task_id: str,
    attempt_id: str,
    agent_instance_id: str,
    *,
    session_id: str | None = None,
    platform_url: str,
    office_id: str,
    security_token: str,
) -> None:
    """Idempotently acknowledge physical quiescence, including stale attempts."""
    async with httpx.AsyncClient(
        timeout=10.0, headers=auth_headers(security_token)
    ) as client:
        response = await client.post(
            f"{platform_url.rstrip('/')}/api/offices/{office_id}/tasks/{task_id}/execution-attempts/{attempt_id}/release",
            json={"agent_instance_id": agent_instance_id, "session_id": session_id},
        )
        response.raise_for_status()
