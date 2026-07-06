"""Eval family: spec-driven planning (Phase 10 S-A behaviors).

Pins the load-bearing prompt/template facts so they can't silently rot:
  - Planner drafts the spec FIRST in roadmap mode, scopes carry `covers:`,
  - Manager's Tier-3 rung starts with the spec + the requirement-change
    routing rule (spec-first, never patch a brief),
  - briefs cite `[REQ-n]` and reviewers verify against the cited REQs,
  - STEP 0.0 reads the workstream spec when the workstream has one,
  - the office/workstream templates point at the spec convention.

Each assertion targets a specific sentence; deleting that sentence from
the prompt fails the eval (mutation-checkable). The S-C scope extends this
file (T10.3.5).
"""
from __future__ import annotations

from src.config_sync.claude_md_content import (
    MANAGER_CLAUDE_MD,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._spec_template import (
    workstream_spec_path,
)
from src.config_sync.claude_md_templates._system_agents._planner import (
    PLANNER_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._workstream import (
    generate_workstream_claude_md,
)
from src.orchestrator.manager_context import build_dynamic_context
from src.orchestrator.planner_prompt import build_planner_prompt
from src.orchestrator.worker_prompt import build_worker_prompt


class _Store:
    """Minimal ConfigStore stand-in for build_dynamic_context."""

    def get_workstream_list(self):
        return []

    def get_team_roster(self):
        return ""


_BRIEF_NO_SPEC = {
    "goal": "G", "context": "C", "inputs": "None", "output_format": "OF",
    "acceptance_criteria": ["Do the thing"],
    "allowed_tools": [], "required_skills": [],
    "risks_and_edge_cases": "None", "verification_steps": "VS",
}
_BRIEF_WITH_REQ = {
    **_BRIEF_NO_SPEC,
    "acceptance_criteria": ["Hamburger menu shown below 768px [REQ-4]"],
}


def _task(brief, *, status="ready", agent="developer"):
    return {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "AU-001.T01",
        "title": "Eval task",
        "status": status,
        "rework_count": 0,
        "brief": brief,
        "workstream_short_code": "AU",
        "assigned_agent": agent,
        "reviewer": "auditor",
        "workstream_context": {"name": "Auth Project"},
    }


# ---- T10.1.2 — Planner specify-first + covers: -----------------------------


def test_planner_playbook_specifies_first_and_requires_covers():
    assert "Specify first" in PLANNER_CLAUDE_MD
    assert "covers: [REQ" in PLANNER_CLAUDE_MD
    # Requirements-not-designs guardrail.
    assert "Requirements, not designs" in PLANNER_CLAUDE_MD
    # Append-only id hygiene.
    assert "append-only" in PLANNER_CLAUDE_MD.lower()


def test_planner_specify_prompt_drafts_via_update_spec():
    # specify mode OWNS spec drafting (via update_spec → DB draft → user
    # approval), and injects the spec path.
    prompt = build_planner_prompt({
        "planner_consult": {
            "mode": "specify", "objective": "Draft the spec",
            "workstream_id": "w1",
        },
        "workstream_context": {"name": "Auth Project"},
    })
    assert "update_spec" in prompt
    assert "DRAFT" in prompt
    assert workstream_spec_path("Auth Project") in prompt


def test_planner_roadmap_prompt_reads_approved_spec_not_drafts_it():
    # roadmap mode must NOT re-draft the spec (that would bypass the approval
    # gate); it reads the already-approved spec and builds the covers: map.
    prompt = build_planner_prompt({
        "planner_consult": {
            "mode": "roadmap", "objective": "Build auth",
            "workstream_id": "w1",
        },
        "workstream_context": {"name": "Auth Project"},
    })
    assert "already APPROVED" in prompt
    assert "get_spec" in prompt
    # Scopes carry a STRUCTURED covers field of exact REQ ids (the coverage map
    # the verification gate checks), not a free-text notes tag.
    assert 'covers: ["REQ' in prompt
    assert workstream_spec_path("Auth Project") in prompt
    # It must NOT instruct a Write of the spec in roadmap mode.
    assert "`Write` it" not in prompt


def test_planner_materialize_cites_req_in_briefs():
    assert "[REQ-n]" in PLANNER_CLAUDE_MD


# ---- T10.1.3 — Manager Tier-3 spec-first + requirement-change routing ------


def test_manager_tier3_starts_with_spec():
    assert "STARTS WITH THE SPEC" in MANAGER_CLAUDE_MD


def test_manager_requirement_change_routes_spec_first():
    assert "Requirement changes — spec first" in MANAGER_CLAUDE_MD
    # The hard rule: never patch a brief for a requirement change.
    assert "NEVER `update_task` a brief because a REQUIREMENT changed" in (
        MANAGER_CLAUDE_MD
    )
    # Worked examples distinguishing requirement vs task-level.
    assert "magic-link" in MANAGER_CLAUDE_MD


# ---- T10.1.4 — briefs cite REQ, reviewer verifies against REQs --------------


def test_manager_brief_guidance_cites_req():
    assert "[REQ-4]" in MANAGER_CLAUDE_MD
    assert "where the workstream has a spec" in MANAGER_CLAUDE_MD.lower()


def test_reviewer_block_checks_spec_when_req_tagged():
    # Reviewer of a REQ-tagged task gets the spec-check step.
    prompt = build_worker_prompt(_task(_BRIEF_WITH_REQ, status="review"))
    assert "Spec check" in prompt
    assert "contradicts" in prompt.lower()


# ---- T10.1.5 — STEP 0.0 spec read (gated on has-spec) ----------------------


def test_step0_reads_spec_when_workstream_has_spec():
    prompt = build_worker_prompt(_task(_BRIEF_WITH_REQ))
    assert "0.0a — Read the workstream SPEC" in prompt
    assert workstream_spec_path("Auth Project") in prompt


def test_step0_omits_spec_read_when_no_spec():
    prompt = build_worker_prompt(_task(_BRIEF_NO_SPEC))
    assert "0.0a — Read the workstream SPEC" not in prompt


def test_explicit_has_spec_flag_forces_spec_read():
    task = _task(_BRIEF_NO_SPEC)
    task["workstream_has_spec"] = True
    prompt = build_worker_prompt(task)
    assert "0.0a — Read the workstream SPEC" in prompt


def test_office_template_documents_spec_convention():
    assert "## Specs (requirements contracts)" in SHARED_OFFICE_CLAUDE_MD
    assert "/workspace/specs/office/" in SHARED_OFFICE_CLAUDE_MD


def test_config_store_get_office_specs_filters_workstream_specs():
    """T10.2.4: ConfigStore ingests config['specs'] and get_office_specs()
    returns only the office-shared ones (workstream_id is None)."""
    import asyncio

    from src.config_sync.sync_service import ConfigStore

    store = ConfigStore()
    asyncio.run(store.update_from_sync({
        "config": {
            "office_name": "X",
            "specs": [
                {"id": "s1", "name": "Shared", "revision": 1,
                 "workstream_id": None, "path": "specs/office/shared.md"},
                {"id": "s2", "name": "WS", "revision": 1,
                 "workstream_id": "ws-1", "path": "workstreams/a/spec.md"},
            ],
        }
    }))
    # Full ingest keeps both; the getter filters to office-shared.
    assert len(store.specs) == 2
    office = store.get_office_specs()
    assert [s["id"] for s in office] == ["s1"]


def test_config_store_get_office_specs_empty_when_unset():
    from src.config_sync.sync_service import ConfigStore

    assert ConfigStore().get_office_specs() == []


def test_manager_context_swaps_to_spec_pointer_when_present():
    ctx = {
        "workstream_id": "w1",
        "workstream_name": "Auth Project",
        "workstream_description": "raw desc that should be hidden",
        "workstream_goals": "raw goals",
        "spec": {
            "title": "Auth Project",
            "revision": 2,
            "path": "/workspace/workstreams/auth-project/spec.md",
        },
    }
    out = build_dynamic_context("workstream:w1", ctx, _Store())
    assert "## Workstream Spec" in out
    assert "rev 2" in out
    # The raw (now-subsumed) description/goals block is suppressed.
    assert "raw desc that should be hidden" not in out


def test_manager_context_keeps_raw_meta_when_no_spec():
    ctx = {
        "workstream_id": "w1",
        "workstream_name": "Quick WS",
        "workstream_description": "raw desc shown for spec-less ws",
        "workstream_goals": "raw goals",
    }
    out = build_dynamic_context("workstream:w1", ctx, _Store())
    assert "## Workstream Spec" not in out
    assert "raw desc shown for spec-less ws" in out


def test_manager_context_draft_spec_manager_mode_prompts_self_approval():
    """Incident 2026-06-23: a DRAFT spec in a manager-approval workstream must
    surface a proactive 'review + approve YOURSELF' instruction in standing
    context (no path → not the approved block), AND keep the raw metadata
    visible (a draft is not yet the contract)."""
    ctx = {
        "workstream_id": "w1",
        "workstream_name": "Auth Project",
        "workstream_description": "raw desc still useful pre-approval",
        "workstream_goals": "raw goals",
        "spec": {
            "title": "Auth Project",
            "revision": 1,
            "status": "draft",
            "spec_approval": "manager",
            # no path — drafts are never materialised
        },
    }
    out = build_dynamic_context("workstream:w1", ctx, _Store())
    assert "DRAFT awaiting YOUR approval" in out
    assert "approve_spec" in out
    # FX-24.T02 backstop (prod bug "A"): manager-approval mode must explicitly
    # tell the Manager NOT to defer approval to the user — the observed bug was
    # the Manager asking the user to approve despite manager-approval mode.
    assert "Do NOT ask the user to approve it" in out
    # It must NOT render the approved "Read the spec at path" block.
    assert "It is the WHAT/WHY contract" not in out
    # Raw metadata stays visible while the draft is pending (regression: it
    # used to vanish the moment a draft existed).
    assert "raw desc still useful pre-approval" in out


def test_manager_context_draft_spec_user_mode_does_not_tell_manager_to_approve():
    """A DRAFT in a user-approval workstream must tell the Manager the USER
    approves (and that it must NOT call approve_spec)."""
    ctx = {
        "workstream_id": "w1",
        "workstream_name": "Auth Project",
        "workstream_description": "raw desc",
        "workstream_goals": "raw goals",
        "spec": {
            "title": "Auth Project",
            "revision": 1,
            "status": "draft",
            "spec_approval": "user",
        },
    }
    out = build_dynamic_context("workstream:w1", ctx, _Store())
    assert "DRAFT awaiting the USER's approval" in out
    assert "must NOT call" in out and "approve_spec" in out
    assert "DRAFT awaiting YOUR approval" not in out


# ---- T10.1.6 — Context Notes subsumed by spec ------------------------------


def test_workstream_template_points_durable_context_at_spec():
    ws = generate_workstream_claude_md(
        {"name": "Auth Project", "short_code": "AU"},
    )
    assert "Durable requirements live in the spec" in ws
    assert workstream_spec_path("Auth Project") in ws


# ---- Scope 10.3 (S-C) — change discipline + traceability -------------------


def test_planner_documents_spec_change_impact_pass():
    assert "Spec changes — the spec-first protocol" in PLANNER_CLAUDE_MD
    assert "Impact pass" in PLANNER_CLAUDE_MD
    # The core discipline: never patch briefs directly for a requirement change.
    assert "never patch task briefs directly" in PLANNER_CLAUDE_MD
    # Append-only id rule restated in the change protocol.
    assert "never renumber" in PLANNER_CLAUDE_MD


def test_planner_verify_checks_req_coverage():
    assert "REQ coverage" in PLANNER_CLAUDE_MD
    # A covered REQ that isn't delivered or consciously deferred is a FAIL.
    assert "verification FAIL" in PLANNER_CLAUDE_MD
    # Coverage is reported via the STRUCTURED coverage_map argument (the
    # verification gate checks it), NOT written back into the spec.
    assert "coverage_map" in PLANNER_CLAUDE_MD


def test_propose_spec_update_in_worker_catalog():
    from src._agent_image._mcp.tools_worker import get_worker_tools

    names = {t["name"] for t in get_worker_tools()}
    assert "propose_spec_update" in names


def test_propose_spec_update_transform_carries_all_fields():
    # The 04/F1 lesson: the transform must carry every payload field through
    # to the backend (else the user sees an empty Inbox card).
    from src._agent_image._mcp.transforms import transform_params

    out = transform_params(
        "propose_action",
        "propose_spec_update",
        {
            "proposed_text": "REQ-9 magic-link login",
            "rationale": "user asked for it mid-task",
            "target": "REQ-9",
            "spec_id": "abc",
        },
    )
    payload = out["payload"]
    assert out["request_type"] == "propose_spec_update"
    assert payload["proposed_text"] == "REQ-9 magic-link login"
    assert payload["rationale"] == "user asked for it mid-task"
    assert payload["target"] == "REQ-9"
    assert payload["spec_id"] == "abc"


# --- MGR-04 / TOOL-02: spec-approval wording must be MODE-AWARE --------------
# The DECISION-8 contract: user-mode = the USER approves; manager-mode = the
# Manager reviews + approves via approve_spec (no user gate). Any surface that
# says "the USER approves" UNCONDITIONALLY re-introduces the days-long stall
# the manager-approval mode exists to remove.

def _manager_md() -> str:
    return MANAGER_CLAUDE_MD.replace("{manager_tool_allowlist}", "").replace(
        "{office_name}", "X"
    )


def test_manager_right_size_ladder_spec_approval_is_mode_aware():
    md = _manager_md()
    # The Tier-3 spec step must name BOTH modes, not assert "user approves".
    seg = md.split("Tier 3", 1)[1][:1200]
    assert "approve_spec" in seg, "manager-mode approval path (approve_spec) missing"
    assert "user-approval" in seg or "USER approves" in seg
    assert "manager-approval" in seg or "manager-mode" in seg.lower()


def test_consult_planner_and_update_spec_descriptions_are_mode_aware():
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_planner import get_planner_tools

    cp = next(
        t for t in get_manager_tools() if t["name"] == "consult_planner"
    )["description"]
    # Must not claim an unconditional user gate; must reference approve_spec.
    assert "approve_spec" in cp
    assert "spec-approval mode" in cp or "per mode" in cp

    us = next(
        t for t in get_planner_tools() if t["name"] == "update_spec"
    )["description"]
    assert "approve_spec" in us
    assert "spec-approval mode" in us
