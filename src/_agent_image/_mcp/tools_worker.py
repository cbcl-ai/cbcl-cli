"""Worker-role MCP tool definitions (split from mcp_tool_server.py).

Same shape as ``tools_manager`` but for executor/reviewer sessions.
"""
from __future__ import annotations


# T5.1.1/T5.1.3 — board-write tools whose visibility is role-filtered at
# registration time. The base ``get_worker_tools()`` is the definition pool;
# ``get_worker_subcatalog`` carves it into three named role surfaces so the
# tool surface MATCHES each role's authority instead of relying solely on the
# runtime executor guard + description-as-refusal prose (which models follow
# less reliably than an absent tool). The runtime executor guard is RETAINED
# as defense-in-depth.
_BOARD_WRITE_TOOLS = frozenset({"create_task", "move_task", "update_task"})
# Manager-Assistant Board-Operator additions, pulled by name from the manager
# catalog so the MA gets the real (manager-grade) definitions, not worker
# stubs. ``retry_blocked_task`` = the bounce-cap recovery escape hatch (Path D);
# ``get_board`` + ``list_scopes`` = the Board Overview reads.
_MA_BOARD_OPERATOR_EXTRAS = ("retry_blocked_task", "get_board", "list_scopes")


# Pivot-1 T5: the ask-class assignee's ONE sanctioned move — served in place
# of the base pool's reviewer/Board-Operator-voiced move_task description,
# which forbids moving your OWN task (the exact use ask registration exists
# for). Voice matches the shared playbook's ask-class exception
# (_shared_agent.py, Communication section).
_ASK_MOVE_TASK_DESCRIPTION = (
    "Your task is ask-class: the answer IS the deliverable and there is NO "
    "review round. Post the answer as a comment, then move YOUR OWN task "
    "straight to done with this tool (new_status=\"done\", task_id = your "
    "task) — do NOT `update_status` to review. Any other use (other tasks, "
    "other statuses) is refused by the server."
)


def get_worker_subcatalog(
    task_mode: str, agent_name: str, task_class: str | None = None,
) -> list[dict]:
    """Return the role-appropriate worker tool surface (T5.1.1/T5.1.3).

    Three named sub-catalogs over the base ``get_worker_tools()`` pool:

    * ``manager-assistant`` (any TASK_MODE) — keeps the full board-write set
      AND gains the Board-Operator reads/recovery
      (``retry_blocked_task``/``get_board``/``list_scopes``). The triage-mode
      runtime lockout on the *current* blocked task still applies separately.
    * reviewer (``TASK_MODE == "review"``) — keeps ``move_task`` (the verdict
      surface) but loses ``create_task`` + ``update_task``.
    * executor (everything else) — loses all three board-write tools; its only
      board-write path is the ``propose_*`` family. EXCEPTION (pivot-1 T5,
      C-3): an **ask-class** executor (``task_class == "ask"``) keeps
      ``move_task`` — ask tasks skip Review, so the assignee closes its OWN
      task straight to ``done``; the runtime executor guard confines the
      registered tool to exactly that move. Absent ``task_class`` (older
      payloads) = the plain executor surface — graceful degrade.

    The Planner does NOT use this — it has its own ``get_planner_tools()`` and
    is dispatched via the ``AGENT_NAME == "planner"`` branch upstream.
    """
    base = get_worker_tools()
    if agent_name == "manager-assistant":
        from .tools_manager import get_manager_tools

        # TOOL-09: in triage mode ``update_status`` is always refused at
        # runtime (flipping the current blocked task's status would bypass the
        # bounce cap), so DON'T register it — the runtime guard stays as
        # defense-in-depth. This also keeps the triage-exception prose out of
        # the (executor-facing) description entirely.
        pool = base
        if task_mode == "triage":
            pool = [t for t in base if t["name"] != "update_status"]
        present = {t["name"] for t in pool}
        extras = [
            t
            for t in get_manager_tools()
            if t["name"] in _MA_BOARD_OPERATOR_EXTRAS and t["name"] not in present
        ]
        return pool + extras
    if task_mode == "review":
        drop = _BOARD_WRITE_TOOLS - {"move_task"}
        return [t for t in base if t["name"] not in drop]
    if task_class == "ask":
        # Ask-class executor: move_task stays registered so the assignee can
        # close its own task straight to done (no review round). The runtime
        # executor guard still refuses every OTHER move_task use. The base
        # pool's move_task description is reviewer/Board-Operator-voiced and
        # forbids the exact use this registration exists for, so it is
        # RE-VOICED here (07 review, tool-descriptions group): the ask
        # assignee must be TOLD it closes its own task — pinned by
        # test_tool_catalog_drift.test_ask_executor_move_task_is_ask_voiced.
        drop = _BOARD_WRITE_TOOLS - {"move_task"}
        out = []
        for t in base:
            if t["name"] in drop:
                continue
            if t["name"] == "move_task":
                t = {**t, "description": _ASK_MOVE_TASK_DESCRIPTION}
            out.append(t)
        return out
    return [t for t in base if t["name"] not in _BOARD_WRITE_TOOLS]


def get_worker_tools() -> list[dict]:
    """Unfiltered worker tool-definition POOL.

    This is NOT served directly — every worker session is filtered through
    ``get_worker_subcatalog(task_mode, agent_name)`` (T5.1.1/T5.1.3). Use this
    only as the source pool (tests, transform-consistency checks).
    """
    return [
        {
            "name": "update_task",
            # AIQ fix 15 (2026-07-29): the "Executors: NOT registered for
            # you" paragraph was dead prose — since T5.1.1 only the MA's
            # sub-catalog registers this tool, so no executor ever reads
            # this description.
            "description": (
                "Set task fields directly (Manager Assistant / Board "
                "Operator tool): set `reviewer` (designate who reviews), "
                "`depends_on` (wire a helper task so the backend "
                "auto-promotes the blocked task when the helper finishes), "
                "or `priority`/`labels`. Note: `assigned_agent` can NOT be "
                "cleared once a task reaches Ready "
                "(no-unassign-after-Ready invariant); a returned task stays "
                "with its original executor — reviewers resolve with "
                "`move_task`, never by unassigning."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id (e.g. 'WR-003.T01')."},
                    "reviewer": {"type": "string", "description": "Agent name to designate as the task's reviewer (e.g. 'auditor'). Board-Operator use."},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Task ids/readable_ids this task depends on. Setting this on a blocked task lets the backend auto-promote it to Ready once the dependencies reach done."},
                    "priority": {"type": "string", "enum": ["urgent", "high", "medium", "low"], "description": "Task priority."},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Cross-cutting tags."},
                    "description": {"type": "string", "description": "Updated task description."},
                    "assigned_agent": {"type": "string", "description": "Cannot be cleared after Ready (no-unassign-after-Ready). Use propose_update_task to suggest a reassignment."},
                },
                "required": ["task_id"],
            },
            "action": "update_task",
        },
        {
            "name": "update_status",
            "description": (
                "Submit YOUR task by moving its status. Allowed: "
                "in_progress -> review (work complete), in_progress -> "
                "blocked (genuine blocker — pass the structured ESCALATED "
                "comment in THIS call; do not post a separate `question` "
                "first). Calling this with new_status=review is your "
                "FINAL action — STOP IMMEDIATELY after; further tool calls "
                "in the same session are rejected. Do NOT use to move other "
                "tasks; do NOT use as a substitute for the designated "
                "reviewer's move_task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Your task's UUID or readable_id (e.g. WR-003.T01)."},
                    "new_status": {
                        "type": "string",
                        "enum": ["review", "blocked"],
                        "description": "review = work complete; blocked = cannot proceed.",
                    },
                    "comment": {
                        "type": "string",
                        "description": (
                            "Summary of submission or blocker. "
                            "REQUIRED when new_status='blocked' — use the "
                            "canonical 4-section template. The backend routes "
                            "the escalation from the ESCALATED (<class>) prefix "
                            "in THIS comment, so put the class here — ONE call, "
                            "no separate add_activity/question first:\n\n"
                            "ESCALATED (<blocker_class>): <one-sentence summary>\n\n"
                            "Original error: <verbatim error text or N/A>\n\n"
                            "What I was trying to do: <one or two sentences>\n"
                            "What I already tried: <bullets — leave blank if nothing>\n"
                            "What's needed to resume: <bullets — be concrete>\n\n"
                            "blocker_class must be one of: auth_failed, "
                            "missing_credential, permission_denied, "
                            "missing_data, ambiguous_spec, broken_dependency, "
                            "external_outage, unknown."
                        ),
                    },
                },
                "required": ["task_id", "new_status"],
            },
            "action": "task_status_update",
        },
        {
            "name": "add_activity",
            "description": (
                "Post to this task's Activity feed. Use \"checkpoint\" for "
                "concrete progress (something was produced), \"question\" "
                "when you genuinely need Manager input before continuing, "
                "\"answer\" to reply to a `question` posted on a task you "
                "are triaging or reviewing (the Board-Operator answer "
                "path — the reply lands in that task's Discussion), "
                "and \"comment\" for everything else. Reviewers post their "
                "verdict on the `move_task` call (`comment` + structured "
                "`verdict`), NOT a separate add_activity — use an add_activity "
                "\"comment\" for a verdict ONLY when escalating at the rework "
                "cap (where no `move_task` happens). Do not use as a "
                "substitute for `update_status` when you finish a task, "
                "and do not use to post `task_proposed` events directly — "
                "use the `propose_task` tool for that."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id."},
                    "event_type": {
                        "type": "string",
                        "enum": ["checkpoint", "question", "comment", "answer", "task_proposed"],
                        "description": "checkpoint = progress; question = blocker for Manager; comment = note; answer = reply to another task's question (triage/review); task_proposed = legacy (prefer propose_task).",
                    },
                    "content": {"type": "string", "description": "Activity content text."},
                    "details": {
                        "type": "object",
                        "description": (
                            "Optional structured metadata for checkpoints / "
                            "notes. You do NOT need this to block a task — the "
                            "canonical block flow is a single "
                            "`update_status(blocked, comment=\"ESCALATED "
                            "(<class>): …\")` and the backend routes on that "
                            "comment's prefix. `{\"blocker_class\": \"<enum>\"}` "
                            "here is an optional legacy carrier, not required."
                        ),
                    },
                },
                "required": ["task_id", "event_type", "content"],
            },
            "action": "add_activity",
            "transform": "add_activity",
        },
        {
            "name": "get_my_brief",
            "description": (
                "Re-read YOUR currently-assigned task — full Brief, recent "
                "Activity, registered Artifacts. Use this to refresh state "
                "after a long tool sequence or to confirm the task is still "
                "in the status you assumed. Pass the task_id from your task "
                "prompt; do NOT use this to inspect other tasks (use "
                "get_task_detail for that)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Your task's UUID (from the task prompt)."},
                },
                "required": ["task_id"],
            },
            "action": "get_task_detail",
        },
        {
            "name": "get_task_detail",
            "description": (
                "Inspect ANOTHER task on the board (not your own). Used "
                "primarily by the Manager Assistant in Board Operator mode "
                "for triage. Returns Brief + Activity + Artifacts. For your "
                "OWN task call get_my_brief — same backend, but get_my_brief "
                "documents that you should not be inspecting others' work."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id (e.g. 'WR-003.T01')."},
                },
                "required": ["task_id"],
            },
            "action": "get_task_detail",
        },
        {
            "name": "create_task",
            "description": "Create a task with a complete brief. Provide ALL brief fields for auto-Ready. Only when you are Board Operator (Manager Assistant) or are explicitly authorised to create tasks on behalf of the Manager — regular worker agents must use `propose_task` instead, which routes the request through the Action Request inbox for approval.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID"},
                    "title": {"type": "string", "description": "REQUIRED. Task title"},
                    "description": {"type": "string", "description": "Task description"},
                    "assigned_agent": {"type": "string", "description": "REQUIRED. Name of the agent that will execute this task (e.g. 'manager-assistant', 'analyst'). Must match an agent in the office roster. Never leave empty — unassigned tasks stall in Ready."},
                    "reviewer": {"type": "string", "description": "REQUIRED. Agent name for the designated reviewer. MUST be different from assigned_agent — an agent cannot review its own work."},
                    "priority": {"type": "string", "description": "urgent, high, medium, low"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "Optional label tags (e.g. ['frontend','urgent']) shown on the board card."},
                    "scope_id": {"type": "string", "description": "Scope UUID — scopes are PROGRAM MILESTONES: a milestone-scope normally holds ONE fat assignment (2-3 only on a genuine expert boundary). 2-5 related fat assignments ship as plain tasks chained with depends_on — no scope. A cohesive deliverable one agent can finish in a single session ships as ONE unscoped task — the DEFAULT for prototypes and one-sitting builds."},
                    "goal": {"type": "string", "description": "REQUIRED. The OUTCOME — what 'done' means, one sentence"},
                    "context": {"type": "string", "description": "OPTIONAL (Brief 2.0). Extra framing only when it adds signal beyond inputs; omit rather than pad"},
                    "inputs": {"type": "string", "description": "REQUIRED. The originating request VERBATIM + reference paths/URLs — never a paraphrase. 'None' only when no upstream request exists"},
                    "output_format": {"type": "string", "description": "OPTIONAL (Brief 2.0). Only when the artifact shape isn't obvious"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "REQUIRED. ≤3-5 objectively checkable items (min 1)"},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Optional + ADVISORY only — a hint shown to the worker, NOT enforced (the agent's own config is the real tool boundary). Leave empty unless you have a specific reason to suggest a subset."},
                    "required_skills": {"type": "array", "items": {"type": "string"}, "description": "Optional skill slugs the assigned agent must have for this task."},
                    "risks_and_edge_cases": {"type": "string", "description": "OPTIONAL (Brief 2.0). Pitfalls worth a warning; omit rather than 'None'"},
                    "verification_steps": {"type": "string", "description": "REQUIRED. The REVIEW — how the reviewer checks (smoke vs audit)"},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Array of readable_ids (e.g. ['WR-003.T01']) that must reach 'done' before this task can move to Ready. REQUIRED when adding a task to a scope that is already Ready/Executing with active tasks — set it to the readable_id of the last incomplete task to preserve ordering."},
                },
                # Brief 2.0 (pivot-1 T3): the four-part assignment contract —
                # see the Manager catalog's create_task for the rationale.
                "required": ["workstream_id", "title", "assigned_agent", "reviewer", "goal", "inputs", "acceptance_criteria", "verification_steps"],
            },
            "action": "create_task",
        },
        {
            "name": "move_task",
            "description": "Move a task to a new board column. Used by Board Operator for review/blocked management. Do not use to submit your OWN task for review — call `update_status` with status=\"review\" instead. Only when triaging tasks assigned to others as a designated reviewer or in Board Operator mode.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task UUID or readable_id"},
                    "new_status": {
                        "type": "string",
                        # Enum locks the worker / reviewer surface to the
                        # four states a non-executor can drive. ``review``
                        # is the executor's own ``update_status`` path
                        # (NOT this tool); ``backlog`` is not a target at
                        # all (no *→backlog edge exists — it is a
                        # source-only status); ``archived`` is a separate
                        # Manager tool. Without this enum Claude
                        # occasionally tried invalid moves and the
                        # backend rejected after a round-trip.
                        "enum": ["done", "ready", "blocked", "in_progress"],
                        "description": "Target status: done, ready, blocked, in_progress",
                    },
                    "comment": {"type": "string", "description": "Reason for the move. For a review verdict, put the full summary-first Markdown verdict here — it becomes the task Discussion entry."},
                    "verdict": {
                        "type": "object",
                        "description": "Optional STRUCTURED review verdict, rendered as a card in the task Discussion. Provide it alongside `comment` when approving/returning a reviewed task so the user gets an at-a-glance pass/fail breakdown.",
                        "properties": {
                            "overall": {"type": "string", "enum": ["pass", "fail", "conditional"], "description": "Overall verdict"},
                            "rationale": {"type": "string", "description": "One-sentence rationale"},
                            "criteria": {
                                "type": "array",
                                "description": "One entry per acceptance criterion",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "description": "Criterion name / short label"},
                                        "status": {"type": "string", "enum": ["pass", "fail", "partial"], "description": "Per-criterion status"},
                                        "evidence": {"type": "string", "description": "Terse one-line evidence"},
                                    },
                                    "required": ["name", "status"],
                                },
                            },
                            "required_fixes": {"type": "array", "items": {"type": "string"}, "description": "Specific actionable fixes (FAIL only)"},
                        },
                        "required": ["overall"],
                    },
                },
                "required": ["task_id", "new_status"],
            },
            "action": "move_task",
            "transform": "move_task",
        },
        {
            "name": "propose_task",
            "description": (
                "Propose a new task to the Manager. LEGACY fallback — use "
                "`propose_subtask` (same-scope follow-up) or "
                "`propose_split_into_scope` (new body of work) unless "
                "neither fits; the typed tools carry structure the Manager "
                "can act on. This is bridged into the Action Request inbox "
                "automatically. Do not use to create a task directly; "
                "this only sends a proposal that the Manager must approve."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Current task UUID"},
                    "content": {"type": "string", "description": "Proposed task title and description"},
                },
                "required": ["task_id", "content"],
            },
            "action": "add_activity",
            "transform": "propose_task",
        },
        # ── Action Request family (Phase A of Worker→Manager channel) ──
        # All of these are sugar over the generic ``propose_action`` —
        # they exist as separate tools so the Manager / Worker prompt
        # surfaces are friendlier than asking workers to remember enum
        # values. Backend dispatcher accepts ``propose_action`` with a
        # typed ``request_type`` + ``payload``; the transforms below
        # do the rewrite. The ``source_task_id`` is auto-injected from
        # ``TASK_ID`` env so workers don't have to pass it.
        {
            "name": "propose_subtask",
            "description": (
                "Propose a follow-up SUBTASK that should run inside the "
                "same Scope as your current task. Only when finishing your "
                "task naturally produces a clearly-scoped next step. Do not "
                "use for unrelated follow-up work that needs its own "
                "Scope — use `propose_split_into_scope` for that. Lands in "
                "the Inbox as request_type=create_subtask."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Concise task title (one sentence)."},
                    "brief_hints": {"type": "object", "description": "Optional partial Brief fields (goal, context, inputs, etc.) the Manager can use as a starting point."},
                    "justification": {"type": "string", "description": "Why this subtask is needed (one or two sentences)."},
                },
                "required": ["title", "justification"],
            },
            "action": "propose_action",
            "transform": "propose_subtask",
        },
        {
            "name": "propose_split_into_scope",
            "description": (
                "Propose splitting a body of work into a NEW Scope (multiple "
                "related tasks). Use when finishing your task reveals that "
                "follow-up requires real planning, not a single subtask. "
                "Lands in the Inbox as request_type=split_into_scope and "
                "always escalates to the Manager (never auto-handled)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope_short_key": {"type": "string", "description": "1-2 word UI label, e.g. 'Auth', 'Sourcing'."},
                    "scope_name": {"type": "string", "description": "Full scope name, e.g. 'User auth migration'."},
                    "tasks": {
                        "type": "array",
                        "description": "Array of {title, brief_hints?} entries describing each task in the new scope.",
                        "items": {"type": "object"},
                    },
                    "justification": {"type": "string", "description": "Why this body of work needs its own Scope."},
                },
                "required": ["scope_short_key", "scope_name", "tasks", "justification"],
            },
            "action": "propose_action",
            "transform": "propose_split_into_scope",
        },
        {
            "name": "propose_update_task",
            "description": (
                "Propose updating fields on an existing task (priority, "
                "labels, description, assigned_agent, reviewer, depends_on). "
                "Lands in the Inbox as request_type=update_task and "
                "routes to the Manager's auto-decide queue (workstream "
                "category); the Manager approves or rejects and the "
                "decision lands on the request. Do not "
                "use to change a task's status or workstream — those are "
                "out of scope for this tool."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Target task UUID or readable_id."},
                    "changes": {"type": "object", "description": "Whitelisted keys: priority, labels, description, assigned_agent, reviewer, depends_on."},
                    "justification": {"type": "string", "description": "Why the change is needed."},
                },
                "required": ["task_id", "changes", "justification"],
            },
            "action": "propose_action",
            "transform": "propose_update_task",
        },
        {
            "name": "escalate_blocker",
            "description": (
                "Tell the office you are blocked and cannot proceed. Use "
                "ONLY when posting a `question` to Activity isn't enough "
                "(e.g. you need a scope decision, not a clarification). "
                "Lands in the Inbox as request_type=escalate_blocker; the "
                "REQUIRED ``blocker_class`` field drives routing: credential "
                "classes (auth_failed / missing_credential / "
                "permission_denied) and external_outage surface to the "
                "USER's Inbox, the workstream classes (missing_data / "
                "ambiguous_spec / broken_dependency / unknown) go to the "
                "Manager's auto-decide queue. No `category`/`severity` arg "
                "exists. See the ESCALATED template in your playbook "
                "(CLAUDE.md, Communication section)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "blocker_summary": {"type": "string", "description": "One-sentence description of what's blocking you."},
                    "blocker_class": {
                        "type": "string",
                        "enum": [
                            "auth_failed",
                            "missing_credential",
                            "permission_denied",
                            "missing_data",
                            "ambiguous_spec",
                            "broken_dependency",
                            "external_outage",
                            "unknown",
                        ],
                        "description": (
                            "REQUIRED. Categorises the failure so the "
                            "Manager Assistant can route the escalation. "
                            "Pick the most specific match: "
                            "auth_failed (token/OAuth rejected), "
                            "missing_credential (Office Secret not set), "
                            "permission_denied (agent lacks access), "
                            "missing_data (required input absent), "
                            "ambiguous_spec (brief contradicts itself), "
                            "broken_dependency (upstream task/artifact "
                            "missing), external_outage (third-party "
                            "API down), unknown (none of the above)."
                        ),
                    },
                    "suggested_unblock": {"type": "string", "description": "Optional: what the Manager could do to unblock."},
                    "justification": {"type": "string", "description": "Detail / context the Manager needs to decide."},
                    "rework_cap": {
                        "type": "boolean",
                        "description": (
                            "Set true ONLY when you are the designated REVIEWER "
                            "escalating because a task hit the rework cap (2 "
                            "failed rework cycles). Forces the decision to the "
                            "USER inbox (a human judgment), not Manager "
                            "auto-decide. Leave false/unset for normal blockers."
                        ),
                    },
                },
                "required": ["blocker_summary", "blocker_class", "justification"],
            },
            "action": "propose_action",
            "transform": "escalate_blocker",
        },
        {
            "name": "request_clarification",
            "description": (
                "Ask a question that needs a real answer, not just a comment "
                "on Activity. Only when the brief is ambiguous and you can't "
                "make progress without input. Manager Assistant tries to "
                "answer from CLAUDE.md / board context first; truly "
                "ambiguous questions escalate to the Manager. Do not use "
                "for general comments or status updates — post those to "
                "Activity instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The exact question (precise, single-purpose)."},
                    "justification": {"type": "string", "description": "Why this matters / what you'll do once answered."},
                },
                "required": ["question", "justification"],
            },
            "action": "propose_action",
            "transform": "request_clarification",
        },
        {
            "name": "request_review_check",
            "description": (
                "Ask the Manager (or designated reviewer) to confirm whether "
                "a specific acceptance criterion is met BEFORE you submit. "
                "Only when one criterion is genuinely a judgement-call you "
                "cannot resolve yourself. Do not use for clear yes/no "
                "criteria you can verify mechanically — the reviewer will "
                "check those at submission time. Lands in the Inbox as "
                "request_type=request_review_check."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "criterion_index": {"type": "integer", "description": "Zero-indexed criterion from your brief (optional)."},
                    "justification": {"type": "string", "description": "What you produced and why you're unsure it satisfies the criterion."},
                },
                "required": ["justification"],
            },
            "action": "propose_action",
            "transform": "request_review_check",
        },
        {
            "name": "propose_artifact_handoff",
            "description": (
                "Tell the Manager that a specific artifact you produced is "
                "needed by another (already-existing) task. Lands in the "
                "Inbox as request_type=propose_artifact_handoff. Only when "
                "the target task already exists — do not use to propose "
                "new work; for that use `propose_subtask` or "
                "`propose_split_into_scope`."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_task_id": {"type": "string", "description": "Task UUID/readable_id that should consume the artifact."},
                    "file_path": {"type": "string", "description": "Workspace-relative path to the artifact file."},
                    "justification": {"type": "string", "description": "Why this task needs that file."},
                },
                "required": ["target_task_id", "file_path", "justification"],
            },
            "action": "propose_action",
            "transform": "propose_artifact_handoff",
        },
        {
            "name": "propose_spec_update",
            "description": (
                "Propose a REQUIREMENT-level change to the workstream spec "
                "when you discover mid-task that a requirement is wrong, "
                "missing, or conflicts with reality. Lands in the Inbox as "
                "request_type=propose_spec_update (always user-decided — spec "
                "changes are the user's call). Do NOT use for task-level "
                "tweaks (use propose_update_task) — only for changes to WHAT "
                "the work must do. On approval the Manager routes it to the "
                "spec_change flow (Planner drafts the revision, you don't)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "proposed_text": {"type": "string", "description": "The requirement change you propose, in plain language (a new/edited REQ)."},
                    "rationale": {"type": "string", "description": "Why the current spec is wrong/insufficient — what you found."},
                    "target": {"type": "string", "description": "Which REQ id or section this touches (e.g. 'REQ-3'), if known."},
                    "spec_id": {"type": "string", "description": "Spec UUID, if known (optional — the Manager resolves it from your task)."},
                },
                "required": ["proposed_text", "rationale"],
            },
            "action": "propose_action",
            "transform": "propose_spec_update",
        },
        {
            "name": "schedule_script",
            "description": "Create or update a cron schedule for a script. Standard 5-field cron (min hour dom mon dow) or aliases (@hourly @daily @weekly @monthly). The schedule fires automatically; each firing produces a ScriptExecution row visible in the script's history. Use this to automate recurring tasks instead of calling execute_script repeatedly.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Slug of the script to schedule."},
                    "name": {"type": "string", "description": "Short name for this schedule (unique per script). Example: 'morning-refresh', 'hourly-sync'."},
                    "cron_expression": {"type": "string", "description": "Cron expression, e.g. '0 9 * * 1-5' (9am weekdays) or '@daily'."},
                    "description": {"type": "string", "description": "Optional. What this schedule does / why it exists."},
                    "variable_overrides": {"type": "object", "description": "Per-run variable overrides as a dict. Keys must match the script's variable_schema."},
                    "is_active": {"type": "boolean", "description": "Default true. Set false to create disabled."},
                },
                "required": ["script_name", "name", "cron_expression"],
            },
            "action": "schedule_script",
        },
        {
            "name": "list_script_crons",
            "description": "List cron schedules, optionally filtered by script_name. Only when you need to inspect existing schedules — do not use as a pre-check before `schedule_script`, which is itself idempotent on (script_name, name).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Optional filter."},
                },
            },
            "action": "list_script_crons",
        },
        {
            "name": "update_script_cron",
            "description": "Update a cron schedule (change expression, overrides, active state). Only when modifying an existing schedule — do not use to create a new one; for that, use `schedule_script` (idempotent by name).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cron_id": {"type": "string", "description": "UUID of the cron schedule."},
                    "name": {"type": "string", "description": "New short name for this schedule (e.g. 'morning-sync')."},
                    "description": {"type": "string", "description": "Optional longer description of why this schedule exists."},
                    "cron_expression": {"type": "string", "description": "New crontab expression (e.g. '0 9 * * *') or shortcut ('@daily')."},
                    "variable_overrides": {"type": "object", "description": "Per-run variable values, merged on top of variables.json at execution time."},
                    "is_active": {"type": "boolean", "description": "Toggle the schedule on/off without deleting it."},
                },
                "required": ["cron_id"],
            },
            "action": "update_script_cron",
        },
        {
            "name": "delete_script_cron",
            "description": "Delete a cron schedule permanently. Only when the schedule should be removed entirely. Do not use to temporarily pause a schedule — call `update_script_cron` with is_active=false instead, which preserves the row.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cron_id": {"type": "string", "description": "UUID of the cron schedule to remove."},
                },
                "required": ["cron_id"],
            },
            "action": "delete_script_cron",
        },
        {
            "name": "list_script_templates",
            "description": (
                "List the Cubicle-curated marketplace catalog of "
                "starter scripts. "
                "Returns summary metadata per template: id, name, "
                "display_name, description, category, tags, "
                "recommended_office_secrets, variable_schema. Use this "
                "in your research phase before deciding to write a new "
                "script from scratch — a template is a faster start "
                "when it matches ≥80% of the requirements. Read-only."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "action": "list_script_templates",
        },
        {
            "name": "get_script_template",
            "description": (
                "Fetch a marketplace template's full payload — "
                "summary + default_files (script.yaml, main.py, "
                "lib/, requirements.txt, README.md). Use after "
                "list_script_templates picked a candidate, to "
                "preview the code before installing. Read-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "Template id (e.g. cubicle-hello-world).",
                    },
                },
                "required": ["template_id"],
            },
            "action": "get_script_template",
        },
        {
            "name": "install_script_from_template",
            "description": (
                "Install a marketplace template into this office "
                "as a new script. Creates a Script row stamped "
                "source_kind='template' + source_template_id, and "
                "lays the template's file map under "
                "/workspace/.scripts/{name}/. Variable VALUES / "
                "bindings are NOT installed — the user configures "
                "them via the Variables UI after install. Prefer "
                "this over `register_script` from scratch when a "
                "matching template exists in the catalog."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "Template id to install.",
                    },
                    "new_name": {
                        "type": "string",
                        "description": (
                            "Override slug (optional — defaults to "
                            "the template's own slug)."
                        ),
                    },
                    "new_display_name": {
                        "type": "string",
                        "description": (
                            "Override display name (optional — "
                            "defaults to the template's display name)."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Source task UUID (audit trail).",
                    },
                },
                "required": ["template_id"],
            },
            "action": "install_script_from_template",
        },
        {
            "name": "clone_script",
            "description": (
                "Duplicate an existing office script under a new name. "
                "Use this in your research phase when an existing script "
                "is a ~70%+ match for the new task — clone it, then Edit "
                "the cloned files to adapt. The clone copies every text "
                "file in the source's workspace dir (script.yaml, main.py, "
                "lib/*.py, requirements.txt, README.md, custom modules) "
                "PLUS the source's variable_schema declarations. It does "
                "NOT copy variable VALUES (variables.json), secrets, "
                "execution history, or cron schedules — the user "
                "reconfigures variables in the UI after the clone lands. "
                "Prefer this over `register_script` from scratch whenever "
                "a near-fit exists."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": (
                            "Slug of the script to clone (e.g. "
                            "'linkedin-sourcing'). Either source_name OR "
                            "source_script_id must be provided."
                        ),
                    },
                    "source_script_id": {
                        "type": "string",
                        "description": (
                            "UUID of the script to clone. Use this when "
                            "you already have the id from list_scripts; "
                            "otherwise pass source_name."
                        ),
                    },
                    "new_name": {
                        "type": "string",
                        "description": (
                            "Slug for the cloned script (lowercase, "
                            "hyphens). Optional — when omitted the "
                            "platform slugifies new_display_name."
                        ),
                    },
                    "new_display_name": {
                        "type": "string",
                        "description": "Human-readable name for the cloned script.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Source task UUID (audit trail).",
                    },
                },
                "required": ["new_display_name"],
            },
            "action": "clone_script",
        },
        {
            "name": "register_script",
            "description": (
                "Register a NEW script OR update the METADATA of an "
                "existing one. Behaviour by case:\n\n"
                "* NEW script (name not yet in this office): platform "
                "creates a mini-project at /workspace/.scripts/{name}/ "
                "with boilerplate (script.yaml, main.py, lib/, "
                "lib/cubicle/, requirements.txt, README.md). Edit those "
                "files via the Edit tool — NEVER Write them yourself "
                "or you'll clobber the boilerplate.\n\n"
                "* EXISTING script (same name already registered): "
                "ONLY the metadata fields (display_name, description, "
                "variable_schema) are updated. The on-disk source "
                "files are NEVER touched by register_script — your "
                "previous edits to main.py / script.yaml / "
                "requirements.txt / lib/ are SAFE. Use this freely "
                "to update variable_schema or description without "
                "fear of overwriting source.\n\n"
                "If the existing row has bootstrap_status != "
                "'complete', the response carries "
                "``bootstrap_needs_retry: true``. Resetting source "
                "to boilerplate is a DISTINCT operation behind an "
                "explicit retry path — register_script will not "
                "trigger it.\n\n"
                "Idempotent by (office, name)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Script slug (lowercase, hyphens). Same name on repeat calls updates METADATA ONLY — source files are not touched."},
                    "display_name": {"type": "string", "description": "Human-readable name."},
                    "description": {"type": "string", "description": "Short summary of what the script does."},
                    "variable_schema": {"type": "array", "description": "Variable definitions: [{name, type, is_secret, description}]. Should mirror the variables declared in script.yaml."},
                    "created_by": {"type": "string", "description": "Agent name that authored the script (e.g. 'automation-script-developer')."},
                    "task_id": {"type": "string", "description": "Source task UUID (only on first registration)."},
                },
                "required": ["name", "display_name"],
            },
            "action": "register_script",
        },
        {
            "name": "bind_script_variable",
            "description": (
                "Bind ONE declared script variable to an Office Secret "
                "so the Runner injects the secret's value automatically "
                "at execute time. Use this RIGHT AFTER ``register_script`` "
                "when the script declares an ``is_secret: true`` variable "
                "that matches an existing Office Secret name — wires up "
                "the credential without bouncing the user.\n\n"
                "Workflow: ``list_office_secrets`` → ``register_script`` "
                "(name secret variables after existing Office Secrets) → "
                "``bind_script_variable`` per ``is_secret: true`` variable "
                "(idempotent).\n\n"
                "Errors:\n"
                "  * 400 if the variable isn't declared in the script's "
                "manifest (a binding for a non-existent variable would "
                "silently shadow a later manifest edit).\n"
                "  * 400 if the office secret doesn't exist — missing "
                "secret -> escalate_blocker(blocker_class=missing_credential), "
                "then retry this tool.\n\n"
                "ONLY for ``office_secret`` bindings — literal secret "
                "VALUES never reach the AI by policy; those still flow "
                "through the user's chat-WS path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {
                        "type": "string",
                        "description": "Script slug name (the folder under .scripts/).",
                    },
                    "variable_name": {
                        "type": "string",
                        "description": "The variable's name as declared in script.yaml (ALL_CAPS env-style).",
                    },
                    "office_secret_name": {
                        "type": "string",
                        "description": "Name of the Office Secret to bind to (must already exist; check via list_office_secrets).",
                    },
                },
                "required": [
                    "script_name", "variable_name", "office_secret_name",
                ],
            },
            "action": "bind_script_variable",
        },
        {
            "name": "execute_script",
            "description": (
                "Run a registered script in the BACKGROUND. **Fire-and-"
                "forget — treat this as your session's LAST act (hard "
                "stop by instruction; the Automation Script Developer's "
                "two-run test protocol is the one sanctioned exception).** "
                "Returns "
                "an ``execution_id`` and the script keeps running "
                "independently of your worker process.\n\n"
                "Lifecycle:\n"
                "* Backend creates a 'running' row in Execution History "
                "the moment the spawn lands — user sees it live in the UI.\n"
                "* On completion (success OR failure), the SAME row "
                "updates to ``status=completed|failed`` with "
                "``exit_code``, ``duration_seconds``, log captured to "
                "``executions/{id}/log.txt``.\n"
                "* If the script calls ``cubicle.notify_manager(...)``, "
                "the Manager (NOT you) receives it as a chat message + "
                "the Manager Notifications popup populates.\n\n"
                "Do NOT poll ``get_script_status`` in a tight loop — "
                "long scripts complete OUT-OF-BAND and the Manager is "
                "notified automatically. Do NOT use as a substitute for "
                "the ``Bash`` tool for ad-hoc commands — scripts must "
                "be declared via ``register_script`` first.\n\n"
                "Preconditions: script registered AND "
                "``bootstrap_status='complete'`` (a freshly-registered "
                "script with ``bootstrap_needs_retry: true`` will "
                "refuse). Office secrets referenced by the script's "
                "manifest must already exist in the office store — "
                "missing secrets surface as a ``setup_office_secret`` "
                "action_request in the user's Inbox automatically."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Script slug name (from register_script)."},
                    "variable_overrides": {"type": "object", "description": "Optional per-run variable overrides. Skipped variables fall back to the binding stored in variables.json / .secrets.json / Office Secrets."},
                },
                "required": ["script_name"],
            },
            "action": "script_execute",
            "local": True,
        },
        {
            "name": "get_script_status",
            "description": (
                "Check the status of a specific script execution. "
                "**Rarely the right tool.** Use ONLY when the user "
                "explicitly asks you to wait on a specific execution "
                "OR when you need exit_code / duration for an immediate "
                "report. The Manager handles completion notifications "
                "automatically — do NOT poll this in a loop after "
                "``execute_script`` returns.\n\n"
                "Returns: ``status`` (running / completed / failed), "
                "``exit_code`` (if terminal), ``duration_seconds``, "
                "last 20 lines of log, last ``.progress.json`` snapshot "
                "if the script writes one."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Registered script slug."},
                    "execution_id": {"type": "string", "description": "The execution_id returned by a prior execute_script call."},
                },
                "required": ["script_name", "execution_id"],
            },
            "action": "script_get_status",
            "local": True,
        },
        {
            "name": "list_scripts",
            "description": (
                "List every script registered in this office. Returns a "
                "summary for each (name, display_name, entry_point, "
                "variable_count, has_manifest, source_kind, "
                "source_template_id, cloned_from_script_id, category, "
                "tags). Use this in the research phase (alongside "
                "list_script_templates) to find clone candidates — a "
                "near-fit existing script can be duplicated via "
                "clone_script instead of writing a new one. Also use "
                "when AUDITING a script task to confirm the DB row "
                "exists (a deliverable on disk without a DB row is not "
                "a valid script)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "action": "list_scripts",
        },
        {
            "name": "list_office_secrets",
            "description": (
                "List the office's SHARED secrets (GitLab-style — set once "
                "in Settings → Security, reusable by any script in the "
                "office). Returns ONLY metadata: name, description, "
                "fingerprint, timestamps. The actual VALUE is never "
                "returned — it lives on the user's machine and the script "
                "subprocess receives it only via env injection at "
                "``docker exec`` time. Use this BEFORE writing a new "
                "script that needs credentials: declare the variable as "
                "``is_secret: true`` in the manifest; when a matching "
                "Office Secret EXISTS, bind it yourself via "
                "``bind_script_variable`` (no user click needed). "
                "Missing secret -> "
                "escalate_blocker(blocker_class=missing_credential) — "
                "the user adds it in Settings → Security, then YOU bind "
                "it. Do NOT try to set or rotate "
                "the value yourself — secrets are user-only by policy."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "action": "list_office_secrets",
        },
        {
            "name": "list_office_secret_usage",
            "description": (
                "For every office secret, return which scripts already "
                "reference it (via a variable binding or — for legacy "
                "scripts — a manifest ``from_office_secret`` field). "
                "Pair with ``list_office_secrets``: that lists what "
                "EXISTS, this lists what's already CONNECTED. Use to "
                "answer 'is OPENAI_API_KEY already wired up?' before "
                "deciding to recommend it in a new script's variable "
                "description — and to warn the user 'updating this "
                "secret affects N scripts' before asking them to "
                "rotate it. Read-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "action": "list_office_secret_usage",
        },
        {
            "name": "get_script",
            "description": (
                "Get the full details for a single registered script: name, "
                "display_name, entry_point, variable_schema, cached manifest. "
                "Use this to verify an Automation Script Developer's "
                "delivery — compare the declared variables in script.yaml "
                "against the task brief."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "The script's slug name (folder name)."},
                },
                "required": ["script_name"],
            },
            "action": "get_script",
        },
        {
            "name": "list_script_executions",
            "description": (
                "List recent executions of a script (newest first). Use this "
                "when auditing to verify the Test Evidence block in a "
                "completion checkpoint matches real DB rows — look for at "
                "least one ``status='completed'`` row with a 0 exit_code."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "script_name": {"type": "string", "description": "Registered script slug to list executions for."},
                    "limit": {"type": "integer", "description": "Max rows returned (default 20, max 100)."},
                },
                "required": ["script_name"],
            },
            "action": "list_script_executions",
        },
        {
            "name": "search_kb",
            "description": (
                "Full-text search over the Knowledge Base — the "
                "HUMAN-CURATED reference LIBRARY (specs, runbooks, price "
                "lists, playbooks humans filed). NOT a default research "
                "step: your Brief plus workstream memory (the injected "
                "index + `recall`) are primary. Search the KB ONLY when "
                "your Brief's Assigned references cite KB documents, the "
                "user asked for it, or you can name the specific gap a "
                "reference would fill. Returns hit snippets + document "
                "IDs (limit default 5); `get_kb_document` fetches full "
                "content. Not for the office Files index (`list_files`) "
                "or workspace source code (`Grep` / `Glob`)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tag labels to AND-filter results by."},
                    "limit": {"type": "integer", "description": "Max results (default 5)."},
                },
                "required": ["query"],
            },
            "action": "kb_search",
        },
        {
            "name": "get_kb_document",
            "description": (
                "Fetch the body of ONE Knowledge Base document by ID. "
                "Use ONLY when your Brief's Assigned references name the "
                "document, or AFTER `search_kb` (itself an "
                "explicit-trigger read) returned a relevant candidate. "
                "Do not call without a document_id — there is no 'browse "
                "all documents' mode, and the KB is reference material, "
                "not your working context (the Brief + workstream memory "
                "are)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "KB document UUID returned by search_kb."},
                },
                "required": ["document_id"],
            },
            "action": "kb_get_document",
        },
        {
            "name": "save_file",
            "description": (
                "Register a CONTRACTED deliverable — a file named in the "
                "Brief's Output Format that the reviewer will open to "
                "decide PASS/FAIL. NOT for every file you touched. If "
                "your task is a code change spanning many source files, "
                "register ONE change-summary markdown ONLY when the "
                "Output Format names one (files touched, rationale, "
                "test evidence) — NOT each edited .py/.ts; when it "
                "names no document, register nothing (the code change "
                "itself is the deliverable). "
                "Source edits live in git; only contracted outputs go "
                "through save_file. Workflow: (1) use the Write tool to "
                "create the file at the per-workstream output path your "
                "task prompt names (typically "
                "/workspace/outputs/{workstream_short_code}/[{scope_readable_id}/]<name>.md), "
                "then (2) call save_file with that exact file_path. "
                "Idempotent — same path on a repeat call reuses the existing "
                "artifact row. Auto-attaches to your current task, so do "
                "NOT also call attach_to_task for your own files."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Human-readable title shown in the office Files page."},
                    "file_path": {"type": "string", "description": "Absolute path where you wrote the file. Must already exist on disk."},
                    "file_type": {"type": "string", "description": "Optional content hint: markdown, text, json, csv."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags for later discovery via list_files."},
                },
                "required": ["title", "file_path"],
            },
            "action": "office_save_file",
        },
        {
            "name": "list_files",
            "description": (
                "List durable deliverables saved in the office Files "
                "index (newest first). Use BEFORE delegating new work to "
                "check whether a similar deliverable already exists (e.g. "
                "an Analyst report from last week), and to find input "
                "files to reference in a new Brief. Filters: `tags` "
                "(AND-match — only files carrying EVERY listed tag), "
                "`source_agent` (exact agent name), `limit`. Do not use "
                "to read raw file content — `get_file` returns the "
                "file_path; Read the file at that path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "AND-filter: return only files carrying EVERY tag in this list."},
                    "source_agent": {"type": "string", "description": "Filter to files written by this exact agent name (e.g. 'analyst')."},
                    "limit": {"type": "integer", "description": "Max rows returned (default 20, hard cap 100 — pass limit explicitly when you need more than the first 20)."},
                },
            },
            "action": "office_list_files",
        },
        {
            "name": "get_file",
            "description": "Get an office file's metadata (title, file_path, tags). Pair with the Read tool on the returned file_path to read actual content.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File UUID returned by list_files or save_file."},
                },
                "required": ["file_id"],
            },
            "action": "office_get_file",
        },
        {
            "name": "attach_to_task",
            "description": (
                "Attach an office file (already created via save_file) to a "
                "DIFFERENT task as an artifact. Use to link a deliverable "
                "from a prior task as input to your current task. Do not "
                "use for your own deliverables — save_file auto-attaches "
                "to your current task, so attach_to_task is unnecessary "
                "in that case."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Target task UUID or readable_id."},
                    "file_id": {"type": "string", "description": "Office file UUID returned by save_file or list_files."},
                },
                "required": ["task_id", "file_id"],
            },
            "action": "office_attach_to_task",
        },
        # ── Office MEMORY read (office-memory v1, T3.1). Memory is the
        # distilled durable record store (workstream + office levels) —
        # NOT the KB (the human-curated reference library). Scope is
        # derived SERVER-side from the calling task (the transform
        # injects TASK_ID; no scope parameter exists on the wire).
        # Served in every worker sub-catalog; the write verb (remember)
        # is Manager-catalog-only.
        {
            "name": "recall",
            "description": (
                "Search office MEMORY — the distilled durable records "
                "(decisions, preferences, facts, how-tos, lessons, task "
                "summaries) of YOUR task's workstream plus the office "
                "level. Your task prompt already carries a memory index "
                "(titles only); to expand one, SEARCH for it first — "
                "results carry slugs — then pass a result's `slug` for "
                "the full body. Scope is derived from "
                "your task server-side — there is no scope parameter. "
                "WHEN NOT TO USE: not the Knowledge Base (`search_kb` is "
                "the human-curated reference library, read on explicit "
                "triggers only), not the office Files index "
                "(`list_files`), and never a substitute for the Brief — "
                "the Brief stays authoritative."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Free-text search over record titles + "
                            "bodies. Omit to list the newest records."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": [
                            "task_summary", "decision", "preference",
                            "fact", "how_to", "lesson",
                        ],
                        "description": "Optional filter to ONE record kind.",
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "Fetch ONE record FULL-BODY by its slug "
                            "(from a prior recall result). Overrides "
                            "query/kind."
                        ),
                    },
                    "include_office": {
                        "type": "boolean",
                        "description": (
                            "Default true — include office-level records "
                            "beside the workstream's."
                        ),
                    },
                },
            },
            "action": "memory_recall",
        },
        # ── Collection READS (Flow Studio FS-P3.T3 — worker research
        # surface). Ungated backend-side (reads); the write tools live
        # only in the Data Curator / Flow Architect catalogs. Worker
        # pool 39→41 (41→42 with the office-memory recall above).
        {
            "name": "get_collection",
            "description": (
                "Read ONE office collection's schema: ordered field "
                "definitions (type, options, ref_to, required, help), "
                "schema_revision, and row count. Collections are the "
                "office's shared data tables (a services catalog, rate "
                "cards, deal manifests) that flows and briefs may "
                "reference. Use to understand a collection's shape "
                "before reading its rows with query_rows. READ-ONLY for "
                "you — schema/row changes are the Data Curator's "
                "consult surface; if your task needs one, note it in a "
                "checkpoint or propose it, never work around it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection": {
                        "type": "string",
                        "description": "Collection slug name or UUID.",
                    },
                },
                "required": ["collection"],
            },
            "action": "get_collection",
        },
        {
            "name": "query_rows",
            "description": (
                "Read rows from an office collection (the office-local "
                "datastore on the user's machine — a live proxy read "
                "that errors honestly when the office daemon is "
                "offline). Supports free-text `search`, exact-match AND "
                "`filter`, and limit/offset paging. Use when your task's "
                "inputs reference collection data (look up a service's "
                "parameters, pull a rate). Not a KB search (use "
                "search_kb for documents) and not a file read (use "
                "list_files/Read). READ-ONLY for you — row writes are "
                "the Data Curator's surface."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection": {
                        "type": "string",
                        "description": "Collection slug name or UUID.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Free-text search across row values.",
                    },
                    "filter": {
                        "type": "object",
                        "description": (
                            "Exact-match field filter, AND-combined: "
                            "{field: value}."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows (1-200, default 50).",
                    },
                    "offset": {"type": "integer", "description": "Paging offset."},
                },
                "required": ["collection"],
            },
            "action": "query_rows",
        },
    ]

