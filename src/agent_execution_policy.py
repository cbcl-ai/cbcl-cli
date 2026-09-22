"""Validated daemon policy; malformed sync cannot silently enable concurrency."""

import re

DEFAULT_EXECUTION_POLICY = {
    "enabled": False,
    "max_workers": 4,
    "max_workers_per_profile": 2,
}

POLICY_DRAIN_MESSAGE = (
    "Waiting for execution cleanup before applying disabled parallel execution policy; "
    "new work is paused and the change will apply automatically."
)


class ExecutionPolicyDrainPending(RuntimeError):
    def __init__(self) -> None:
        super().__init__(POLICY_DRAIN_MESSAGE)


def normalize_execution_policy(value: object) -> dict:
    if value is None:
        return dict(DEFAULT_EXECUTION_POLICY)
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        raise ValueError("Invalid agent execution policy")
    if set(value) - DEFAULT_EXECUTION_POLICY.keys():
        raise ValueError("Unknown agent execution policy field")
    result = {**DEFAULT_EXECUTION_POLICY, **value}
    for field in ("max_workers", "max_workers_per_profile"):
        if type(result[field]) is not int or not 1 <= result[field] <= 32:
            raise ValueError(f"Invalid {field} execution limit")
    if result["max_workers_per_profile"] > result["max_workers"]:
        raise ValueError("Per-profile capacity exceeds office capacity")
    return {field: result[field] for field in DEFAULT_EXECUTION_POLICY}


def execution_resources(task: dict) -> list[str]:
    resources = task.get(
        "effective_execution_resources", task.get("execution_resources")
    )
    if resources is None:
        # Profile tool lists guide the worker; they do not restrict its native
        # CLI tool catalog. Even a Read-only list can execute shared writes.
        # Only an explicit task declaration may waive this reservation.
        return ["shared-workspace"]
    if (
        not isinstance(resources, list)
        or len(resources) > 16
        or any(
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", key) is None
            for key in resources
        )
        or len(set(resources)) != len(resources)
    ):
        raise ValueError("Invalid task execution resources")
    return list(resources)
