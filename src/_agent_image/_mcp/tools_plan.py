"""Shared Execution-Plan MCP tool definitions (Planner + Manager).

The Manager needs the plan READS — to review the spec's milestones and a
scope's skeleton during the two-pass authoring flow (the Manager playbook
explicitly tells it to call ``get_execution_plan`` to review the skeleton) —
plus ``complete_scope_verification`` to close a scope's verification (incl.
the stuck/escalated case where the Planner couldn't close it itself), plus
``update_execution_plan`` so the escalated-recovery path is actually
reachable: the PASS gate refuses a close while any chip is unchecked, so a
human-verified manual close needs the chip-flip write too (verify turn-end
incident 2026-07-17).

The Planner needs all of the above PLUS the remaining plan WRITES (it
authors the spec — incl. its milestones — + per-scope execution plans).

These dicts live here, with NO imports of the role modules, so both
``tools_manager`` and ``tools_planner`` can pull the right subset without a
circular import (``tools_planner`` already imports ``tools_manager``).
"""
from __future__ import annotations


# Pivot-1 T6: UPDATE_WORKSTREAM_PLAN + GET_WORKSTREAM_PLAN retired —
# the spec's Milestones section (update_spec's ``milestones`` param)
# absorbed the roadmap. The backend actions stay registered for
# grandfathered read-compat, but no catalog offers them.

UPDATE_EXECUTION_PLAN: dict = {
    "name": "update_execution_plan",
    "description": (
        "Write/replace a SCOPE's structured execution plan (research, "
        "component review, prior-scope learnings, task breakdown, risks, "
        "chips, verification). The Planner uses it in 'scope_plan'/'research' "
        "modes (and in 'materialize' for a small scope sent straight to "
        "authoring). The MANAGER touches it ONLY for the escalated "
        "stuck-verify recovery: read the plan first (get_execution_plan), "
        "flip a chip to done ONLY after personally evidence-checking it "
        "against the actual deliverables, then close via "
        "complete_scope_verification — NEVER mark a chip done unchecked. "
        "Bumps the revision each call; verification bookkeeping is preserved "
        "across edits."
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
        "couldn't close it, or a backend escalation handed it back). "
        "A PASS is GATED by the backend: it is REFUSED while any execution-plan "
        "chip is not done, OR (when the workstream has an approved spec) any "
        "requirement this scope covers is missing from coverage_map. Mark every "
        "chip done and submit a complete coverage_map, or it returns an error. "
        "`notes` is the per-chip EVIDENCE list (what check proved each chip), "
        "not vibes."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "scope_id": {"type": "string", "description": "REQUIRED. Scope UUID."},
            "passed": {"type": "boolean", "description": "REQUIRED. True if the scope's deliverables meet its plan + acceptance criteria."},
            "notes": {"type": "string", "description": "Per-chip evidence list (pass — the concrete check that proved each chip) or what's missing + rework created (fail)."},
            "coverage_map": {
                "type": "object",
                "description": (
                    "REQUIRED ON PASS when the workstream has an approved spec: "
                    "a map of every requirement id this scope is responsible for "
                    "to its outcome, each value carrying EVIDENCE — e.g. "
                    "{\"REQ-1\": \"delivered: WR-003.T14 — export smoke test "
                    "passed\", \"REQ-3\": \"deferred: moved to the auth scope\"}. "
                    "Use the exact REQ ids from the spec; 'delivered: <task "
                    "readable_id> — <the check that proved it>' means a "
                    "completed task satisfies it. The backend refuses PASS "
                    "while any covered REQ is absent here. Omit for a fail "
                    "verdict / spec-less workstream."
                ),
                "additionalProperties": {"type": "string"},
            },
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
        "'specify' mode. Writes a DRAFT — it must be APPROVED before downstream "
        "planning (who approves depends on the workstream's spec-approval mode: "
        "the USER in the UI for user-mode, or the Manager via approve_spec for "
        "manager-mode). Drafts are never shown to executing agents. "
        "MUST open with the user's original request verbatim in a quoted "
        "block, plus a References section listing the exact path/URL of "
        "every user-provided material. Downstream agents see only this spec. "
        "Requirements not designs; ≤1–2k tokens (the cap excludes the "
        "quoted request block); "
        "append-only REQ/FLOW ids. Carries the MILESTONES section — the "
        "ordered scope checklist (this ABSORBED the old roadmap; there is "
        "no separate roadmap artifact). Upserts: creates the spec if "
        "absent, else revises it (editing an approved spec starts a new "
        "draft revision)."
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
            "content": {"type": "string", "description": "REQUIRED. The full spec markdown. MUST open with the user's original request verbatim in a quoted block, plus a References section listing the exact path/URL of every user-provided material — downstream agents see only this spec."},
            "milestones": {
                "type": "array",
                "description": (
                    "The Milestones section (pivot-1 T6 — the ordered scope "
                    "checklist that absorbed the roadmap): [{key, title, goal, "
                    "order, depends_on:[key], covers:[REQ-id], status: "
                    "planned|in_progress|done|dropped, scope_id?, notes}]. "
                    "Right-size each milestone to ONE scope (<=13 tasks); "
                    "write the FEWEST milestones that cover every REQ. Each "
                    "milestone must END at an approver-JUDGEABLE checkpoint "
                    "(something to see/run/read/click), never an internal "
                    "layer."
                ),
                # AIQ fix 15 (2026-07-29): typed items so the CLI rejects a
                # malformed milestone before the backend round-trip; matches
                # backend SpecMilestone (key/title required, order the sort
                # driver).
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Stable short label (e.g. 'Auth') — the scope's short_key must equal it exactly to link scope↔milestone."},
                        "title": {"type": "string", "description": "Milestone title."},
                        "goal": {"type": "string", "description": "One-sentence user-visible outcome."},
                        "order": {"type": "integer", "description": "Execution order (1-based)."},
                        "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Other milestone keys this one waits on."},
                        "covers": {"type": "array", "items": {"type": "string"}, "description": "Exact spec REQ ids this milestone delivers — the verify coverage gate reads this."},
                        "status": {"type": "string", "enum": ["planned", "in_progress", "done", "dropped"], "description": "Bookkeeping status."},
                        "scope_id": {"type": "string", "description": "Linked scope UUID (set when the scope is opened)."},
                        "notes": {"type": "string", "description": "Free-form notes."},
                    },
                    "required": ["key", "title", "order"],
                },
            },
        },
        "required": ["name", "content"],
    },
    "action": "update_spec",
}

GET_SPEC: dict = {
    "name": "get_spec",
    "description": (
        "Read a spec by spec_id OR workstream_id (its current content, "
        "milestones, revision, and status). Milestones ride the spec — "
        "this is ALSO how you read the roadmap (there is no separate "
        "roadmap artifact). Use to review the spec before planning or "
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

APPROVE_SPEC: dict = {
    "name": "approve_spec",
    "description": (
        "Approve a workstream's spec DRAFT (draft → approved; materialises "
        "spec.md and unblocks scope planning). Manager only. Use this in a "
        "MANAGER-APPROVAL workstream AFTER you've reviewed the draft — read it "
        "with get_spec, confirm it captures the user's requirements (no gaps / "
        "mismatches / ambiguity), and consult_planner(mode='specify') to revise "
        "it if needed. In a USER-APPROVAL workstream this is refused — the user "
        "approves it in the Spec panel. Pass workstream_id (or spec_id)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "workstream_id": {"type": "string", "description": "Workstream UUID (or pass spec_id)."},
            "spec_id": {"type": "string", "description": "Spec UUID (or pass workstream_id)."},
        },
        "required": [],
    },
    "action": "approve_spec",
}


# Manager surface: plan READS + close-verification + spec read + spec APPROVE
# (the Manager reviews then approves in manager-approval workstreams) + ONE
# authoring write, UPDATE_EXECUTION_PLAN — the chip-flip surface for the
# escalated stuck-verify recovery (verify turn-end incident 2026-07-17: the
# backend gate `_PLAN_WRITER_ACTORS` always admitted the manager actor and
# `upsert_execution_plan` preserves verdict bookkeeping, but the tool was
# Planner-catalog-only, so a legal human-verified manual close was unreachable
# from a Manager session). The OTHER authoring write (the spec, incl. its
# milestones) stays Planner-only — the Planner authors; the Manager reviews.
MANAGER_PLAN_TOOLS: list[dict] = [
    GET_EXECUTION_PLAN,
    UPDATE_EXECUTION_PLAN,
    COMPLETE_SCOPE_VERIFICATION,
    GET_SPEC,
    APPROVE_SPEC,
]

# Planner surface: everything (it authors AND verifies, incl. the spec).
PLANNER_PLAN_TOOLS: list[dict] = [
    UPDATE_EXECUTION_PLAN,
    GET_EXECUTION_PLAN,
    COMPLETE_SCOPE_VERIFICATION,
    UPDATE_SPEC,
    GET_SPEC,
]
