"""Pins for the research-consult persistence branch (2026-08 AI-base review).

``update_execution_plan`` requires a scope_id (the execution plan is a
column ON the scope row — the backend handler errors without one), but
`research` is deliberately NOT in the backend's ``_SCOPE_REQUIRED_MODES``,
so a workstream-level research consult is legal. The old instruction gave
that consult exactly one persistence target — the scope-only tool — so a
no-scope research session was instructed to write to a plan it cannot
address, and (research being the one mode with no FIX P3 outcome gate)
its findings died with the session.

The fix branches the instruction on scope presence at build time:

* WITH a scope → ``update_execution_plan`` (research_summary /
  component_review), unchanged — that entry stays pinned by
  ``test_aiq_planner_pins.py::test_research_mode_copy_bug_fixed``
  (no ``update_spec``, i.e. never milestones, in the scoped variant).
* WITHOUT a scope → a durable workstream-level target: ``update_spec``
  (Open Questions / notes — explicitly NEVER ``milestones``, preserving
  the 2026-07-29 copy-bug rationale) or a named workspace research file.
"""
from __future__ import annotations

from src.orchestrator.planner_prompt import (
    _MODE_INSTRUCTIONS,
    _RESEARCH_NO_SCOPE_INSTRUCTION,
    build_planner_prompt,
)


def _research_prompt(scope_id: str | None) -> str:
    consult = {
        "mode": "research",
        "objective": "investigate the thing",
        "workstream_id": "ws-1",
    }
    if scope_id:
        consult["scope_id"] = scope_id
    return build_planner_prompt({"planner_consult": consult})


def test_scoped_research_targets_the_execution_plan():
    prompt = _research_prompt("scope-1")
    assert "`update_execution_plan` (research_summary / component_review)" in prompt
    # The scoped variant must NOT point at the spec (the 2026-07-29
    # research copy bug — research has no business writing milestones).
    assert "update_spec" not in _MODE_INSTRUCTIONS["research"]
    assert "This consult has NO scope" not in prompt


def test_unscoped_research_names_a_reachable_durable_target():
    prompt = _research_prompt(None)
    # The scope-only tool is named ONLY to say it is unavailable.
    assert "This consult has NO scope" in prompt
    assert "`update_execution_plan` is NOT available" in prompt
    # Durable workstream-level targets: the spec's Open Questions/notes,
    # or a named workspace file the completion report cites.
    assert "`update_spec`" in prompt
    assert "Open Questions" in prompt
    assert "name its exact path" in prompt
    # The milestones guard survives in the no-scope variant too.
    assert "NEVER touch `milestones`" in prompt


def test_unscoped_research_instruction_is_the_dedicated_variant():
    """The branch swaps the WHOLE instruction — the no-scope prompt must
    not also carry the scoped 'write into the relevant plan' directive."""
    prompt = _research_prompt(None)
    assert _RESEARCH_NO_SCOPE_INSTRUCTION in prompt
    assert "write your findings into the relevant plan" not in prompt


def test_non_research_modes_are_untouched_by_the_branch():
    """The branch keys on mode=research only — a no-scope specify consult
    keeps its own instruction."""
    prompt = build_planner_prompt({
        "planner_consult": {
            "mode": "specify",
            "objective": "obj",
            "workstream_id": "ws-1",
        },
    })
    assert "MODE: specify." in prompt
    assert "This consult has NO scope" not in prompt
