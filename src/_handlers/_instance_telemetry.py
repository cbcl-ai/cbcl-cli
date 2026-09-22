"""Task-owned Agent telemetry and backward-compatible Profile summaries."""

from __future__ import annotations

import math
import time


def wire_instance_status(state: dict) -> dict:
    """Keep execution identity and original observation time in snapshots."""
    result = dict(state)
    raw = state.get("status")
    result["status"] = (
        "working"
        if raw == "working"
        else "error" if raw in ("crashed", "error") else "idle"
    )
    if any(
        state.get(key) is True
        for key in (
            "execution_cleanup_failed",
            "execution_finalization_pending",
        )
    ):
        result["status"] = "error"
    return result


def worker_status_events(
    agent_name: str, event: dict, status: str, supervisor
) -> list[dict]:
    """Build exact-instance plus aggregate events without idling busy siblings.

    The callback precedes the supervisor's terminal state transition, so the
    current attempt is overlaid on its snapshot. An old attempt never replaces
    a newer snapshot in the compatibility Profile summary.
    """
    caller = event.get("_caller") or {}
    instance_id = caller.get("agent_instance_id")
    generation = caller.get("execution_generation")
    if not (
        instance_id
        and caller.get("profile_id")
        and caller.get("attempt_id")
        and caller.get("task_id")
        and caller.get("task_mode") in ("execute", "review", "triage")
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation > 0
    ):
        return []
    states = supervisor.get_instance_statuses() if supervisor is not None else {}
    if not isinstance(states, dict):
        states = {}
    previous = states.get(instance_id) or {}
    same_attempt = (
        previous.get("attempt_id") == caller["attempt_id"]
        and previous.get("execution_generation") == generation
    )
    observed_at = event.get("observed_at")
    if (
        not isinstance(observed_at, (int, float))
        or isinstance(observed_at, bool)
        or not math.isfinite(observed_at)
    ):
        observed_at = previous.get("observed_at") if same_attempt else None
    if (
        not isinstance(observed_at, (int, float))
        or isinstance(observed_at, bool)
        or not math.isfinite(observed_at)
    ):
        observed_at = time.time()
    instance = {
        "agent_instance_id": instance_id,
        "profile_id": caller["profile_id"],
        "agent_name": agent_name,
        "task_id": caller["task_id"],
        "attempt_id": caller["attempt_id"],
        "execution_generation": generation,
        "execution_mode": caller["task_mode"],
        "status": status,
        "current_task": caller["task_id"] if status == "working" else None,
        "observed_at": observed_at,
    }
    profile_states = {
        key: wire_instance_status(value)
        for key, value in states.items()
        if value.get("agent_name") == agent_name
    }
    if not previous or (same_attempt and observed_at >= previous.get("observed_at", 0)):
        profile_states[instance_id] = instance
    working = [
        value for value in profile_states.values() if value["status"] == "working"
    ]
    profile_status = (
        "working"
        if working
        else (
            "error"
            if any(value["status"] == "error" for value in profile_states.values())
            else "idle"
        )
    )
    return [
        {"type": "agent_instance_status_changed", **instance},
        {
            "type": "agent_status_changed",
            "agent_name": agent_name,
            "display_name": agent_name,
            "status": profile_status,
            "current_task": (
                working[0].get("current_task") if len(working) == 1 else None
            ),
            "current_task_title": None,
            "running_count": len(working),
        },
    ]
