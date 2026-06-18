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
from .tools_plan import PLANNER_PLAN_TOOLS


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
    "approve_spec",     # the Planner AUTHORS the spec (update_spec); the
                        # Manager reviews + approves it — never the Planner.
})


def get_planner_tools() -> list[dict]:
    """Allowed Manager board tools (planning surface) + the full plan tools.

    Uses an explicit EXCLUDE set so the Planner can never reach destructive
    or manager-only board actions, matching its "plan and verify only,
    never execute" contract.

    The Manager base now ALSO carries the plan READS + complete_scope_verification
    (``MANAGER_PLAN_TOOLS``); the Planner additionally needs the plan WRITES,
    so we append ``PLANNER_PLAN_TOOLS`` and dedup by name (the shared tools
    appear in both — the write tools are Planner-only).
    """
    base = [
        t for t in get_manager_tools()
        if t.get("name") not in _PLANNER_EXCLUDED_MANAGER_TOOLS
    ]
    seen = {t.get("name") for t in base}
    for t in PLANNER_PLAN_TOOLS:
        if t.get("name") not in seen:
            base.append(t)
            seen.add(t.get("name"))
    return base
