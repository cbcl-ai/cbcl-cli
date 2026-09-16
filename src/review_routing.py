"""Choose queue destinations; the backend claim owns reviewer persistence."""

from collections.abc import Callable


def default_reviewer(task: dict) -> str:
    return (
        "auditor"
        if task.get("assigned_agent") == "manager-assistant"
        else "manager-assistant"
    )


def review_queue_agent(
    task: dict,
    is_dispatchable: Callable[[str], bool] | None = None,
) -> str:
    """Recover a missing/inactive reviewer without changing the task owner.

    Do not replace an active but busy reviewer. The normal process gate waits
    for that reviewer; only the backend's fenced claim can persist a fallback.
    """
    reviewer = task.get("reviewer") or default_reviewer(task)
    if is_dispatchable is not None and not is_dispatchable(reviewer):
        return default_reviewer(task)
    return reviewer
