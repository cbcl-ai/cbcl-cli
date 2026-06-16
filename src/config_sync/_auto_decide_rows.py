"""Per-request-type auto-decide policy rows (T5.3.1).

The Manager's auto-decide guidance used to live as a ~1.8k-token markdown
table in the STANDING Manager CLAUDE.md — loaded every turn of a long-lived
resumed session, even though it's only relevant when an
`[Action Request — Auto-Decide: <type>]` synthetic turn arrives. The synthetic
turn builder (`_manager_action_requests.ingest_action_request_auto_decide`)
knows the `request_type`, so it injects the universal preamble + ONLY the row
for that type. The standing template keeps a 3-line pointer.

Keep these rows reconciled with the backend side-effect code
(`backend/app/action_requests/service.py:apply_decision_side_effects` +
`_AUTO_UNBLOCK_REQUEST_TYPES`). `test_auto_decide_rows.py` pins the key set
against `REQUEST_TYPES`.
"""
from __future__ import annotations

# Preamble shown on EVERY auto-decide turn, before the type-specific row.
# (The "decide now via decide_action_request with the request_id" instruction
# is issued by the synthetic-turn builder body immediately above this, so it is
# NOT repeated here — this preamble carries only the approve-semantics.)
AUTO_DECIDE_PREAMBLE = (
    "**Approve ≠ done — but some types DO auto-fire:** `create_task` creates "
    "the task; and approving an `escalate_blocker` or `request_clarification` "
    "whose source task is `blocked` auto-promotes it to `ready` "
    "(`request_clarification` also posts your `decision_notes` as an `answer`) "
    "— so do NOT also `move_task` a task the approval already unblocked. "
    "`setup_office_secret` carries no source task, so unblock its tasks "
    "manually. For every OTHER type, approve only records the decision: you "
    "MUST take the follow-up action yourself in the SAME turn."
)

# request_type → "Default decision | what you do after deciding".
AUTO_DECIDE_ROWS: dict[str, str] = {
    "create_subtask": (
        "APPROVE if it serves the source task's brief AND fits the active "
        "scope; REJECT if it duplicates/expands scope. No auto side-effect — "
        "call `create_task` yourself with a full brief + `parent_task_id`, "
        "then `add_activity` on the source task linking it."
    ),
    "split_into_scope": (
        "APPROVE if the task is too broad AND the sub-tasks each pass the "
        "sharpness rules; else reject and add tasks to the current scope. No "
        "auto side-effect — `create_scope` → `create_task` ×N → `activate_scope`."
    ),
    "update_task": (
        "APPROVE narrow field changes (priority/reviewer/depends_on); REJECT "
        "changes that materially redirect the task (those are new tasks). No "
        "auto side-effect — call `update_task` yourself with the payload fields."
    ),
    "move_task": (
        "APPROVE a valid transition that solves a real problem; REJECT if it "
        "skips review or promotes prematurely. No auto side-effect — call "
        "`move_task` yourself with the same `new_status` + a clear `comment`."
    ),
    "escalate_blocker": (
        "If it's a workstream/logic blocker you can resolve (clarify brief, "
        "helper task, change agent) — do that, then APPROVE ('Resolved via …'). "
        "If it's credentials/infra/cost it should have routed to the user; on "
        "auto-decide that's a routing bug — REJECT naming the gap (the backend "
        "re-routes the rejection to the user inbox while the task is blocked). "
        "Approving auto-promotes the blocked source task to `ready` — do NOT "
        "also `move_task`; post any answer/helper via `add_activity`/`create_task` "
        "BEFORE approving."
    ),
    "request_clarification": (
        "If the answer is in office files / KB / a done task — APPROVE with the "
        "answer in `decision_notes` (backend posts it as an `answer` Activity "
        "AND auto-promotes the blocked source task to `ready`). If it genuinely "
        "needs the user — REJECT describing what you need (backend re-routes to "
        "the user inbox while the task is blocked)."
    ),
    "request_review_check": (
        "The reviewer answers this; rarely auto-decided. If you get one, route "
        "to the reviewer via an `add_activity` checkpoint, then APPROVE. No auto "
        "side-effect."
    ),
    "propose_artifact_handoff": (
        "APPROVE if the source task is `done` and the target is `ready`/"
        "`in_progress` and can use the file; else REJECT. No auto side-effect — "
        "`add_activity` on the target task naming the file_path from the payload."
    ),
    "create_task": (
        "Apply the Agent-Selection 3-step audit on the proposed assignee; "
        "APPROVE if it passes, else REJECT naming the better agent. **APPROVE "
        "auto-creates the task** — do NOT call `create_task` separately "
        "(double-creates)."
    ),
    "board_overview": (
        "Routed to the user inbox — you should not see this on auto-decide. If "
        "you do, the routing is buggy; REJECT with a note. No side-effect."
    ),
    "informational": (
        "Acknowledge-only. APPROVE to mark seen — no follow-up needed. Use the "
        "payload description to inform later planning."
    ),
    "setup_office_secret": (
        "The user adds the secret in Settings → Security and the backend "
        "auto-approves the row; you'll see the decision as a synthetic turn. "
        "Then `get_board` filtered to `status=blocked`, find tasks whose "
        "escalation names the secret / `blocker_class=missing_credential`, and "
        "`move_task → ready` for each (it carries NO source task, so it does "
        "not auto-unblock)."
    ),
    "propose_spec_update": (
        "NOT auto-decidable — a requirement change is the user's call "
        "(requires_user=True). Do NOT decide it yourself. When the user "
        "approves, route it to the spec_change flow: `consult_planner` "
        "(mode=specify) to draft the spec revision, get it approved, then the "
        "Planner's impact pass regenerates the traced-affected scopes/tasks. "
        "NEVER patch a task brief directly for a requirement change."
    ),
}

# Categories the router always pins to the user inbox (never auto-decided).
AUTO_DECIDE_USER_ONLY_NOTE = (
    "Categories that NEVER reach you (router pins them to the user inbox "
    "regardless of severity): `credentials`, `infrastructure`, `user_input`, "
    "`cost` — plus anything at `critical` severity. If one reaches you, it's a "
    "routing bug — reject with a note."
)


def render_auto_decide_guidance(request_type: str) -> str:
    """Preamble + the row for ``request_type`` (or a generic fallback)."""
    row = AUTO_DECIDE_ROWS.get(
        request_type,
        "Unrecognised request_type — read the payload + justification, decide "
        "on its merits, and take any follow-up action yourself.",
    )
    return (
        f"{AUTO_DECIDE_PREAMBLE}\n\n"
        f"**Policy for `{request_type}`:** {row}\n\n"
        f"{AUTO_DECIDE_USER_ONLY_NOTE}"
    )
