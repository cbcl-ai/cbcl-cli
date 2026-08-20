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
    "ask_user_choice",  # pivot-2 P1: the chat question bubble is the
                        # Manager's surface alone — the Planner never talks
                        # to the user directly (its results arrive via the
                        # Manager poke). Backend handler gate refuses the
                        # planner actor too.
    # Pivot-3 P2-2: standing-operation schedules are the MANAGER's routing
    # decision (recurring-with-judgment vs script vs one-off) — manager/MA-
    # gated backend-side. The Planner plans programs, never operates them.
    "schedule_assignment",
    "update_assignment_schedule",
    "delete_assignment_schedule",
    "list_assignment_schedules",
    # Pivot-4 flow-intake (spec §C): intake records and office flows are
    # the MANAGER's chat/routing surface — amending a user's recorded
    # decisions or registering office workflows takes user-facing consent
    # the Planner never holds. Manager/MA-gated backend-side; all three
    # also excluded from the worker pool.
    "amend_intake",
    "define_flow",
    "update_flow",
    # Flow Studio (FS-P2.T9, spec §7.2): flow RUNS are operations — the
    # Manager's surface (start rides user consent, stop archives board
    # tasks). The Planner plans programs, never operates runs. All three
    # excluded; the backend gates the actions to manager/manager-assistant.
    "start_flow_run",
    "stop_flow_run",
    "get_flow_run",
    # ui-ux-aug19 D4.7: the collection READS joined the MANAGER catalog
    # (46→48) so the Manager can answer "what did the script save?"
    # directly — but the Flow Studio v1 decision stands: the Planner plans
    # programs from specs/board/KB; collection data is execution-surface
    # context. Both excluded here so the eval pin
    # (test_planner_excludes_collection_reads_v1) stays green — revisit
    # only with a spec change.
    "get_collection",
    "query_rows",
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
