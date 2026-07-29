"""Eval family: AI-quality review — worker-surface pins (2026-07-29).

The AI-quality review's worker-surface fixes are prompt-surface facts with no
code enforcement behind them, so these pins are the regression teeth:

- Action S — the MA-run SMOKE review the Manager's Tier-1b flow promises
  (the review's top finding: the MA playbook used to FORBID the exact smoke
  check the Manager's default-reviewer routing relies on).
- The Builder's product-delivery canon (non-technical delivery, ONE project
  dir, command-backed verification, never simulate a deploy).
- The fat-build .py carve-outs (shared STOP section + Auditor red-flag step)
  so a one-sitting build's product source stops tripping the script pipeline.
- The mode-aware reviewer STEP 0.1, the ask-class prompt purge of
  update_status('review'), the office non-technical-reader output rule, the
  Auditor depth dial, the MA tool-error rule, and the published-collections
  KB reuse line.

Pins run against whitespace-normalised views so a re-wrap can't dodge them.
The backend system-prompt mirrors are pinned by reading the backend source
file (monorepo layout); those tests skip on a standalone communicator
checkout (e.g. the cbcl-cli mirror) where backend/ is absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config_sync.claude_md_content import (
    AUDITOR_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents import (
    BUILDER_CLAUDE_MD,
)
from src.orchestrator.worker_prompt import build_worker_prompt

_MA_NORM = " ".join(MANAGER_ASSISTANT_CLAUDE_MD.split())
_AUDITOR_NORM = " ".join(AUDITOR_CLAUDE_MD.split())
_BUILDER_NORM = " ".join(BUILDER_CLAUDE_MD.split())
_SHARED_NORM = " ".join(SHARED_AGENT_WORK_RULES.split())
_OFFICE_NORM = " ".join(SHARED_OFFICE_CLAUDE_MD.split())

# Backend mirror pins read the backend source file directly (prompt strings
# are plain module constants). Monorepo-relative; absent on standalone
# communicator checkouts.
_BACKEND_SYSTEM_AGENTS = (
    Path(__file__).resolve().parents[3]
    / "backend" / "app" / "agents" / "system_agents.py"
)

_needs_backend = pytest.mark.skipif(
    not _BACKEND_SYSTEM_AGENTS.exists(),
    reason="backend tree not present (standalone communicator checkout)",
)


def _backend_norm() -> str:
    return " ".join(
        _BACKEND_SYSTEM_AGENTS.read_text(encoding="utf-8").split()
    )


_BRIEF = {
    "goal": "G", "context": "C", "inputs": "None",
    "output_format": "OF",
    "acceptance_criteria": ["AC1"],
    "allowed_tools": [],
    "required_skills": [],
    "risks_and_edge_cases": "None",
    "verification_steps": "VS",
}


def _task(**overrides):
    base = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "AQ-001.T01",
        "title": "Eval task",
        "status": "ready",
        "rework_count": 0,
        "brief": dict(_BRIEF),
        "workstream_short_code": "AQ",
        "assigned_agent": "dev",
        "reviewer": "auditor",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1 — MA smoke review (Action S)
# ---------------------------------------------------------------------------


def test_ma_playbook_has_action_s_smoke_review():
    """The Review Management decision tree opens with the smoke-review
    check BEFORE the Action A/B routing."""
    assert "is this a SMOKE review you should run yourself?" in _MA_NORM
    assert "Action S" in _MA_NORM
    # The qualifying conditions.
    assert "YOU are the designated reviewer" in _MA_NORM
    assert "≤3" in _MA_NORM
    assert (
        "NOT production code, credentials, or data-integrity" in _MA_NORM
    )
    # The one carve-out from the no-deliverable-reading rule.
    assert (
        "the ONE review shape where you DO open the deliverable" in _MA_NORM
    )
    # Bounded, and never expanded into a full audit.
    assert "Budget ≈5 calls" in _MA_NORM
    assert "Do NOT expand into a full audit" in _MA_NORM


def test_ma_non_reviewer_rule_is_scoped_to_non_smoke():
    """The old unconditional "You are NOT a reviewer" hard rule forbade the
    exact smoke check the Manager's Tier-1b flow promises. It must now be
    scoped to the non-smoke case."""
    assert "Outside Action S, you are NOT a reviewer." in _MA_NORM
    # The unscoped absolute must not resurface.
    assert "**You are NOT a reviewer.**" not in MANAGER_ASSISTANT_CLAUDE_MD
    # The call cap is scoped too (Action S has its own budget).
    assert (
        "Maximum 3 tool calls per non-smoke Review-triage turn" in _MA_NORM
    )


@_needs_backend
def test_backend_ma_prompt_mirrors_smoke_exception():
    """MANAGER_ASSISTANT_DEFAULT_PROMPT hard rule 1 must carry the smoke
    exception so the system prompt and the playbook agree."""
    norm = _backend_norm()
    assert "a SMOKE review (playbook Action S)" in norm
    assert "≤3 objectively checkable" in norm
    # The absolute no-read is scoped, not unconditional.
    assert "Otherwise do NOT read deliverable files" in norm


# ---------------------------------------------------------------------------
# 2 — Builder product-delivery canon
# ---------------------------------------------------------------------------


def test_builder_playbook_has_three_delivery_sections():
    for headline in (
        "Deliver it like a product, not a repo",
        "Where a multi-file build lives",
        "Verify with commands, not confidence",
    ):
        assert headline in _BUILDER_NORM, (
            f"Builder playbook lost section: {headline!r}"
        )


def test_builder_delivery_rules_are_concrete():
    # Non-technical delivery: one copy-pasteable how-to-see step + RUN.md.
    assert "what / where / how-to-see" in _BUILDER_NORM
    assert "RUN.md" in _BUILDER_NORM
    assert "Prefer zero-setup tech" in _BUILDER_NORM
    # ONE project dir, ONE registered artifact — never one per file.
    assert "ONE project directory" in _BUILDER_NORM
    assert "never one `save_file` per file" in _BUILDER_NORM
    # Command-backed verification + the honest not-verified list.
    assert "build/lint/tests exit 0" in _BUILDER_NORM
    assert "LIST what you genuinely could NOT verify" in _BUILDER_NORM
    assert "honest gap beats a false PASS" in _BUILDER_NORM


def test_builder_never_simulates_a_deploy():
    assert "never simulate a deploy" in _BUILDER_NORM
    assert "`blocker_class=missing_credential`" in _BUILDER_NORM
    assert "locally-runnable build" in _BUILDER_NORM


@_needs_backend
def test_backend_builder_rule3_aligned_with_playbook():
    """Builder hard rule 3: the working artifact + ONE summary (RUN.md for a
    multi-file build), hard cap 3 — aligned with the playbook's
    one-artifact-per-tree rule."""
    norm = _backend_norm()
    assert "the summary/RUN.md pointing at the project tree" in norm
    assert "never one per file" in norm


# ---------------------------------------------------------------------------
# 3 — fat-build .py carve-outs (shared STOP section + Auditor red-flags)
# ---------------------------------------------------------------------------


def test_shared_stop_section_carves_out_fat_build_product_source():
    assert (
        "a one-sitting build delivered as a project tree" in _SHARED_NORM
    )
    assert "RE-RUNNABLE AUTOMATION" in _SHARED_NORM
    assert (
        "the product source code OF your build is the deliverable, not a "
        "script" in _SHARED_NORM
    )


def test_auditor_red_flag_step_has_product_source_exception():
    assert "Exception — fat-build product source." in _AUDITOR_NORM
    assert "product source, not a mis-routed script" in _AUDITOR_NORM
    assert (
        "apply the Code review path above, not the mis-route FAIL"
        in _AUDITOR_NORM
    )


# ---------------------------------------------------------------------------
# 4 — reviewer STEP 0.1 is mode-aware
# ---------------------------------------------------------------------------


def test_step01_routes_designated_reviewer_instead_of_stop():
    """A review dispatch to the designated reviewer must NOT open with an
    unconditional STOP — STEP 0.1 routes reviewers/Board Operators to their
    role instructions and reserves the STOP for the executor."""
    prompt = build_worker_prompt(
        _task(status="review", assigned_agent="dev", reviewer="auditor"),
    )
    norm = " ".join(prompt.split())
    assert "this prompt contains a DESIGNATED REVIEWER section below" in norm
    assert "you are here to REVIEW/triage this task" in norm
    assert "If status is `review` and YOU were its executor → STOP" in norm


def test_step01_executor_stop_still_present_on_every_prompt():
    prompt = build_worker_prompt(_task(status="ready"))
    norm = " ".join(prompt.split())
    assert "YOU were its executor → STOP" in norm


# ---------------------------------------------------------------------------
# 5 — ask-class prompts carry no submit-for-review machinery
# ---------------------------------------------------------------------------


def test_ask_prompt_contains_no_update_status_review_instruction():
    """An ask closes with ONE move_task('done') — the prompt must not carry
    a single `update_status('review')` instruction (STEP 0.6/0.7, branches,
    and the non-negotiable rules all branch on the class)."""
    prompt = build_worker_prompt(_task(task_class="ask"))
    assert "update_status('review')" not in prompt
    # The ask close machinery is present instead.
    assert "move_task('done')" in prompt or "How to Close This Ask Task" in (
        prompt
    )
    # No completion marker for asks.
    assert "COMPLETED.json" not in prompt
    assert "Completion fence" not in prompt
    # Artifact copy branches: asks normally register nothing.
    assert "An ask normally produces NO artifacts" in prompt


def test_assignment_prompt_keeps_submit_machinery():
    prompt = build_worker_prompt(_task(task_class="assignment"))
    assert "update_status('review')" in prompt
    assert "COMPLETED.json" in prompt
    assert "An ask normally produces NO artifacts" not in prompt


# ---------------------------------------------------------------------------
# 6 — office Output Style rule 5: write for a non-technical reader
# ---------------------------------------------------------------------------


def test_office_output_style_has_non_technical_reader_rule():
    assert "Write for a non-technical reader." in _OFFICE_NORM
    assert "no unexplained jargon" in _OFFICE_NORM
    assert "what the result MEANS" in _OFFICE_NORM
    assert (
        "Technical evidence stays, under a labelled evidence section, after "
        "the plain-language answer" in _OFFICE_NORM
    )


# ---------------------------------------------------------------------------
# 7 — Auditor depth dial
# ---------------------------------------------------------------------------


def test_auditor_playbook_has_depth_dial():
    assert (
        "Right-size the depth — read Verification Steps first." in
        _AUDITOR_NORM
    )
    assert (
        "do not expand a prototype into the full per-work-type audit"
        in _AUDITOR_NORM
    )
    assert (
        "Reserve full depth for production code, credentials, "
        "data-integrity" in _AUDITOR_NORM
    )


# ---------------------------------------------------------------------------
# 8 — shared trims kept the pointer honest
# ---------------------------------------------------------------------------


def test_shared_reviewer_section_is_a_pointer_with_the_two_invariants():
    """The collapsed "When You Are a Reviewer" section must point at the
    task-prompt DESIGNATED REVIEWER block and keep exactly the two
    never-change rules (ONE move_task; never touch assigned_agent)."""
    assert "## When You Are a Reviewer" in SHARED_AGENT_WORK_RULES
    assert (
        "full DESIGNATED REVIEWER instructions in the task prompt"
        in _SHARED_NORM
    )
    assert "resolve the review with ONE `move_task` call" in _SHARED_NORM
    assert "NEVER touch `assigned_agent`" in _SHARED_NORM
    # The near-duplicate step-by-step (which lacked the rework-cap branch)
    # must not resurface: the shared section carries no verdict template.
    reviewer_section = SHARED_AGENT_WORK_RULES.split(
        "## When You Are a Reviewer", 1,
    )[1].split("##", 1)[0]
    assert "VERDICT: PASS" not in reviewer_section
    assert "get_my_brief" not in reviewer_section


# ---------------------------------------------------------------------------
# 9 — MA hardening: tool errors are not blockers / not an outage
# ---------------------------------------------------------------------------


def test_ma_playbook_has_tool_error_rule():
    assert "Tool Errors ≠ Blockers ≠ MCP Down" in _MA_NORM
    assert "retry ONCE" in _MA_NORM
    assert 'Never conclude "MCP unavailable"' in _MA_NORM
    assert "never move a task to `blocked` over a tool error" in _MA_NORM


def test_ma_role1_has_artifact_boundary_pointer():
    assert (
        "a lookup/check whose answer fits the submit comment registers "
        "nothing" in _MA_NORM
    )


@_needs_backend
def test_backend_ma_prompt_bounds_the_synthesis_comment():
    """The backend MA prompt's "comprehensive synthesis comment" invited
    walls of text — it must now carry the ≤8-line bound (matching the
    playbook's 8-line synthesis mandate)."""
    norm = _backend_norm()
    assert "post a synthesis `comment` (≤8 lines)" in norm
    assert "comprehensive synthesis" not in norm


# ---------------------------------------------------------------------------
# 11 — published-collections KB reuse (compounding across offices)
# ---------------------------------------------------------------------------


def test_shared_rules_point_at_published_collections():
    assert 'company "Published — <office name>" KB collections' in _SHARED_NORM
    assert "search them before re-researching" in _SHARED_NORM
    assert "cite what you reuse" in _SHARED_NORM
