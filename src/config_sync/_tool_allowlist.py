"""Generated tool-allowlist rendering (T5.2.1 / R4).

The Manager CLAUDE.md used to HAND-WRITE its "Positive Allowlist" of MCP
tools — and it drifted: it omitted `consult_planner` (mandated 30+ times in
the same file), `decide_action_request`, `retry_blocked_task`, and the three
execution-plan tools, while listing `attach_to_task` which the Manager does
not have. A Manager that believes the stale allowlist refuses the Planner
flow.

The cure (R4 — "prompt facts are rendered, not hand-written"): generate the
allowlist by iterating the live catalog (`get_manager_tools()`), so the list
can NEVER drift from the real surface. The only hand-maintained piece is the
name→category grouping, which a test asserts is complete (every catalog tool
has a category).
"""
from __future__ import annotations

# Ordered category buckets for the Manager allowlist. Every tool returned by
# ``get_manager_tools()`` must map to one of these (enforced by
# ``test_manager_allowlist_render``). Order here is the render order.
_MANAGER_TOOL_CATEGORY: dict[str, str] = {
    # Board & scope writes
    "create_task": "Board & scope writes",
    "update_task": "Board & scope writes",
    "move_task": "Board & scope writes",
    "archive_task": "Board & scope writes",
    "delete_task": "Board & scope writes",
    "add_activity": "Board & scope writes",
    "retry_blocked_task": "Board & scope writes",
    "decide_action_request": "Board & scope writes",
    "create_scope": "Board & scope writes",
    "update_scope": "Board & scope writes",
    "activate_scope": "Board & scope writes",
    "archive_scope": "Board & scope writes",
    # Board & scope reads
    "get_board": "Board & scope reads",
    "get_task_detail": "Board & scope reads",
    "list_scopes": "Board & scope reads",
    "get_scope": "Board & scope reads",
    # Planner consult + execution plan
    "consult_planner": "Planner consult & execution plan",
    "get_execution_plan": "Planner consult & execution plan",
    # The escalated stuck-verify chip-flip surface (2026-07-17) — the Manager's
    # one plan WRITE, grouped with the verification-close it exists to enable.
    "update_execution_plan": "Planner consult & execution plan",
    "complete_scope_verification": "Planner consult & execution plan",
    "get_spec": "Planner consult & execution plan",
    "approve_spec": "Planner consult & execution plan",
    # Chat — the choice selector (pivot-2 P1). Asking ENDS the turn; the
    # answer arrives as the user's next message.
    "ask_user_choice": "Chat (user interaction)",
    # Flows & intake records (pivot-4 flow-intake): amend an answered
    # intake record; register/patch office flow definitions (define_flow
    # is consent-gated at the playbook level).
    "amend_intake": "Flows & intake records",
    "define_flow": "Flows & intake records",
    "update_flow": "Flows & intake records",
    # Flow runs (Flow Studio FS-P2.T9): the Manager OPERATES runs —
    # start rides user consent (the run_flow card), stop archives the
    # run's open tasks, get is the status read. Never edits definitions.
    "start_flow_run": "Flow runs (operate, never design)",
    "stop_flow_run": "Flow runs (operate, never design)",
    "get_flow_run": "Flow runs (operate, never design)",
    # Standing operations (pivot-3 P2-2) — assignment schedules: recurring
    # work WITH judgment on a cadence (or the scheduled Manager digest).
    "schedule_assignment": "Standing operations (assignment schedules)",
    "update_assignment_schedule": "Standing operations (assignment schedules)",
    "delete_assignment_schedule": "Standing operations (assignment schedules)",
    "list_assignment_schedules": "Standing operations (assignment schedules)",
    # Team
    "list_agents": "Team",
    # Files
    "save_file": "Office files",
    "list_files": "Office files",
    "get_file": "Office files",
    # Collections (ui-ux-aug19 D4.7): read-only research surface — rows
    # live on the user's machine; schema/row WRITES stay the Data
    # Curator's consult surface and never enter the Manager catalog.
    "get_collection": "Collections (read-only)",
    "query_rows": "Collections (read-only)",
    # Knowledge Base
    "search_kb": "Knowledge Base (read-only)",
    "get_kb_document": "Knowledge Base (read-only)",
    # Scripts (read-only catalog — authoring belongs to the ASD)
    "list_scripts": "Scripts (read-only)",
    "get_script": "Scripts (read-only)",
    "list_script_executions": "Scripts (read-only)",
    "list_script_templates": "Scripts (read-only)",
    "get_script_template": "Scripts (read-only)",
    # Office secrets (read-only metadata — names/descriptions, NEVER values)
    "list_office_secrets": "Office secrets (read-only)",
    "list_office_secret_usage": "Office secrets (read-only)",
}

_CATEGORY_ORDER: tuple[str, ...] = (
    "Board & scope writes",
    "Board & scope reads",
    "Planner consult & execution plan",
    "Chat (user interaction)",
    "Flows & intake records",
    "Flow runs (operate, never design)",
    "Standing operations (assignment schedules)",
    "Team",
    "Office files",
    "Collections (read-only)",
    "Knowledge Base (read-only)",
    "Scripts (read-only)",
    "Office secrets (read-only)",
)


def _manager_tool_names() -> set[str]:
    # Imported lazily so importing this module never drags the catalog in
    # before it's needed (and keeps the import local to the host-side writer).
    from src._agent_image._mcp.tools_manager import get_manager_tools

    return {t["name"] for t in get_manager_tools()}


def render_manager_allowlist() -> str:
    """Render the Manager MCP-tool allowlist grouped by category.

    Generated from ``get_manager_tools()`` — the rendered set is exactly the
    live catalog, so the "EXACTLY these" framing in the template is true by
    construction. Tools with no category mapping land in a trailing
    "Other" bucket (the completeness test fails loudly if that ever happens).
    """
    names = _manager_tool_names()
    by_cat: dict[str, list[str]] = {}
    for name in names:
        cat = _MANAGER_TOOL_CATEGORY.get(name, "Other")
        by_cat.setdefault(cat, []).append(name)

    lines: list[str] = []
    rendered_cats = list(_CATEGORY_ORDER)
    if "Other" in by_cat:  # surface uncategorised tools rather than hide them
        rendered_cats.append("Other")
    for cat in rendered_cats:
        bucket = by_cat.get(cat)
        if not bucket:
            continue
        joined = ", ".join(f"`{n}`" for n in sorted(bucket))
        lines.append(f"- {cat}: {joined}.")
    return "\n".join(lines)
