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


def completion_disposition(task: dict, event: dict, *, active_scripts: bool, started_script: bool = False,
                           capacity_wait: bool = False) -> str:
    if task.get("status") == "blocked" and task.get("human_action_request_id"):
        return "human_handoff"
    caller = event.get("_caller") or {}
    if caller and (
        caller.get("execution_generation") != task.get("execution_generation")
        or caller.get("execution_cycle") != task.get("execution_cycle")
        or caller.get("review_retry_epoch", 0) != task.get("review_retry_epoch", 0)
    ):
        return "superseded"
    if capacity_wait:
        return "capacity_handoff"
    # An attested script started by this exact phase/attempt is a durable
    # handoff, including Review. It is not an empty reviewer verdict. A random
    # active script from another attempt never suppresses review recovery.
    if task.get("status") in {"in_progress", "review", "blocked"} and started_script:
        return "script_handoff"
    if event.get("is_review_completion"):
        return "normal" if task.get("status") == "review" else "already_transitioned"
    if task.get("status") in {"done", "archived"}:
        return "already_transitioned"
    if task.get("status") == event.get("status"):
        return "already_transitioned"
    if task.get("status") == "in_progress" and active_scripts:
        return "script_handoff"
    return "normal"


# Longest triage report the daemon posts as a comment (block notices, S3).
_TRIAGE_REPORT_MAX_CHARS = 4000


def triage_failure_report(task: dict, event: dict) -> dict | None:
    """A Manager Assistant triage session that ended with an error.

    The task is already Blocked, so the executor completion's
    ``blocked -> blocked`` move is a no-op that used to drop the session's
    ESCALATED text, and nothing counted the failure: the dispatcher simply
    launched triage again. Returns ``{comment, cause, cycle, attempt_id}``
    for such a completion, else ``None``. The caller posts ``comment`` as a
    ``system`` activity (a Manager Assistant activity would start the
    blocked-triage cooldown although no triage ran) and counts the failure
    toward the triage launch-failure budget, so the third one files the
    existing escalation card.
    """
    caller = event.get("_caller") or {}
    if caller.get("task_mode") != "triage" or event.get("is_review_completion"):
        return None
    if task.get("status") != "blocked" or event.get("status") != "blocked":
        return None
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    error_class = str(details.get("error_class") or "")
    if not error_class or error_class == "cancelled":
        return None
    cycle = caller.get("execution_cycle")
    if isinstance(cycle, bool) or not isinstance(cycle, int):
        cycle = task.get("execution_cycle")
    if isinstance(cycle, bool) or not isinstance(cycle, int):
        return None
    report = str(event.get("comment") or "").strip()
    if len(report) > _TRIAGE_REPORT_MAX_CHARS:
        report = report[:_TRIAGE_REPORT_MAX_CHARS].rstrip() + "…"
    comment = (
        f"The Manager Assistant's triage of this blocked task ended with an "
        f"error ({error_class}); the task stays Blocked. Its report:\n\n"
        f"{report or '(no report)'}"
    )
    cause = (
        f"the triage session ended with an error ({error_class}): "
        f"{str(details.get('escalation_message') or '').strip()}"
    ).rstrip(": ")
    return {
        "comment": comment,
        "cause": cause,
        "cycle": cycle,
        "attempt_id": str(caller.get("attempt_id") or ""),
    }


async def record_triage_failure(publish_event, runtime_state, task_id: str, report: dict) -> int | None:
    """Post a failed triage's report and count it toward the launch budget.

    ``publish_event`` is the router's event publisher. The comment is posted
    as ``system`` with no ``_caller``: the attempt is over, and a Manager
    Assistant activity would start the blocked-triage cooldown although no
    triage ran. Returns the failure count for this execution cycle (``None``
    without a runtime ledger). Never raises: a lost report must not turn the
    completion into an unverified move.
    """
    import logging
    import uuid

    logger = logging.getLogger(__name__)
    try:
        await publish_event({
            "type": "task_activity", "task_id": task_id,
            "event_type": "comment", "actor": "system",
            "content": report["comment"],
            "details": {"triage_failure": True},
        })
    except Exception:
        logger.warning("Triage failure report for %s was not posted", task_id[:8], exc_info=True)
    if runtime_state is None:
        return None
    try:
        return runtime_state.record_triage_launch_failure(
            task_id, report["cycle"],
            report["attempt_id"] or f"completion-{uuid.uuid4().hex}",
            report["cause"],
        )
    except Exception:
        logger.warning("Triage failure for %s was not counted", task_id[:8], exc_info=True)
        return None
