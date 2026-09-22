"""Build worker progress without granting daemon receipt authority."""

from __future__ import annotations


def worker_activity(agent_name: str, event: dict) -> dict | None:
    """Reject reserved proof even when worker output rides authenticated transport.

    The supervisor attests who sent a frame; it does not turn the worker's
    payload into an operation observation or a persisted transition receipt.
    The backend independently enforces this boundary.
    """
    details = event.get("details")
    if event.get("event_type") == "execution_progress" or (
        isinstance(details, dict)
        and {"execution_event", "execution_source", "move_invocation"}.intersection(
            details
        )
    ):
        return None
    return {
        "type": "task_activity",
        "task_id": event.get("task_id", ""),
        "event_type": event.get("event_type", "checkpoint"),
        "actor": agent_name,
        "content": event.get("content", ""),
        "details": details if isinstance(details, dict) else None,
        "token_cost": event.get("token_cost"),
        **({"_caller": event["_caller"]} if event.get("_caller") else {}),
    }
