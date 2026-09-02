"""Parameter transforms for MCP tools (split from mcp_tool_server.py).

Translates backend-friendly parameter shapes from Claude CLI's
arg conventions. Pure function with the same signature as the
original ``_transform_params``.

Reads ``TASK_ID`` / ``AGENT_NAME`` from ``os.environ`` (the same
values mcp_tool_server.py sees) so the propose_* transforms can
auto-inject them. Reading at call time rather than import time
means a test that monkeypatches the env vars after import still
sees the override.
"""
from __future__ import annotations

import os


def transform_params(action: str, transform: str | None, params: dict) -> dict:
    """Apply transforms to tool parameters before sending to backend."""
    # P3-F: these used to be module-level globals in mcp_tool_server.py.
    # Reading via ``os.environ.get`` at call time keeps the helper
    # decoupled from the entrypoint and survives env mutation in tests.
    TASK_ID = os.environ.get("TASK_ID", "")  # noqa: N806 — matches original
    AGENT_NAME = os.environ.get("AGENT_NAME", "")  # noqa: N806

    # Auto-inject context for file operations
    if action == "office_save_file":
        # Auto-attach files to the current task
        if TASK_ID and "source_task_id" not in params:
            params["source_task_id"] = TASK_ID
        if AGENT_NAME and "source_agent" not in params:
            params["source_agent"] = AGENT_NAME
        return params

    if transform == "move_task":
        out = {
            "task_id": params.get("task_id", ""),
            "new_status": params.get("new_status", ""),
            "actor": AGENT_NAME or "manager",
            "comment": params.get("comment", ""),
        }
        # Forward a structured review verdict so the backend can attach it to
        # the comment activity row's ``details`` (rendered as a card). Unlike
        # the add_activity carrier, this path bypasses the activity-detail
        # whitelist, so the verdict does NOT re-enter agent context on
        # get_task_detail / get_board.
        verdict = params.get("verdict")
        if isinstance(verdict, dict):
            out["verdict"] = verdict
        return out
    elif transform == "archive_task":
        return {
            "task_id": params.get("task_id", ""),
            "new_status": "archived",
            "actor": "manager",
            "comment": params.get("comment", "Archived by Manager"),
        }
    elif transform == "add_activity":
        out = {
            "task_id": params.get("task_id", ""),
            "event_type": params.get("event_type", "comment"),
            "actor": AGENT_NAME or "manager",
            "content": params.get("content", ""),
        }
        # T5.1.2 (04/F3): forward structured ``details`` (e.g. the blocker
        # escalation's ``blocker_class``) through the SAME whitelist the
        # activity-reader uses, so the channel the worker prompt + MA
        # playbook + worker-spec all describe is actually writable. Unknown
        # keys are dropped; an empty/absent details blob adds nothing.
        raw_details = params.get("details")
        if isinstance(raw_details, dict):
            slim = {
                k: raw_details[k]
                for k in _ACTIVITY_DETAIL_KEEP
                if k in raw_details
            }
            if slim:
                out["details"] = slim
        return out
    elif transform == "propose_task":
        return {
            "task_id": params.get("task_id", ""),
            "event_type": "task_proposed",
            "actor": AGENT_NAME or "worker",
            "content": params.get("content", ""),
        }
    # ── Action Request transforms ────────────────────────────────────
    # Each rewrites a typed worker-friendly tool call into the generic
    # ``propose_action`` shape the backend dispatcher expects:
    #   {request_type, payload, justification, source_task_id?,
    #    requesting_agent}
    # ``source_task_id`` is auto-injected from TASK_ID env so workers
    # don't have to thread it through every call.
    elif transform == "propose_subtask":
        return {
            "request_type": "create_subtask",
            "payload": {
                "title": params.get("title", ""),
                "brief_hints": params.get("brief_hints") or {},
                # Phase A backend doesn't read parent_task_id from
                # payload — it derives the parent from source_task_id.
                # Pass it anyway so logs show what the worker meant.
                "parent_task_id": TASK_ID,
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "propose_split_into_scope":
        return {
            "request_type": "split_into_scope",
            "payload": {
                "scope_short_key": params.get("scope_short_key", ""),
                "scope_name": params.get("scope_name", ""),
                "tasks": params.get("tasks") or [],
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "propose_update_task":
        return {
            "request_type": "update_task",
            "payload": {
                "task_id": params.get("task_id", ""),
                "changes": params.get("changes") or {},
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "escalate_blocker":
        payload = {
            "blocker_summary": params.get("blocker_summary", ""),
            "suggested_unblock": params.get("suggested_unblock") or "",
        }
        # T3.3.1 (04/F1): the tool REQUIRES blocker_class and the
        # backend maps it class→category for routing (credentials /
        # infrastructure classes land in the user Inbox). Dropping it
        # here silently downgraded every escalation to the Manager
        # auto-decide queue. Only include when supplied so legacy
        # callers fall back to the backend's keyword side-channel.
        if params.get("blocker_class"):
            payload["blocker_class"] = params["blocker_class"]
        # WRK-02: a reviewer escalating at the rework cap sets rework_cap=true
        # so the backend forces the AR to the USER inbox (2 failed rework
        # cycles is a human decision, not a Manager auto-decide).
        if params.get("rework_cap"):
            payload["rework_cap"] = True
        return {
            "request_type": "escalate_blocker",
            "payload": payload,
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "request_clarification":
        return {
            "request_type": "request_clarification",
            "payload": {"question": params.get("question", "")},
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "request_review_check":
        payload: dict = {}
        if "criterion_index" in params:
            payload["criterion_index"] = params["criterion_index"]
        if TASK_ID:
            payload["task_id"] = TASK_ID
        return {
            "request_type": "request_review_check",
            "payload": payload,
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "propose_artifact_handoff":
        return {
            "request_type": "propose_artifact_handoff",
            "payload": {
                "source_task_id": TASK_ID,
                "target_task_id": params.get("target_task_id", ""),
                "file_path": params.get("file_path", ""),
            },
            "justification": params.get("justification", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    elif transform == "propose_spec_update":
        return {
            "request_type": "propose_spec_update",
            "payload": {
                "spec_id": params.get("spec_id"),
                "target": params.get("target"),
                "proposed_text": params.get("proposed_text", ""),
                "rationale": params.get("rationale", ""),
            },
            "justification": params.get("rationale", ""),
            "source_task_id": TASK_ID,
            "requesting_agent": AGENT_NAME or "worker",
        }
    # ask_user_choice (pivot-2 P1): pin the choice row to the Manager
    # turn's LOCKED context. The context comes from the session env (set
    # by the orchestrator at spawn) — NEVER from the model (L-6): a
    # hallucinated/stale ``context_key`` would pin the question (and the
    # click's consent side effect) to the WRONG conversation, so the env
    # overrides unconditionally and any model-supplied value is dropped.
    # Unknown params are stripped by the whitelist below — the catalog
    # convention omits ``additionalProperties: false``, so the CLI does
    # not reject extras; this whitelist is the schema guard's stand-in.
    # CONTEXT_KEY is set only on Manager sessions ("general_chat" |
    # "workstream:{uuid}"); the backend refuses non-workstream contexts
    # (and the tool is stripped in General Chat anyway).
    if action == "ask_user_choice":
        out = {
            k: params[k]
            for k in (
                # "questions" — the intake card's sub-questions (pivot-3
                # P1-6); without it here the whitelist would silently
                # strip every intake ask down to an optionless shell.
                # "proposed_agent" — the hire card's profile (pivot-4
                # P2-4); stripping it would turn every hire ask into a
                # profile-less card the backend refuses.
                # "topic" / "derived_values" — the pivot-4 flow-intake
                # extensions (spec §A); topic is REQUIRED for intake
                # kind backend-side, so stripping it would refuse every
                # intake ask (the "questions" lesson, again). The
                # per-question fields (multi/min_select/max_select/
                # requires_input) ride inside "questions".
                # "flow_name" / "derived_preview" — the run_flow consent
                # card (Flow Studio FS-P2.T9); flow_name is REQUIRED for
                # the kind backend-side, so stripping it would refuse
                # every run_flow ask (the "questions" lesson, again).
                "question", "options", "kind", "questions",
                "topic", "derived_values",
                "proposed_workstream_name", "proposed_agent",
                "flow_name", "derived_preview",
            )
            if k in params
        }
        context_key = os.environ.get("CONTEXT_KEY", "")
        if context_key:
            out["context_key"] = context_key
        return out
    # Office-memory v1 (T3.2): memory scope is derived SERVER-side —
    # the worker's task_id (→ its workstream) and the Manager's
    # context_key are injected from the session env, NEVER from the
    # model (the ask_user_choice L-6 posture: a hallucinated/stale
    # scope key would read or write the WRONG workstream's memory).
    # The whitelist doubles as the schema guard's stand-in (the
    # catalog convention omits ``additionalProperties: false``), so a
    # client-supplied task_id / context_key / workstream_id is DROPPED
    # here even before the env injection overrides.
    if action in ("memory_recall", "memory_remember"):
        keep = (
            ("query", "kind", "slug", "include_office")
            if action == "memory_recall"
            else ("kind", "title", "body", "tags", "supersedes", "office_wide")
        )
        out = {k: params[k] for k in keep if k in params}
        if TASK_ID:
            out["task_id"] = TASK_ID
        context_key = os.environ.get("CONTEXT_KEY", "")
        if context_key:
            out["context_key"] = context_key
        return out
    # retry_blocked_task: inject the actor from AGENT_NAME so the
    # backend can gate on manager vs manager-assistant correctly. The
    # backend handler refuses workers explicitly, so a worker who
    # somehow gets this tool (shouldn't happen — manager_tools only)
    # would still be rejected.
    if action == "retry_blocked_task":
        return {
            "task_id": params.get("task_id", ""),
            "reason": params.get("reason", ""),
            "actor": AGENT_NAME or "manager",
        }
    # decide_action_request: the Manager calls this from the
    # auto-decide synthetic turn. Inject ``actor`` from AGENT_NAME so
    # the backend stamps ``handled_by`` correctly (the row's audit
    # trail shows "Manager" not "user"). Worker tools don't include
    # this verb so no risk of the worker spoofing it.
    if action == "decide_action_request":
        return {
            "request_id": params.get("request_id", ""),
            "decision": params.get("decision", ""),
            "decision_notes": params.get("decision_notes", ""),
            "actor": AGENT_NAME or "manager",
        }
    # schedule_assignment / update_assignment_schedule (pivot-3 review
    # F2/F3): the tool schema is model-friendly — top-level ``prompt``
    # for manager_digest schedules, ``autonomy_note`` NESTED inside
    # ``brief_template`` for agent_task schedules — while the backend
    # stores the digest prompt INSIDE ``brief_template`` and
    # ``autonomy_note`` as a top-level column. Reshape here (this layer
    # is the canonical seam for exactly this) so every schema-conformant
    # call lands in the backend shape; the backend handler ALSO accepts
    # both shapes as the belt behind this transform.
    if action in ("schedule_assignment", "update_assignment_schedule"):
        out = dict(params)
        template = out.get("brief_template")
        template = dict(template) if isinstance(template, dict) else None
        prompt = out.pop("prompt", None)
        if isinstance(prompt, str):
            # Merge into (or create) brief_template — top-level wins on
            # the rare double-supply so the model's explicit arg rules.
            template = {} if template is None else template
            template["prompt"] = prompt
        if template is not None and "autonomy_note" in template:
            note = template.pop("autonomy_note")
            # Hoist to the top-level column slot; an explicit top-level
            # value (already the backend shape) wins over the nested one.
            out.setdefault("autonomy_note", note)
        if template is not None:
            out["brief_template"] = template
        return out
    return params


# ── Lean response projections ─────────────────────────────────────
#
# The Manager session is long-lived and resumable; every tool RESULT it
# receives is appended to its conversation transcript and replayed on the
# next `--resume`. ``get_board`` (called most turns) returns the full
# TaskResponse for every task — including the variable-length
# ``description`` and a pile of UUIDs / timestamps / display metadata the
# Manager never reasons over. Over a long workstream that accumulated
# ~3 MB of board dumps and was the single biggest driver of the context
# bloat that wedged the session.
#
# These projections trim each board/task read down to the fields the
# Manager actually orchestrates on, BEFORE the result enters its context.
# This is "send less" — it does not add a layer or an extra prompt; it
# just stops us over-feeding the native context manager. The platform's
# REST API and the UI are unaffected (they don't go through this path).

# Per-task fields kept in a board listing. Everything else (description,
# office_id/workstream_id, *_display_name, *_emoji, parent_task_id,
# rework_count, has_brief, token_cost, session_id, timestamps) is dropped
# — the Manager uses get_task_detail when it needs a single task in full.
_BOARD_TASK_KEEP = (
    "id",                 # kept so move_task/update_task by UUID still work
    "readable_id",
    "title",
    "status",
    "assigned_agent",
    "reviewer",
    "priority",
    "labels",
    "workstream_short_code",  # tiny; lets the Manager map tasks → workstream
                              # in a multi-workstream (General Chat) board read
    "scope_short_key",
    "scope_readable_id",
    "brief_is_complete",
    "depends_on",
    "completed_at",       # F12 (pivot-3): lets digest/summary turns date
                          # completions ("done this week" vs last month)
                          # without a second fetch; null while non-terminal
)

_MAX_DETAIL_ACTIVITIES = 10      # keep only the most recent N
_MAX_ACTIVITY_CONTENT = 600      # chars per LOW-signal activity content
# TOOL-05: high-signal events carry the actionable payload the reading agent
# most needs — a Manager `answer`, a worker `question`, and any ESCALATED
# blocker comment. A flat 600-char END cap severed the tail of these (the
# "What's needed to resume" bullets of an escalation, the back half of a long
# Manager answer). Give them a larger budget AND truncate MIDDLE-OUT so both
# the summary head and the actionable tail survive; keep the tight end-cap for
# checkpoint / tool_run noise.
_MAX_HIGH_SIGNAL_CONTENT = 2000
_HIGH_SIGNAL_EVENTS = ("answer", "question")
_ESCALATED_PREFIX = "ESCALATED ("
# Activity ``details`` keys worth keeping (routing signals); the rest of
# the (often large) details blob is dropped.
_ACTIVITY_DETAIL_KEEP = ("blocker_class", "error_class", "new_status")


def _is_high_signal(event_type: object, content: str) -> bool:
    return (
        event_type in _HIGH_SIGNAL_EVENTS
        or content.lstrip().startswith(_ESCALATED_PREFIX)
    )


def _truncate_activity_content(content: str, event_type: object) -> str:
    """Trim one activity's content for the lean projection. Low-signal events
    get a tight end-cap; high-signal events (answers, questions, ESCALATED
    blockers) get a larger budget with middle-out truncation so the head
    (summary) and tail (what's-needed) both survive."""
    if _is_high_signal(event_type, content):
        if len(content) <= _MAX_HIGH_SIGNAL_CONTENT:
            return content
        # Middle-out: keep the first and last halves of the budget.
        half = _MAX_HIGH_SIGNAL_CONTENT // 2
        dropped = len(content) - 2 * half
        marker = f"\n…({dropped} chars omitted)…\n"
        # Only middle-out when it actually SHORTENS the content — a message
        # barely over the cap isn't worth mangling (and the marker could make it
        # longer). Keep those whole.
        if 2 * half + len(marker) >= len(content):
            return content
        return f"{content[:half]}{marker}{content[-half:]}"
    if len(content) > _MAX_ACTIVITY_CONTENT:
        return content[:_MAX_ACTIVITY_CONTENT] + " …(truncated)"
    return content


def _lean_task(task: dict) -> dict:
    return {k: task[k] for k in _BOARD_TASK_KEEP if k in task}


def project_response(action: str, result: object) -> object:
    """Trim a board/task read to a lean projection before it enters the
    agent's context. No-op for every other action, for errors, and for
    unexpected shapes (defensive — never raise, never drop data we can't
    safely project)."""
    if not isinstance(result, dict) or result.get("error"):
        return result

    if action == "get_board":
        items = result.get("items")
        if isinstance(items, list):
            lean = dict(result)
            lean["items"] = [
                _lean_task(t) if isinstance(t, dict) else t for t in items
            ]
            # TOOL-11: the backend caps get_board at ``limit`` (default 100). A
            # board larger than that silently truncated with no in-band signal.
            # Tell the model explicitly so it can page with offset instead of
            # assuming it saw the whole board.
            total = result.get("total")
            if isinstance(total, int) and len(items) < total:
                lean["truncated"] = True
                lean["hint"] = (
                    f"Showing {len(items)} of {total} tasks. Narrow with a "
                    "filter (status/workstream_id/assigned_agent) or page with "
                    "offset to see the rest."
                )
            return lean
        return result

    if action == "get_task_detail":
        # The detail view is meant to be FAITHFUL — the Manager asked for
        # one task in full. The only real bloat is the activity feed (up to
        # 20 entries, each with full content + a potentially large details
        # blob). Trim ONLY that; leave the task fields, brief, and artifacts
        # intact (dropping structural ids to save ~150 bytes on a rare
        # single-task read isn't worth the risk of breaking a follow-up
        # action that referenced them).
        acts = result.get("recent_activities")
        if not isinstance(acts, list):
            return result
        lean = dict(result)
        trimmed = []
        for a in acts[-_MAX_DETAIL_ACTIVITIES:]:
            if not isinstance(a, dict):
                trimmed.append(a)
                continue
            content = _truncate_activity_content(
                a.get("content") or "", a.get("event_type")
            )
            details = a.get("details") or {}
            slim_details = {
                k: details[k] for k in _ACTIVITY_DETAIL_KEEP
                if isinstance(details, dict) and k in details
            }
            trimmed.append({
                "event_type": a.get("event_type"),
                "actor": a.get("actor"),
                "content": content,
                "details": slim_details,
                "created_at": a.get("created_at"),
            })
        lean["recent_activities"] = trimmed
        return lean

    return result


# ── MCP Protocol (JSON-RPC over stdio) ────────────────────────────

