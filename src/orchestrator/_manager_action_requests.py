"""Manager ingest paths for script + scope + action-request events.

Extracted from ``manager_controller.py`` so the residual file can
stay focused on lifecycle + chat dispatch. Owns four entry points
the backend uses to synthesise a chat turn into the Manager without
the user typing anything:

* ``ingest_script_message`` — outbox watcher forwards a
  ``cubicle.notify_manager(...)`` drop from a running script.
* ``ingest_scope_completed`` — backend fires when a scope finishes
  and no follow-up scope is queued (Chat-2026-05).
* ``ingest_action_request_decided`` — user clicked approve / reject
  in the Inbox panel.
* ``ingest_action_request_auto_decide`` — backend hands the Manager
  a Manager-decidable action request to decide directly.

All four wrap the synthetic turn in a ``handle_chat_message(
source='script')`` call so they inherit the chat-lock serialisation
and never preempt an active user turn. Shared helper
``build_script_context_data`` builds the prompt-header envelope so
script / scope / action-request turns see the same workstream
context a user turn would.

Each function takes the owning ``ManagerController`` as the first
argument — same free-function-with-owner-param pattern as wave-10
and wave-11 extractions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestrator.manager_controller import ManagerController

logger = logging.getLogger("src.orchestrator.manager_controller")


def build_script_context_data(
    controller: "ManagerController", context_key: str,
) -> dict:
    """Context-data envelope for a script-origin chat turn.

    Populates the same shape ``build_dynamic_context`` reads for
    a user-chat turn so the Manager's prompt header looks
    identical whether the current turn is user- or script-
    originated — avoids drift where a script drop sees a
    degraded ("Workstream Unknown") prompt while a user turn
    sees the full context.

    What we CAN populate from the communicator side:
      * workstream_id / name / description / goals / priority
        (from ``config_store.get_workstream``).
      * ``scopes`` for the workstream (from config_store — kept
        fresh by the backend's sync_config broadcasts).

    What we deliberately OMIT:
      * ``task_summary`` / ``kb_summary`` / ``chat_history`` —
        these would require a synchronous backend RPC, which a
        script drop's critical path shouldn't pay for. The
        Manager's persistent session already has the latest
        summary from the most recent user turn (common case) or
        from the startup sync_config broadcast. If a drop is
        the first turn of the session after a cold boot, the
        prompt will lack a board summary; the Manager can
        request one via kanban.get_board if it needs it.
    """
    if context_key == "general_chat":
        return {}
    if not context_key.startswith("workstream:"):
        return {}
    ws_id = context_key.split(":", 1)[1]
    ws = None
    try:
        ws = controller._config.get_workstream(ws_id)
    except Exception:
        logger.exception(
            "Failed to look up workstream %s for script context_data",
            ws_id,
        )
    if ws is None:
        return {}
    data: dict = {
        "workstream_id": ws.get("id", ws_id),
        "workstream_name": ws.get("name", ""),
        "workstream_description": ws.get("description", ""),
        "workstream_goals": ws.get("goals", ""),
        "workstream_priority": ws.get("priority", "medium"),
    }
    # Scopes: the Manager uses these to avoid creating a second
    # 'preparing' scope, adding tasks to the wrong scope, etc.
    # Missing them on a script-origin turn would meaningfully
    # degrade the Manager's create_task decisions.
    try:
        scopes = controller._config.get_scopes_for_workstream(ws_id)
    except AttributeError:
        # Older ConfigStore without the helper — omit silently
        # rather than crash the whole drop.
        scopes = None
    except Exception:
        logger.exception(
            "Failed to fetch scopes for workstream %s (script ctx)",
            ws_id,
        )
        scopes = None
    if scopes:
        data["scopes"] = scopes
    return data


async def ingest_script_message(
    controller: "ManagerController",
    *,
    context_key: str,
    script_name: str,
    content: str,
    execution_id: str,
    attachments: list[str] | None = None,
) -> None:
    """Route a notification from a running script into the Manager."""
    prefixed = f"[Script: {script_name}] {content}"
    if attachments:
        block = "\n".join(f"- `{a}`" for a in attachments)
        prefixed = f"{prefixed}\n\n**Attachments:**\n{block}"

    # Synthesise a conversation_id that's recognisably from a
    # script — useful in logs. Deterministic from the
    # execution id so a replay of the same payload gets the
    # same id (handy for dedup on the backend side later).
    # Defensive fallback when both identifiers are blank
    # shouldn't happen in practice (the watcher always passes
    # ``script_name``), but defend against future callers.
    conv_id = (
        f"script-{execution_id}" if execution_id
        else f"script-{script_name}" if script_name
        else f"script-{id(controller)}"
    )

    msg = {
        "context_key": context_key,
        "user_message": prefixed,
        # Populate the context header the Manager's prompt
        # builder expects. Without this the prompt renders
        # "Workstream Unknown" and the Manager can't correctly
        # scope create_task calls.
        "context_data": build_script_context_data(controller, context_key),
        "conversation_id": conv_id,
    }
    logger.info(
        "Ingesting script notification: script=%s exec=%s ctx=%s",
        script_name, execution_id, context_key,
    )

    # Persist the script's trigger message as a ``role='system'``
    # chat row BEFORE the Manager processes it. Without this row,
    # the Manager's reply lands in chat with no antecedent — the
    # user sees a Manager response that looks orphaned. Best-effort
    # — a publish failure here mustn't block the IPC handoff.
    if controller._router is not None:
        try:
            await controller._router.publish_event({
                "type": "script_chat_trigger",
                "context_key": context_key,
                "script_name": script_name,
                "execution_id": execution_id,
                "content": content,
                "attachments": attachments or [],
            })
        except Exception:
            logger.debug(
                "script_chat_trigger publish failed (non-fatal) — "
                "Manager reply will appear orphaned in chat",
                exc_info=True,
            )

    await controller.handle_chat_message(msg, source="script")

    # Fire a board event so the UI can show a tiny "script
    # pinged the Manager" indicator without waiting for the
    # 5 s REST poll. Best-effort — a publish failure here must
    # NOT re-raise (the actual Manager ingest already succeeded
    # by this point; tripping the caller would trigger a phantom
    # retry on the outbox side).
    ws_id: str | None = None
    if context_key.startswith("workstream:"):
        ws_id = context_key.split(":", 1)[1]
    event_payload = {
        "type": "script_notified_manager",
        "script_name": script_name,
        "execution_id": execution_id,
        "workstream_id": ws_id,
        "message_preview": (content or "")[:200],
    }
    if controller._router is not None:
        try:
            await controller._router.publish_event(event_payload)
        except Exception:
            logger.debug(
                "script_notified_manager publish failed "
                "(non-fatal)",
                exc_info=True,
            )


async def ingest_scope_completed(
    controller: "ManagerController", message: dict,
) -> None:
    """Route a scope-completion event from the backend into the
    Manager so it can plan the next step (CHAT-2026-05).

    The backend fires this when an executing scope transitions to
    ``done`` AND there's no next scope queued in the workstream.
    Without this nudge, the Manager waits indefinitely after each
    scope even when the user's overall request needs more work.
    """
    context_key = (message or {}).get("context_key", "general_chat")
    readable_id = (message or {}).get("scope_readable_id", "")
    scope_name = (message or {}).get("scope_name") or readable_id
    task_count = (message or {}).get("task_count") or 0

    logger.info(
        "Ingesting scope_completed notification for %s (%s, %d tasks)",
        readable_id, context_key, task_count,
    )

    lines = [
        f"[Scope Completed: {readable_id}]",
        (
            f'Scope "{scope_name}" finished. {task_count} '
            f"task{'s' if task_count != 1 else ''} done. No "
            "follow-up scope is queued."
        ),
        "",
        "Assess the current workstream state via list_scopes / "
        "get_board and decide the next step: plan and activate "
        "the next scope, ask the user for clarification if the "
        "overall goal isn't clear, or report completion if the "
        "original request is fulfilled.",
    ]
    content = "\n".join(lines)

    # Deterministic conversation id derived from the scope id so
    # a duplicate event (rare but possible on retries) doesn't
    # double-prompt the Manager.
    conv_id = (
        f"scope-{readable_id}" if readable_id
        else f"scope-{id(controller)}"
    )

    msg = {
        "context_key": context_key,
        "user_message": content,
        "context_data": build_script_context_data(controller, context_key),
        "conversation_id": conv_id,
    }
    await controller.handle_chat_message(msg, source="script")


async def ingest_planner_result(
    controller: "ManagerController", message: dict,
) -> None:
    """Poke the Manager after a Planner consult finishes
    (execution_improvements_v1 Phase 3).

    The Planner ran asynchronously, wrote its plan/verdict via the plan
    tools, and exited. This nudges the Manager to act on the fresh plan
    (review the roadmap, create/activate the next scope, etc.). The
    ``planner_consult`` marker (mode + ids) rides the ``task_complete``
    event from the Planner worker. Fire-and-forget.
    """
    consult = (message or {}).get("planner_consult") or {}
    mode = (consult.get("mode") or "roadmap").strip()
    workstream_id = consult.get("workstream_id") or ""
    scope_id = consult.get("scope_id") or ""
    context_key = (
        f"workstream:{workstream_id}" if workstream_id else "general_chat"
    )

    if mode == "roadmap":
        body = (
            "The Planner has written/updated the workstream roadmap (the "
            "ordered list of intended scopes). Review it via "
            "get_workstream_plan, then create + activate the FIRST scope "
            "(create_scope → create_task × N → activate_scope). Create only "
            "ONE scope now; the rest stay in the roadmap until each is done "
            "and verified."
        )
    elif mode == "scope_plan":
        body = (
            "The Planner has written the execution plan for the scope. "
            "Review it via get_scope, make sure its tasks + briefs are "
            "complete, then activate the scope when ready."
        )
    elif mode == "verify":
        body = (
            "The Planner has completed scope verification. Check the scope's "
            "verification status via get_scope. If it passed, the scope is "
            "done — plan the next scope from the roadmap. If it failed, the "
            "Planner created rework tasks and the scope is executing again."
        )
    else:  # research
        body = (
            "The Planner has finished research and written findings into the "
            "plan. Read them via get_workstream_plan / get_scope and decide "
            "the next step."
        )

    lines = ["[Planner]", body]
    content = "\n".join(lines)

    conv_id = (
        f"planner-{mode}-{scope_id or workstream_id or id(controller)}"
    )
    msg = {
        "context_key": context_key,
        "user_message": content,
        "context_data": build_script_context_data(controller, context_key),
        "conversation_id": conv_id,
    }
    logger.info(
        "Ingesting planner_result (mode=%s, %s)", mode, context_key,
    )
    # source="script" so the poke parks behind any in-flight user turn
    # (same posture as ingest_scope_completed / ingest_action_request_decided).
    await controller.handle_chat_message(msg, source="script")


async def ingest_action_request_decided(
    controller: "ManagerController", message: dict,
) -> None:
    """Route a user's action_request decision into the Manager.

    Mirrors the scope_completed nudge: the backend fires this
    when the user clicks approve / reject in the Inbox panel,
    and the Manager gets a synthetic chat turn so it can react
    without polling.
    """
    context_key = (message or {}).get("context_key", "general_chat")
    request_id = (message or {}).get("request_id", "")
    request_type = (message or {}).get("request_type", "unknown")
    decision = (message or {}).get("decision", "decided")
    notes = (message or {}).get("decision_notes", "") or ""
    resulting_task = (message or {}).get("resulting_task_id") or None
    source_task = (message or {}).get("source_task_id") or None
    requesting_agent = (message or {}).get("requesting_agent", "")

    logger.info(
        "Ingesting action_request_decided: id=%s decision=%s type=%s",
        request_id[:8] if request_id else "?", decision, request_type,
    )

    lines = [
        f"[Action Request {decision.title()}: {request_type}]",
        (
            f"The user just {decision} an action request"
            + (f" from {requesting_agent}" if requesting_agent else "")
            + f" (type: {request_type})."
        ),
    ]
    if notes:
        lines.append(f"User notes: {notes}")
    if resulting_task:
        lines.append(
            f"A new task was created from the approval: {resulting_task}. "
            "Verify it has the right brief and dependencies."
        )
    if source_task:
        lines.append(
            f"The request originated from task {source_task}. "
            "Check whether that task is now unblocked and any "
            "follow-up planning is needed."
        )
    lines.append(
        "Assess what (if anything) you need to do next: re-plan, "
        "update a task brief, post a comment, or report back to "
        "the user. Do NOT take action that the user already "
        "explicitly rejected."
    )
    content = "\n".join(lines)

    # Deterministic conv id so a duplicate delivery of the same
    # decision doesn't double-prompt.
    conv_id = (
        f"action-req-{request_id}" if request_id
        else f"action-req-{id(controller)}"
    )
    msg = {
        "context_key": context_key,
        "user_message": content,
        "context_data": build_script_context_data(controller, context_key),
        "conversation_id": conv_id,
    }
    await controller.handle_chat_message(msg, source="script")


async def ingest_action_request_auto_decide(
    controller: "ManagerController", message: dict,
) -> None:
    """Route a newly-created Manager-decidable action_request into
    the Manager so it decides without waiting on the user.
    """
    context_key = (message or {}).get("context_key", "general_chat")
    request_id = (message or {}).get("request_id", "")
    request_type = (message or {}).get("request_type", "unknown")
    severity = (message or {}).get("severity", "medium")
    category = (message or {}).get("category", "workstream")
    requesting_agent = (message or {}).get("requesting_agent", "")
    source_task = (message or {}).get("source_task_id") or None
    scope_id = (message or {}).get("scope_id") or None
    payload = (message or {}).get("payload") or {}
    justification = (message or {}).get("justification") or ""

    logger.info(
        "Ingesting action_request_auto_decide: id=%s type=%s "
        "severity=%s category=%s",
        (request_id[:8] if request_id else "?"),
        request_type, severity, category,
    )

    # Format a compact summary so the Manager has the essentials
    # without parsing the whole payload itself. Keep this terse —
    # the Manager will pull the full row via the action_request
    # service if it needs to.
    payload_lines: list[str] = []
    for key, value in (payload.items() if isinstance(payload, dict) else []):
        v = str(value)
        if len(v) > 200:
            v = v[:197] + "…"
        payload_lines.append(f"  - {key}: {v}")

    lines = [
        f"[Action Request — Auto-Decide: {request_type}]",
        (
            f"A new action_request landed in the Manager-auto-decide "
            f"queue (id `{request_id}`, severity `{severity}`, "
            f"category `{category}`). The user has NOT been "
            "notified — you decide directly."
        ),
        "",
        f"Requested by: {requesting_agent or '(system)'}",
    ]
    if source_task:
        lines.append(f"Source task: {source_task}")
    if scope_id:
        lines.append(f"Scope: {scope_id}")
    if justification:
        lines.append("")
        lines.append("Justification:")
        for ln in justification.splitlines():
            lines.append(f"  {ln}")
    if payload_lines:
        lines.append("")
        lines.append("Payload:")
        lines.extend(payload_lines)
    lines.append("")
    lines.append(
        "**Decide now via "
        "`mcp__cubicle-tools__decide_action_request`** with the "
        "request_id above and either `decision=\"approved\"` "
        "(if the proposal fits the workstream goal) or "
        "`decision=\"rejected\"` (with `decision_notes` "
        "explaining why)."
    )
    lines.append("")
    lines.append(
        "**CRITICAL — side-effects on approval are NOT automatic "
        "for most request types.** Only `create_task` (creates "
        "the task) and `request_clarification` (posts an answer "
        "activity on the source task) apply their side-effect "
        "inside `decide_action_request`. For ALL other types — "
        "`create_subtask`, `update_task`, `move_task`, "
        "`split_into_scope`, `request_review_check`, "
        "`escalate_blocker`, `propose_artifact_handoff`, "
        "`board_overview`, `setup_office_secret` — approve "
        "records the decision but you MUST ALSO call the "
        "corresponding tool yourself in the SAME turn:"
    )
    lines.append("")
    lines.append("  - `create_subtask` → call `create_task` with `parent_task_id`")
    lines.append("  - `update_task` / `move_task` → call those tools")
    lines.append("  - `split_into_scope` → call `create_scope` then `create_task` × N")
    lines.append("  - `request_review_check` → call `update_task` to set reviewer (or trigger your own review)")
    lines.append("  - `propose_artifact_handoff` → create the consumer task that needs the artifact")
    lines.append("  - `escalate_blocker` → take the user-visible remedial action (typically the user is the actor; you may need to comment or create a clarifying task)")
    lines.append("  - `board_overview` → no side-effect needed; the row is informational")
    lines.append("  - `setup_office_secret` → no Manager-side action; the user adds the secret in Settings → Security and the backend auto-resolves the row")
    lines.append("")
    lines.append(
        "Approving without the follow-up tool call silently "
        "drops the proposed work. Reject closes the row with "
        "no side-effect. The `informational` type is "
        "acknowledge-only — neither approve nor reject applies; "
        "use `decide_action_request` with `decision=\"approved\"` "
        "to mark it acknowledged. See your Manager CLAUDE.md — "
        "the **Auto-Deciding Action Requests** section — for "
        "the full decision tree."
    )
    content = "\n".join(lines)

    # Deterministic conv id so a duplicate delivery doesn't
    # double-prompt the Manager.
    conv_id = (
        f"auto-decide-{request_id}" if request_id
        else f"auto-decide-{id(controller)}"
    )
    msg = {
        "context_key": context_key,
        "user_message": content,
        "context_data": build_script_context_data(controller, context_key),
        "conversation_id": conv_id,
    }
    await controller.handle_chat_message(msg, source="script")
