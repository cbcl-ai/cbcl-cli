"""Validate a host-accepted wait separately from a launched script receipt."""

import uuid


def capacity_wait_result(body: object, *, task_id: str, phase: str) -> dict:
    error = {
        "error": True,
        "message": "Capacity wait was not confirmed for this task/phase. Inspect the existing operation before retrying; no launch is inferred.",
    }
    if not isinstance(body, dict) or body.get("accepted") is not True:
        return error
    wait = body.get("wait")
    if (
        body.get("status") != "waiting_for_capacity"
        or not isinstance(wait, dict)
        or not task_id
        or wait.get("task_id") != task_id
        or wait.get("phase") != phase
        or phase not in {"execute", "review", "triage"}
        or wait.get("state") != "waiting"
        or type(wait.get("execution_cycle")) is not int
        or wait["execution_cycle"] < 0
        or type(wait.get("retry_after_seconds")) is not int
        or not 1 <= wait["retry_after_seconds"] <= 300
    ):
        return error
    try:
        uuid.UUID(wait["wait_id"])
        uuid.UUID(wait["operation_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return error
    return {
        "accepted_wait": True,
        "status": "waiting_for_capacity",
        "wait": {
            key: wait[key]
            for key in (
                "wait_id",
                "operation_id",
                "task_id",
                "execution_cycle",
                "phase",
                "state",
                "retry_after_seconds",
            )
        },
        "message": "Capacity wait recorded. STOP this session; the daemon will resume the same task phase when eligible. No script was launched by this call. Do not poll, change the operation key or submit a verdict.",
    }
