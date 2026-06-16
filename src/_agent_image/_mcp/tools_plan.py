"""Shared Execution-Plan MCP tool definitions (Planner + Manager).

The Manager needs the plan READS — to review the Planner's roadmap and a
scope's skeleton during the two-pass authoring flow (the Manager playbook
explicitly tells it to call ``get_execution_plan`` to review the skeleton) —
plus ``complete_scope_verification`` to close a scope's verification (incl.
the stuck/escalated case where the Planner couldn't close it itself).

The Planner needs all of the above PLUS the plan WRITES (it authors the
roadmap + per-scope execution plans).

These dicts live here, with NO imports of the role modules, so both
``tools_manager`` and ``tools_planner`` can pull the right subset without a
circular import (``tools_planner`` already imports ``tools_manager``).
"""
from __future__ import annotations


UPDATE_WORKSTREAM_PLAN: dict = {
    "name": "update_workstream_plan",
    "description": (
        "Write/replace the WORKSTREAM ROADMAP (the master execution "
        "plan): the ordered list of intended scopes for the whole body "
        "of work. This is the checklist that prevents a scope from being "
        "forgotten. Bumps the revision each call. Use in 'roadmap' mode."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID."},
            "plan": {
                "type": "object",
                "description": (
                    "REQUIRED. {summary: str, planned_scopes: "
                    "[{key, title, goal, order, depends_on:[key], "
                    "status: planned|in_progress|done|dropped, "
                    "scope_id?, notes}], open_questions: [str]}"
                ),
            },
        },
        "required": ["workstream_id", "plan"],
    },
    "action": "update_workstream_plan",
}

GET_WORKSTREAM_PLAN: dict = {
    "name": "get_workstream_plan",
    "description": "Read the current workstream roadmap (execution plan).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "workstream_id": {"type": "string", "description": "REQUIRED. Workstream UUID."},
        },
        "required": ["workstream_id"],
    },
    "action": "get_workstream_plan",
}

UPDATE_EXECUTION_PLAN: dict = {
    "name": "update_execution_plan",
    "description": (
        "Write/replace a SCOPE's structured execution plan (research, "
        "component review, prior-scope learnings, task breakdown, risks, "
        "chips, verification). Use in 'scope_plan'/'research' modes (and in "
        "'materialize' for a small scope sent straight to authoring). "
        "Bumps the revision each call."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
            "plan": {
                "type": "object",
                "description": (
                    "REQUIRED. {summary, research_summary, "
                    "component_review, prior_scope_learnings, "
                    "task_breakdown: [{title, intent, assigned_agent, "
                    "depends_on}], risks: [str], chips: [{label, done}]}"
                ),
            },
        },
        "required": ["scope_id", "plan"],
    },
    "action": "update_execution_plan",
}

GET_EXECUTION_PLAN: dict = {
    "name": "get_execution_plan",
    "description": (
        "Read a scope's structured execution plan. The Manager uses this "
        "to REVIEW the Planner's skeleton (task_breakdown) before asking "
        "the Planner to materialize it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
        },
        "required": ["scope_id"],
    },
    "action": "get_execution_plan",
}

COMPLETE_SCOPE_VERIFICATION: dict = {
    "name": "complete_scope_verification",
    "description": (
        "Resolve a scope that is in the 'verifying' state. passed=true → the "
        "scope goes 'done' and the next scope can be created. passed=false → "
        "create the rework task(s) FIRST, then call this; the scope returns "
        "to 'executing' and the rework dispatches. The Planner normally calls "
        "this after it verifies; the MANAGER calls it to close a scope whose "
        "Planner verdict is already known (e.g. the Planner verified PASS but "
        "couldn't close it, or a backend escalation handed it back)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
            "passed": {"type": "boolean", "description": "REQUIRED. True if the scope's deliverables meet its plan + acceptance criteria."},
            "notes": {"type": "string", "description": "Evidence summary (pass) or what's missing + rework created (fail)."},
        },
        "required": ["scope_id", "passed"],
    },
    "action": "complete_scope_verification",
}


UPDATE_SPEC: dict = {
    "name": "update_spec",
    "description": (
        "Draft or revise the workstream SPEC — the durable WHAT/WHY "
        "requirements contract (Goal & Why, REQ-n with acceptance notes, "
        "FLOW-n, Non-goals, Constraints, Open Questions, Status). Use in "
        "'specify' mode. Writes a DRAFT — the USER approves it in the UI "
        "(downstream planning is blocked until approved; drafts are never "
        "shown to executing agents). Requirements not designs; ≤1–2k tokens; "
        "append-only REQ/FLOW ids. Upserts: creates the spec if absent, else "
        "revises it (editing an approved spec starts a new draft revision)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workstream_id": {
                "type": "string",
                "description": (
                    "Workstream UUID for a workstream spec. Omit for an "
                    "office-shared spec (keyed by name)."
                ),
            },
            "name": {"type": "string", "description": "REQUIRED. Spec name (workstream title, or the shared-spec name)."},
            "content": {"type": "string", "description": "REQUIRED. The full spec markdown."},
        },
        "required": ["name", "content"],
    },
    "action": "update_spec",
}

GET_SPEC: dict = {
    "name": "get_spec",
    "description": (
        "Read a spec by spec_id OR workstream_id (its current content, "
        "revision, and status). Use to review the spec before planning or "
        "revising it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "spec_id": {"type": "string", "description": "Spec UUID (or pass workstream_id)."},
            "workstream_id": {"type": "string", "description": "Workstream UUID (or pass spec_id)."},
        },
        "required": [],
    },
    "action": "get_spec",
}


# Manager surface: plan READS + close-verification + spec read. NOT the
# authoring writes — the Planner authors plans/specs; the Manager reviews,
# and spec approval is the user's UI gate.
MANAGER_PLAN_TOOLS: list[dict] = [
    GET_WORKSTREAM_PLAN,
    GET_EXECUTION_PLAN,
    COMPLETE_SCOPE_VERIFICATION,
    GET_SPEC,
]

# Planner surface: everything (it authors AND verifies, incl. the spec).
PLANNER_PLAN_TOOLS: list[dict] = [
    UPDATE_WORKSTREAM_PLAN,
    GET_WORKSTREAM_PLAN,
    UPDATE_EXECUTION_PLAN,
    GET_EXECUTION_PLAN,
    COMPLETE_SCOPE_VERIFICATION,
    UPDATE_SPEC,
    GET_SPEC,
]
