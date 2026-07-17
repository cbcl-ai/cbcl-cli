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

from src.config_sync.claude_md_templates._spec_template import (
    workstream_spec_path,
)

_MODE_INSTRUCTIONS = {
    "specify": (
        "MODE: specify. Draft (or revise) the workstream SPEC — the durable "
        "WHAT/WHY requirements contract. Research the objective + the user's "
        "request, then write the spec via the `update_spec` tool "
        "(workstream_id + name + content) following the seven-section "
        "structure: Goal & Why, Requirements (append-only REQ-n, one sentence "
        "+ acceptance note each), User Flows (FLOW-n), Non-goals, Constraints, "
        "Open Questions, Status. Requirements NOT designs (the plan owns HOW); "
        "≤1–2k tokens; surface ambiguities as Open Questions for the user. "
        "This writes a DRAFT — the USER approves it in the UI before any "
        "planning. Do NOT write the roadmap or create scopes/tasks in this "
        "mode."
    ),
    "roadmap": (
        "MODE: roadmap. The workstream SPEC is already APPROVED (the user "
        "gated it in `specify` mode; the backend refuses roadmap while the "
        "spec is an unapproved draft). DO NOT draft or `Write` the spec here "
        "— that is `specify` mode's job. First `get_spec` to read the approved "
        "requirements, THEN build (or revise) the WORKSTREAM ROADMAP — the "
        "ordered list of every intended scope for this body of work. For EACH "
        "planned scope set its `covers` field to the exact spec requirement "
        "ids that scope delivers (e.g. `covers: [\"REQ-1\", \"REQ-3\"]`) — this "
        "structured field makes the roadmap a coverage map over the spec AND "
        "is what the scope-verification gate checks, so every REQ in the spec "
        "must be covered by exactly one scope (no REQ left uncovered). Research "
        "the objective, decompose it end-to-end "
        "(do NOT stop at the obvious first scopes — the gap you miss is the "
        "bug), then persist via `update_workstream_plan`. Do NOT create scope "
        "or task rows in this mode; the Manager reviews the roadmap first."
    ),
    "scope_plan": (
        "MODE: scope_plan. Produce the SKELETON execution plan for the "
        "scope named below — this is a PLANNING pass, NOT a creation pass. "
        "Research, review related existing components, read prior scopes' "
        "verification outcomes, then write the plan via "
        "`update_execution_plan` (summary, research_summary, "
        "component_review, prior_scope_learnings, task_breakdown, risks, "
        "chips). The task_breakdown is the skeleton: per task a title + "
        "one-line intent + assigned_agent + depends_on — NOT full briefs. "
        "Keep the scope to a BALANCED set, NEVER more than 13 tasks; if it "
        "needs more, split it across scopes via `update_workstream_plan` "
        "instead. The scope ALREADY EXISTS — it is the scope_id you were "
        "given; the skeleton attaches to it. Do NOT create TASK rows and do "
        "NOT `activate_scope` — the Manager reviews the skeleton, then "
        "consults you again with mode=materialize to author the tasks."
    ),
    "materialize": (
        "MODE: materialize. The skeleton execution plan for the scope below "
        "was reviewed and approved. Author its tasks — this is an AUTHORING "
        "pass, do NO new research. First read the approved plan "
        "(`get_execution_plan`) so every sibling task is in view. "
        "THEN — CRITICAL, this may be a RE-RUN of a partial materialize — call "
        "`get_board(scope_id=…)` to list the tasks ALREADY created in this "
        "scope. For each task_breakdown item, check that list FIRST: "
        "(a) if no task with that title exists yet, `create_task(scope_id=…)` "
        "it with a COMPLETE 9-field brief + `depends_on`; "
        "(b) if a task with that title EXISTS but its brief is incomplete "
        "(`brief_is_complete:false` — a partial run can leave has_brief:true "
        "with missing fields), call `create_task` again with the SAME title and "
        "the full brief — creation is idempotent on (scope, title), so this "
        "FILLS the existing row's brief instead of adding a duplicate; "
        "(c) if it already exists WITH a complete brief, skip it. "
        "This makes a re-run safe: you converge the scope to exactly one "
        "well-briefed task per breakdown item, never duplicates. "
        "The scope ALREADY EXISTS (the scope_id below) — do NOT create it. "
        "Size each task for ONE focused AI session: solid and detailed, not "
        "fragmented into slivers, not so big it can't finish cleanly. Keep "
        "deps consistent and avoid duplication across tasks. A scope must "
        "NEVER exceed 13 tasks — if the breakdown has more, author the first "
        "13 and flag in your completion that the scope is too large and must "
        "be split. Do NOT `activate_scope` — the Manager reviews and activates."
    ),
    "research": (
        "MODE: research. Investigate the question in the objective and "
        "write your findings into the relevant plan via "
        "`update_execution_plan` (research_summary / component_review) or "
        "`update_workstream_plan`. Cite sources. Do not execute task work."
    ),
    "verify": (
        "MODE: verify. The scope below has all tasks finished. Verify its "
        "deliverables comprehensively — NO gaps, NO unverified claims:\n"
        "1. Read the execution plan (`get_execution_plan`) and the tasks "
        "(`get_board(scope_id=…)` + `get_task_detail`) and open the registered "
        "artifacts. Check each task's deliverable against its acceptance "
        "criteria.\n"
        "2. Confirm EVERY execution-plan chip is actually satisfied and mark it "
        "done via `update_execution_plan` — the backend REFUSES a PASS while "
        "any chip is unchecked.\n"
        "3. If the workstream has a spec, `get_spec` and check the requirements "
        "this scope covers (given to you below as 'Requirements this scope "
        "covers'). For EACH covered REQ decide 'delivered' (a completed task "
        "satisfies it) or, only if it genuinely belongs elsewhere, 'deferred: "
        "<where/why>'. A REQ that is neither is a verification FAIL.\n"
        "4. Call `complete_scope_verification(scope_id, passed, notes, "
        "coverage_map)` where coverage_map maps every covered REQ to its "
        "outcome (e.g. {\"REQ-1\": \"delivered\"}). The backend REFUSES a PASS "
        "while any covered REQ is absent from coverage_map.\n"
        "On FAIL, create the specific rework task(s) FIRST, then call with "
        "passed=false (coverage_map optional on fail).\n"
        "SIZING — verification is read + judge, not build: for scopes of "
        "≤5 tasks prefer DIRECT evidence checks (read the plan, briefs, "
        "artifacts and run read-only checks yourself) over spawning a "
        "dynamic workflow. When a workflow IS warranted, cap fan-out at "
        "≤4 concurrent verification subagents — office containers are "
        "CPU-capped, so parallel subagents mostly serialize; extra fan-out "
        "adds wall-clock time, not depth.\n"
        "ONE-SHOT SESSION — this is a ONE-SHOT headless session: ending "
        "your turn EXITS the process and KILLS any still-running workflow "
        "subagents or background tasks. Background work will NEVER "
        "re-invoke you — that contract does not exist here. NEVER end your "
        "turn to wait for a workflow: await IN-TURN with a bounded, "
        "timeout-wrapped poll loop (`timeout 600 bash -c 'until <check>; "
        "do sleep 15; done'` — the bash guard allows timeout-prefixed "
        "waits), or size the work to complete synchronously within this "
        "turn.\n"
        "HARD RULES for the verdict call: `complete_scope_verification` is "
        "the LAST act of YOUR main session and MUST be made by YOU directly "
        "— NEVER delegate the verdict call to a workflow subagent, and NEVER "
        "end the session without it; a session that ends with no accepted "
        "verdict is a FAILED verify and will be re-run from scratch. If a "
        "PASS is refused (unchecked chips / missing coverage_map entries), "
        "FIX the cause (mark the chips via `update_execution_plan`, complete "
        "the coverage_map) and call again — do not stop on a refused verdict."
    ),
}


def build_planner_prompt(task_data: dict[str, Any]) -> str:
    """Build the Planner's session prompt from a planner-consult task."""
    consult = task_data.get("planner_consult") or {}
    mode = (consult.get("mode") or "roadmap").strip()
    objective = (consult.get("objective") or "").strip()
    workstream_id = consult.get("workstream_id") or ""
    scope_id = consult.get("scope_id") or ""
    approved_spec_reqs = consult.get("approved_spec_reqs") or []
    scope_covers = consult.get("scope_covers") or []

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
    if ws_name:
        lines.append(f"- spec path: `{workstream_spec_path(ws_name)}`")
    lines.append("")

    if mode == "verify" and (approved_spec_reqs or scope_covers):
        lines.append("## Requirements this scope covers (verify)")
        if scope_covers:
            lines.append(
                "- This scope is responsible for these spec requirements — "
                "EACH must appear in your `coverage_map` marked 'delivered' or "
                f"explicitly 'deferred: <where/why>': {', '.join(scope_covers)}."
            )
        elif approved_spec_reqs:
            lines.append(
                "- The roadmap has no `covers` tag for this scope; the approved "
                f"spec defines {', '.join(approved_spec_reqs)}. Submit a "
                "`coverage_map` accounting for the requirements this scope "
                "delivers (use the exact REQ ids)."
            )
        if approved_spec_reqs:
            lines.append(
                f"- Full approved-spec requirement set: "
                f"{', '.join(approved_spec_reqs)}."
            )
        lines.append(
            "- A PASS is REFUSED while any covered REQ is absent from "
            "coverage_map or any execution-plan chip is undone."
        )
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
