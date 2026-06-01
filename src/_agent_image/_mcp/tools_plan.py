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


# Manager surface: plan READS + close-verification. NOT the authoring writes —
# the Planner authors plans; the Manager reviews and closes verification.
MANAGER_PLAN_TOOLS: list[dict] = [
    GET_WORKSTREAM_PLAN,
    GET_EXECUTION_PLAN,
    COMPLETE_SCOPE_VERIFICATION,
]

# Planner surface: everything (it authors AND verifies).
PLANNER_PLAN_TOOLS: list[dict] = [
    UPDATE_WORKSTREAM_PLAN,
    GET_WORKSTREAM_PLAN,
    UPDATE_EXECUTION_PLAN,
    GET_EXECUTION_PLAN,
    COMPLETE_SCOPE_VERIFICATION,
]
