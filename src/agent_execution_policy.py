"""Validated daemon policy; malformed sync cannot silently enable concurrency."""

import re

DEFAULT_EXECUTION_POLICY = {
    "enabled": False,
    "max_workers": 4,
    "max_workers_per_profile": 2,
}

# Shown to the office admin in Office Settings (health report ``errors`` ->
# ``OfficeStatus.config_sync_error``), so they are written in plain words and
# name no path, task or credential. Neither says no action is needed: an
# unconfirmed run can hold the drain until someone looks at it.
POLICY_DRAIN_MESSAGE = (
    "Turning off parallel execution will apply after the work that is running "
    "now finishes. New work is paused until then."
)
# Used while a worker run's shutdown could not be confirmed. The daemon keeps
# retrying, but a stopped office container keeps the change from applying.
POLICY_DRAIN_SHUTDOWN_UNCONFIRMED_MESSAGE = (
    "Turning off parallel execution is waiting for a run whose shutdown could "
    "not be confirmed. New work is paused until then. Retries are automatic; "
    "if this message stays, check that the office container is running."
)


class ExecutionPolicyDrainPending(RuntimeError):
    def __init__(self, *, shutdown_unconfirmed: bool = False) -> None:
        super().__init__(
            POLICY_DRAIN_SHUTDOWN_UNCONFIRMED_MESSAGE
            if shutdown_unconfirmed
            else POLICY_DRAIN_MESSAGE
        )


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
