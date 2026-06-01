"""Planner-role MCP tool list (execution_improvements_v1 Phase 3).

The Planner needs a manager-like board toolset (create_scope / create_task /
update_task / activate_scope + reads) PLUS the plan-write/verify tools. We
build it by reusing the Manager toolset (minus ``consult_planner`` — the
Planner does not consult itself) and appending the plan tools.

The Planner is spawned as a normal worker process whose ``AGENT_NAME`` is
``planner``; ``mcp_tool_server`` selects THIS toolset for that agent name and
exempts it from the executor guard. The plan tools' ``_caller.agent_name``
(stamped by ``_mcp_backend``) is ``planner``, which satisfies the backend's
role gate on ``complete_scope_verification`` / ``update_*_plan``.
"""
from __future__ import annotations

from .tools_manager import get_manager_tools


# The plan-write + verify tools, unique to the Planner.
_PLAN_TOOLS: list[dict] = [
    {
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
    },
    {
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
    },
    {
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
    },
    {
        "name": "get_execution_plan",
        "description": "Read a scope's structured execution plan.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
            },
            "required": ["scope_id"],
        },
        "action": "get_execution_plan",
    },
    {
        "name": "complete_scope_verification",
        "description": (
            "VERIFY mode only. Resolve a scope's verification after checking "
            "its deliverables against the execution plan + task acceptance "
            "criteria. passed=true → scope goes 'done' and the Manager is "
            "prompted to plan the next scope. passed=false → create the "
            "rework task(s) FIRST, then call this; the scope returns to "
            "'executing' and the rework dispatches."
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
    },
]


# Manager tools the Planner must NOT have. It plans + verifies + materializes
# scopes/tasks — it never deletes/archives/force-moves tasks, retries blocked
# tasks, decides action_requests, or consults itself. Anything not excluded
# here (board reads, create_task/create_scope/update_task, activate/archive
# scope, add_activity, KB/file/script reads) is legitimate planning surface.
_PLANNER_EXCLUDED_MANAGER_TOOLS = frozenset({
    "consult_planner",  # never consults itself
    "move_task",        # status transitions are the reviewer's / workers' job
    "delete_task",      # destructive
    "archive_task",     # destructive
    "retry_blocked_task",
    "decide_action_request",
})


def get_planner_tools() -> list[dict]:
    """Allowed Manager board tools (planning surface) + the plan tools.

    Uses an explicit EXCLUDE set so the Planner can never reach destructive
    or manager-only board actions, matching its "plan and verify only,
    never execute" contract.
    """
    base = [
        t for t in get_manager_tools()
        if t.get("name") not in _PLANNER_EXCLUDED_MANAGER_TOOLS
    ]
    return base + _PLAN_TOOLS
