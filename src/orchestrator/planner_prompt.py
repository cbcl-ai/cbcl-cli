"""Planner session prompt builder (execution_improvements_v1 Phase 3).

The Planner is consulted asynchronously by the Manager. It runs as a
worker process (AGENT_NAME=planner) whose synthetic task carries a
``planner_consult`` marker. This module turns that marker into the
session prompt. The full operating playbook lives in the Planner's
``/workspace/agents/planner/CLAUDE.md`` (auto-discovered); this prompt
states the specific consult: mode, objective, and the ids to act on.
"""
from __future__ import annotations

from typing import Any

_MODE_INSTRUCTIONS = {
    "roadmap": (
        "MODE: roadmap. Build (or revise) the WORKSTREAM ROADMAP — the "
        "ordered list of every intended scope for this body of work. "
        "Research the objective, decompose it end-to-end (do NOT stop at "
        "the obvious first scopes — the gap you miss is the bug), then "
        "persist it via `update_workstream_plan`. Do NOT create scope or "
        "task rows in this mode; the Manager reviews the roadmap first."
    ),
    "scope_plan": (
        "MODE: scope_plan. Produce the detailed EXECUTION PLAN for the "
        "scope named below. Research, review related existing components, "
        "read prior scopes' verification outcomes, then write the plan via "
        "`update_execution_plan` (summary, research_summary, "
        "component_review, prior_scope_learnings, task_breakdown, risks, "
        "chips). If — and only if — explicitly asked to materialize it, "
        "create the scope's tasks with `create_task` (each with a COMPLETE "
        "9-field brief + depends_on for ordering). Do not activate_scope "
        "unless told; the Manager reviews first."
    ),
    "research": (
        "MODE: research. Investigate the question in the objective and "
        "write your findings into the relevant plan via "
        "`update_execution_plan` (research_summary / component_review) or "
        "`update_workstream_plan`. Cite sources. Do not execute task work."
    ),
    "verify": (
        "MODE: verify. The scope below has all tasks finished. Verify its "
        "deliverables against its execution plan AND every task's "
        "acceptance criteria (read get_scope / get_task_detail / the "
        "registered artifacts). Then call "
        "`complete_scope_verification(scope_id, passed, notes)`. On FAIL, "
        "create the specific rework task(s) FIRST, then call it with "
        "passed=false."
    ),
}


def build_planner_prompt(task_data: dict[str, Any]) -> str:
    """Build the Planner's session prompt from a planner-consult task."""
    consult = task_data.get("planner_consult") or {}
    mode = (consult.get("mode") or "roadmap").strip()
    objective = (consult.get("objective") or "").strip()
    workstream_id = consult.get("workstream_id") or ""
    scope_id = consult.get("scope_id") or ""

    ws_ctx = task_data.get("workstream_context") or {}
    ws_name = ws_ctx.get("name", "") if isinstance(ws_ctx, dict) else ""
    ws_goals = ws_ctx.get("goals", "") if isinstance(ws_ctx, dict) else ""
    ws_desc = ws_ctx.get("description", "") if isinstance(ws_ctx, dict) else ""

    lines: list[str] = [
        "# Planning Consult",
        "",
        "You are the office Planner. The Manager has consulted you. Follow "
        "your CLAUDE.md playbook (`/workspace/agents/planner/CLAUDE.md`) "
        "exactly. You PLAN and VERIFY — you never execute the deliverable "
        "work itself.",
        "",
        _MODE_INSTRUCTIONS.get(mode, _MODE_INSTRUCTIONS["roadmap"]),
        "",
        "## Objective",
        objective or "(none provided — infer from the workstream context)",
        "",
        "## Identifiers",
        f"- workstream_id: `{workstream_id}`",
    ]
    if scope_id:
        lines.append(f"- scope_id: `{scope_id}`")
    lines.append("")

    if ws_name or ws_goals or ws_desc:
        lines.append("## Workstream context")
        if ws_name:
            lines.append(f"- Name: {ws_name}")
        if ws_desc:
            lines.append(f"- Description: {ws_desc}")
        if ws_goals:
            lines.append(f"- Goals: {ws_goals}")
        lines.append("")

    lines.extend([
        "## Before you start",
        "1. Read the current roadmap (`get_workstream_plan`) and scopes "
        "(`list_scopes`).",
        "2. Check existing knowledge / files (`search_kb`, `list_files`) "
        "and prior scopes' verification outcomes.",
        "3. Review related components in the workspace (Glob / Grep / Read).",
        "",
        "## When done",
        "Persist the plan (or verdict) via your plan tools, then STOP. The "
        "Manager is notified automatically — do not message the user "
        "directly.",
    ])
    return "\n".join(lines)
