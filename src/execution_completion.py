"""Distinguish execution completion from an explicit durable handoff."""


def completion_task_state(response) -> dict | None:
    if response.status_code == 404:
        return None
    response.raise_for_status()
    task = response.json()
    if not isinstance(task, dict) or not task.get("status"):
        raise RuntimeError("Authoritative completion state could not be verified")
    return task


def completion_move_result(response) -> dict | None:
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("Task completion response could not be verified")
    if result.get("code") == "stale_execution":
        return None
    if result.get("error"):
        raise RuntimeError("Task completion was not accepted by the platform")
    return result


def completion_disposition(task: dict, event: dict, *, active_scripts: bool, started_script: bool = False) -> str:
    if task.get("status") == "blocked" and task.get("human_action_request_id"):
        return "human_handoff"
    caller = event.get("_caller") or {}
    if caller and (
        caller.get("execution_generation") != task.get("execution_generation")
        or caller.get("execution_cycle") != task.get("execution_cycle")
        or caller.get("review_retry_epoch", 0) != task.get("review_retry_epoch", 0)
    ):
        return "superseded"
    if event.get("is_review_completion"):
        return "normal" if task.get("status") == "review" else "already_transitioned"
    if task.get("status") in {"done", "archived"}:
        return "already_transitioned"
    if task.get("status") == event.get("status"):
        return "already_transitioned"
    if task.get("status") == "in_progress" and (active_scripts or started_script):
        return "script_handoff"
    return "normal"
