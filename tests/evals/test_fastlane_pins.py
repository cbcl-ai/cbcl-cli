"""Eval family: execution-fastlane pins (ai/execution-fastlane, 2026-07-21).

The fastlane remediation re-tiered the Manager's right-sizing ladder so a
cohesive one-sitting build ships as ONE task to ONE ultracode-effort agent
instead of a scope + Planner ceremony, raised the scope-first threshold from
"2+ related tasks" to "4+ related tasks that need cross-task ordering or
verification", made the user's original request travel VERBATIM into every
brief's Inputs, capped artifact registration, and made the Auditor's report
file conditional (FAIL / on-request only — a PASS is the move_task verdict).

Every one of those is a prompt-surface fact — playbook prose or a tool
description — with no code enforcement behind it, so these pins are the
regression teeth. Each assertion targets a specific sentence; deleting or
re-paraphrasing that sentence fails the eval (mutation-checkable). Pins run
against whitespace-normalised views so a re-wrap can't dodge them.
"""
from __future__ import annotations

from pathlib import Path

import src.config_sync.claude_md_templates as _templates_pkg
from src._agent_image._mcp.tools_manager import get_manager_tools
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
from src.config_sync.claude_md_templates._shared_agent import (
    SHARED_AGENT_WORK_RULES,
)
from src.config_sync.claude_md_templates._system_agents._auditor import (
    AUDITOR_CLAUDE_MD,
)

# The playbooks hard-wrap at ~78 cols, so pin against whitespace-normalised
# views (same sentence, independent of where the wrap falls).
_MANAGER_NORM = " ".join(MANAGER_CLAUDE_MD.split())
_SHARED_NORM = " ".join(SHARED_AGENT_WORK_RULES.split())
_AUDITOR_NORM = " ".join(AUDITOR_CLAUDE_MD.split())


def _manager_tool(name: str) -> dict:
    tools = {t["name"]: t for t in get_manager_tools()}
    assert name in tools, f"{name} missing from the Manager catalog"
    return tools[name]


# ---------------------------------------------------------------------------
# (a) Tier 1b — the cohesive one-sitting build fastlane
# ---------------------------------------------------------------------------


def test_manager_playbook_has_tier_1b():
    """The fastlane tier must exist by name in the right-sizing ladder."""
    assert "Tier 1b — Cohesive one-sitting build." in _MANAGER_NORM


def test_tier_1b_routes_to_one_ultracode_agent_without_ceremony():
    """Tier 1b is ONE task to ONE agent with internal orchestration — no
    scope, no Planner, no upstream research task."""
    assert "Create ONE task to ONE agent" in _MANAGER_NORM
    assert "ultracode/xhigh effort" in _MANAGER_NORM
    assert "orchestrates its own subagents internally" in _MANAGER_NORM
    assert (
        "NO scope, NO Planner consult, NO upstream research task"
        in _MANAGER_NORM
    )


def test_tier_1b_mandates_verbatim_inputs():
    """The tier's own text must carry the verbatim-Inputs rule — the model
    reads the tier entry when routing, before it ever reaches the brief
    section."""
    assert "request VERBATIM into the brief's Inputs" in _MANAGER_NORM
    assert "every reference path/URL" in _MANAGER_NORM


def test_tier_1b_defaults_to_light_ma_review():
    assert (
        "Default reviewer: manager-assistant with smoke-test acceptance "
        "criteria" in _MANAGER_NORM
    )
    assert "≤3 objectively checkable items" in _MANAGER_NORM


# ---------------------------------------------------------------------------
# (b) Scopes are program milestones — the ladder re-center (pivot-3 P1-2
# repinned the fastlane 4+ threshold: 2-5 related fat assignments chain with
# depends_on and never get a scope; a scope exists only inside a program)
# ---------------------------------------------------------------------------


def test_scope_first_threshold_is_program_milestones():
    """The canonical threshold sentence must survive verbatim."""
    assert (
        "a scope exists ONLY inside a Tier-3 program (one per milestone, "
        "1-3 fat tasks each)" in _MANAGER_NORM
    )


def test_two_to_five_tasks_are_plain_depends_on_chains():
    assert (
        "2-5 related tasks are plain tasks chained with `depends_on` — "
        "no scope" in _MANAGER_NORM
    )


def test_old_two_plus_scope_mandate_appears_nowhere_in_templates():
    """The retired "scope-first is MANDATORY for 2+ related tasks" wording
    must not survive in ANY template module (playbooks, shared rules,
    office/workstream/custom-agent generators). Scanned at the source-file
    level so generator-rendered templates are covered without invoking
    them, and whitespace-normalised so a re-wrap can't hide it."""
    templates_dir = Path(_templates_pkg.__file__).parent
    offenders = []
    for path in sorted(templates_dir.rglob("*.py")):
        norm = " ".join(path.read_text(encoding="utf-8").split())
        if "MANDATORY for 2+ related tasks" in norm:
            offenders.append(str(path.relative_to(templates_dir)))
    assert offenders == [], (
        "retired 2+ scope mandate resurfaced in: " + ", ".join(offenders)
    )


def test_create_scope_tool_steers_away_from_small_work():
    """The create_scope description must carry the pivot-1 T2 steer (07
    review repin — the old "1-3 tasks or one agent session" clause revived
    the retired 4+-task heuristic): 2-5 related assignments chain unscoped,
    one-sitting builds are ONE unscoped task, and a scope exists ONLY as a
    program milestone holding 1-3 fat tasks (tool descriptions are the
    closest prompt surface at call time)."""
    desc = _manager_tool("create_scope")["description"]
    norm = " ".join(desc.split())
    assert (
        "Skip for 2-5 related assignments (plain depends_on-chained "
        "unscoped tasks) and for any one-sitting build (ONE unscoped task)"
        in norm
    )
    assert (
        "a scope exists ONLY as a program MILESTONE, holding 1-3 fat tasks"
        in norm
    )
    # The retired threshold must not resurface.
    assert "Skip whenever the work fits 1-3 tasks" not in norm
    assert "Scopes add planning + verification wall-clock" in norm


def test_create_task_scope_id_param_carries_the_threshold():
    """Pivot-3 P1-2 repin (was the fastlane 4+ threshold): the scope_id
    param description states the program-milestone model — scopes are
    milestones (ONE fat assignment, 2-3 on expert boundaries), 2-5 related
    fat assignments chain unscoped, and one-sitting builds default to ONE
    unscoped task."""
    props = _manager_tool("create_task")["inputSchema"]["properties"]
    desc = " ".join(props["scope_id"]["description"].split())
    assert "scopes are PROGRAM MILESTONES" in desc
    assert (
        "a milestone-scope normally holds ONE fat assignment (2-3 only on "
        "a genuine expert boundary)" in desc
    )
    assert (
        "2-5 related fat assignments ship as plain tasks chained with "
        "depends_on — no scope" in desc
    )
    assert "the DEFAULT for prototypes and one-sitting builds" in desc


# ---------------------------------------------------------------------------
# (c) Artifact HARD CAP in the shared worker rules
# ---------------------------------------------------------------------------


def test_shared_rules_pin_artifact_hard_cap():
    assert (
        "HARD CAP: register at most 3 artifacts per task — normally ONE "
        "consolidated deliverable" in _SHARED_NORM
    )


def test_shared_rules_pin_consolidate_without_asking():
    """Over-cap briefs consolidate silently — a permission round-trip would
    stall the task on a formatting question."""
    assert "consolidate into fewer documents" in _SHARED_NORM
    assert "do not ask permission to consolidate" in _SHARED_NORM


def test_shared_rules_pin_length_caps():
    """The companion length canon: bounded deliverables/checkpoints/verdicts,
    and cut — never a spillover file."""
    assert "Deliverable documents: <=2 pages (~800 words)" in _SHARED_NORM
    assert "Checkpoints: <=3 lines. Review verdict bodies: <=30 lines" in (
        _SHARED_NORM
    )
    assert (
        "never create an extra file just to hold overflow evidence"
        in _SHARED_NORM
    )


# ---------------------------------------------------------------------------
# (d) create_task inputs — the VERBATIM original request
# ---------------------------------------------------------------------------


def test_manager_create_task_inputs_requires_verbatim_request():
    """The Manager's create_task `inputs` description is where briefs are
    actually authored — it must demand the user's ORIGINAL request verbatim
    and forbid paraphrase."""
    props = _manager_tool("create_task")["inputSchema"]["properties"]
    desc = " ".join(props["inputs"]["description"].split())
    assert "Paste the user's ORIGINAL request VERBATIM (quoted, unedited)" in (
        desc
    )
    assert "never paraphrase or summarize it" in desc
    assert "exact path/URL of every user-provided reference" in desc


def test_manager_playbook_brief_rules_mirror_the_verbatim_mandate():
    """Same fact in the playbook's brief-writing rules — and the prose hard
    caps must be explicitly scoped to the Manager's OWN fields so they can
    never justify truncating the quoted request."""
    assert (
        "The brief's Inputs MUST open with the user's original request "
        "VERBATIM" in _MANAGER_NORM
    )
    assert "Never paraphrase, summarize, or truncate it" in _MANAGER_NORM
    assert (
        "hard caps apply to your own prose fields, never to the quoted "
        "request" in _MANAGER_NORM
    )


# ---------------------------------------------------------------------------
# (e) Auditor report file is conditional — FAIL / on-request only
# ---------------------------------------------------------------------------


def test_auditor_report_file_is_conditional():
    """The audit-flow step must gate the report file on the verdict (or an
    explicit brief request) — the old unconditional 'save it as an office
    file' produced a report per PASS."""
    assert (
        "ONLY on a FAIL / CONDITIONAL verdict, or when the brief requests "
        "an audit artifact" in _AUDITOR_NORM
    )


def test_auditor_pass_is_verdict_only_no_report_file():
    """A PASS lives entirely in the move_task comment + structured verdict;
    both completion checklists must forbid the PASS report file."""
    assert (
        "A PASS is fully recorded by the `move_task` comment + structured "
        "verdict — no report file" in _AUDITOR_NORM
    )
    assert "do NOT save a report file for it" in _AUDITOR_NORM


def test_auditor_reviewer_checklist_gates_save_on_fail_or_request():
    """The designated-reviewer completion checklist's save step must carry
    the same condition (mutation check on the second checklist, not just
    the flow section)."""
    assert (
        "On FAIL / CONDITIONAL — or when the brief requests an audit "
        "artifact — save the full audit report" in _AUDITOR_NORM
    )


# ---------------------------------------------------------------------------
# Pivot-1 T1: the Builder system agent is the Tier-1b executor
# ---------------------------------------------------------------------------


def test_tier_1b_names_the_builder_as_default_executor():
    """The Tier 1b route must name the `builder` system agent as the
    fallback executor when no domain specialist fits (pivot-1 T1)."""
    assert (
        "else the `builder` system agent" in _MANAGER_NORM
    ), "Tier 1b no longer names the builder agent"


def test_builder_playbook_pins_one_shot_session_and_artifact_cap():
    """The Builder playbook must carry the two rules that keep ultracode
    execution safe: the one-shot-session rule (never end the turn while
    workflows are pending) and the 3-artifact hard cap."""
    from src.config_sync.claude_md_templates._system_agents import (
        BUILDER_CLAUDE_MD,
    )

    norm = " ".join(BUILDER_CLAUDE_MD.split())
    assert "Your session is ONE-SHOT." in norm, (
        "Builder playbook lost the one-shot-session rule"
    )
    assert "NEVER end your turn" in norm
    assert "HARD CAP 3 artifacts" in norm, (
        "Builder playbook lost the artifact hard cap"
    )
    assert "verbatim" in norm.lower(), (
        "Builder playbook lost the verbatim-Inputs reading rule"
    )
