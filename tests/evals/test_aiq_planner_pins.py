"""AI-quality review pins — Planner/program prompt fixes (2026-07-29).

Each pin targets a specific sentence added by the pivot-2 AI-quality
review's planner/program batch; deleting that sentence fails the eval
(mutation-checkable, the test_planner_verify_pins.py pattern). Fix
numbers reference the review's planner/program list:

  1. materialize two-entry-state branch (skeleton-exists vs no-plan
     single-pass with compressed planning) + the single two-pass
     threshold, in BOTH prompt layers.
  2. milestone judgeability — approver-checkable endpoints, never an
     internal layer.
  3. chip quality — observable-evidence chips (the verify gate's teeth)
     + never flip a chip to clear the gate.
  4. final-milestone rule — "deferred" unavailable on the last milestone.
  5. spec written for the approver + verbatim-request/References
     alignment with the update_spec tool contract.
  6. task sizing bar — expert/review boundaries, never file count.
  7. effort_hint reach — ultracode for fat builds incl.
     Planner-materialized program tasks.
  8. coverage-map evidence shape — "delivered: <task> — <check>".
"""
from __future__ import annotations

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_plan import (
    COMPLETE_SCOPE_VERIFICATION,
    UPDATE_SPEC,
)
from src.config_sync.claude_md_templates._system_agents._planner import (
    PLANNER_CLAUDE_MD,
)
from src.orchestrator.planner_prompt import _MODE_INSTRUCTIONS, build_planner_prompt

# The playbook hard-wraps at ~78 cols — pin against a whitespace-normalised
# view so a re-wrap can't break a pin.
_PLAYBOOK_NORM = " ".join(PLANNER_CLAUDE_MD.split())


def _prompt(mode: str, scope_id: str = "scope-1") -> str:
    return build_planner_prompt({
        "planner_consult": {
            "mode": mode,
            "objective": "obj",
            "workstream_id": "ws-1",
            "scope_id": scope_id,
        },
    })


# ---------------------------------------------------------------------------
# Fix 1 — materialize two-entry-state branch + the single threshold
# ---------------------------------------------------------------------------


def test_materialize_prompt_has_two_entry_states():
    prompt = _prompt("materialize")
    assert "(A) A SKELETON EXISTS" in prompt
    assert "(B) NO plan yet" in prompt
    # Single-pass compressed planning reads.
    assert "`covers` REQs" in prompt
    assert "execution_plan.verification notes" in prompt
    assert "learnings.md" in prompt
    # The plan is written BEFORE authoring, with chips armed.
    assert "BEFORE authoring" in prompt
    assert "they arm the verify gate" in prompt


def test_materialize_prompt_states_threshold_once():
    prompt = _prompt("materialize")
    assert prompt.count("6+ tasks OR open design questions") == 1
    # The idempotent re-run protocol survives the restructure verbatim.
    assert "idempotent on (scope, title)" in prompt
    assert "brief_is_complete:false" in prompt


def test_materialize_readin_admits_plan_may_be_absent():
    prompt = _prompt("materialize")
    # The old read-in claimed the plan was always approved ("Read the
    # approved plan") and "materialize does NO new research" flatly —
    # both false for the single-pass default.
    assert "may or may not exist" in prompt
    assert "Read the approved plan (`get_execution_plan`)." not in prompt
    assert "Nothing else — materialize does NO new research." not in prompt


def test_playbook_materialize_has_two_entry_states_and_threshold():
    assert "(A) a skeleton EXISTS" in _PLAYBOOK_NORM
    assert "(B) NO plan yet" in _PLAYBOOK_NORM
    assert "6+ tasks OR open design questions" in _PLAYBOOK_NORM
    # Stated ONCE — the threshold must not fork into drifting copies.
    assert _PLAYBOOK_NORM.count("6+ tasks OR open design questions") == 1


def test_research_mode_copy_bug_fixed():
    # The old research instruction named `update_execution_plan` twice and
    # pointed at `update_spec` (milestones) — a write research mode has no
    # business making.
    research = _MODE_INSTRUCTIONS["research"]
    assert research.count("update_execution_plan") == 1
    assert "update_spec" not in research


# ---------------------------------------------------------------------------
# Fix 2 — milestone judgeability
# ---------------------------------------------------------------------------


def test_playbook_pins_milestone_judgeability():
    assert "checkpoint the approver can JUDGE" in _PLAYBOOK_NORM
    assert "NEVER an internal layer" in _PLAYBOOK_NORM
    assert "merge it forward" in _PLAYBOOK_NORM


def test_specify_prompt_and_update_spec_carry_judgeability_short_form():
    specify = _MODE_INSTRUCTIONS["specify"]
    assert "checkpoint the approver can JUDGE" in specify
    assert "merge it forward" in specify
    milestones_desc = UPDATE_SPEC["inputSchema"]["properties"]["milestones"][
        "description"
    ]
    assert "approver-JUDGEABLE checkpoint" in milestones_desc
    assert "never an internal layer" in milestones_desc


# ---------------------------------------------------------------------------
# Fix 3 — chip quality (the verify gate's teeth)
# ---------------------------------------------------------------------------


def test_playbook_pins_chip_quality():
    assert "Chips are the verification gate's TEETH" in _PLAYBOOK_NORM
    assert "OBSERVABLE EVIDENCE statement" in _PLAYBOOK_NORM
    assert "≥1 chip per covered REQ" in _PLAYBOOK_NORM
    assert "NEVER a restated task title" in _PLAYBOOK_NORM
    assert "pass verification on theater" in _PLAYBOOK_NORM


def test_playbook_verify_2a_pins_no_evidence_no_done():
    assert (
        "A chip you cannot back with concrete evidence is NOT done"
        in _PLAYBOOK_NORM
    )
    assert "never flip a chip to clear the gate" in _PLAYBOOK_NORM


# ---------------------------------------------------------------------------
# Fix 4 — final-milestone rule
# ---------------------------------------------------------------------------


def test_playbook_pins_final_milestone_no_deferred():
    assert (
        'On the FINAL milestone "deferred" is NOT available' in _PLAYBOOK_NORM
    )
    assert "explicit user decision" in _PLAYBOOK_NORM


# ---------------------------------------------------------------------------
# Fix 5 — spec written for the approver + tool-contract alignment
# ---------------------------------------------------------------------------


def test_playbook_pins_spec_for_approver():
    assert "Write for the approver" in _PLAYBOOK_NORM
    assert "you can tell this is done when" in _PLAYBOOK_NORM
    assert "Non-goals are the honesty section" in _PLAYBOOK_NORM
    assert "never filler" in _PLAYBOOK_NORM


def test_playbook_spec_structure_matches_update_spec_contract():
    # The update_spec tool mandates the verbatim quoted request + a
    # References section; the playbook's seven-section structure used to
    # omit both — the two contracts must state the same opening.
    assert "VERBATIM in a quoted block" in _PLAYBOOK_NORM
    assert "**References** section" in _PLAYBOOK_NORM
    tool_desc = UPDATE_SPEC["description"]
    assert "verbatim in a quoted block" in tool_desc
    assert "References section" in tool_desc


# ---------------------------------------------------------------------------
# Fix 6 — task sizing bar
# ---------------------------------------------------------------------------


def test_playbook_pins_expert_boundary_sizing():
    # Pivot-3 P1-1 repin: the anti-fragmentation smell list grew a third
    # member — splitting on the PHASES of one job (setup → implement →
    # style → test) is as wrong as file count / hours. Spirit unchanged.
    assert "route+service+model+tests is ONE task" in _PLAYBOOK_NORM
    assert (
        "never on file count, estimated hours, or the phases of one job"
        in _PLAYBOOK_NORM
    )
    assert "split ONLY on expert or review-criteria boundaries" in (
        _PLAYBOOK_NORM
    )


# ---------------------------------------------------------------------------
# Fix 7 — effort_hint reach (fat builds incl. Planner-materialized tasks)
# ---------------------------------------------------------------------------


def _manager_create_task_props() -> dict:
    for t in get_manager_tools():
        if t["name"] == "create_task":
            return t["inputSchema"]["properties"]
    raise AssertionError("create_task not in manager catalog")


def test_effort_hint_description_reaches_program_tasks():
    desc = _manager_create_task_props()["effort_hint"]["description"]
    assert "ANY fat cohesive build task" in desc
    assert "Planner-materialized" in desc
    assert "omit for normal tasks" in desc


def test_materialize_surfaces_instruct_effort_hint_ultracode():
    assert "effort_hint:'ultracode'" in _prompt("materialize")
    assert "effort_hint: 'ultracode'" in _PLAYBOOK_NORM


# ---------------------------------------------------------------------------
# Fix 8 — coverage-map evidence shape
# ---------------------------------------------------------------------------


def test_playbook_coverage_map_example_carries_evidence():
    assert (
        "delivered: WR-003.T14 — export smoke test passed" in _PLAYBOOK_NORM
    )
    assert "per-chip evidence list, not vibes" in _PLAYBOOK_NORM


def test_verification_tool_pins_evidence_shape():
    desc = COMPLETE_SCOPE_VERIFICATION["description"]
    assert "per-chip EVIDENCE list" in desc
    assert "not vibes" in desc
    cm_desc = COMPLETE_SCOPE_VERIFICATION["inputSchema"]["properties"][
        "coverage_map"
    ]["description"]
    assert "delivered: WR-003.T14 — export smoke test passed" in cm_desc
    assert "the check that proved it" in cm_desc
