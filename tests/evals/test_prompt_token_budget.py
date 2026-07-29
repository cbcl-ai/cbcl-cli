"""EVAL-08 — token-budget regression guards on the standing prompt templates.

Every one of these templates is prepended to EVERY session for its role, so
uncontrolled growth is a per-turn tax that also dilutes the salience of the
load-bearing rules. Nothing pinned their size, so BP-02 (Manager prompt) and
BP-06 (office file) grew across releases unnoticed.

Ceilings are set as a HARD char budget with modest headroom above the current
rendered size — they catch meaningful regrowth today and are meant to be
RATCHETED DOWN as the P7 (context-economy) trims land. A failure here means
either: trim the template, or (deliberately) raise its ceiling in the same
commit with a note on why the growth earns its tokens.

Measured as characters (deterministic); ~chars/4 ≈ tokens. If you raise a
ceiling, update the comment so the intent is reviewable.
"""
from __future__ import annotations

from src.config_sync._tool_allowlist import render_manager_allowlist
from src.config_sync.claude_md_content import (
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents import (
    BUILDER_CLAUDE_MD,
    PLANNER_CLAUDE_MD,
)


def _manager() -> str:
    return (
        MANAGER_CLAUDE_MD.replace(
            "{manager_tool_allowlist}", render_manager_allowlist()
        )
        .replace("{office_name}", "Test Office")
        .replace("{office_specs_index}", "")
    )


def _office() -> str:
    return (
        SHARED_OFFICE_CLAUDE_MD.replace("{office_name}", "Test Office")
        .replace("{office_specs_index}", "")
        .replace("{office_output_style}", "")
    )


# name -> (rendered text, char ceiling). Ceilings ~current + headroom; RATCHET
# DOWN as P7 trims land (MGR-01 targets the Manager toward ~57k; BP-06/CTX-02
# targets a role-parametrized, smaller office file).
_BUDGETS = {
    # MGR-01: ratcheted 66_000→64_500 so Manager-prompt growth is a deliberate,
    # reviewable decision (the finding's eval half). The finding's ~52KB target
    # assumed collapsing the "General Chat Tool Restrictions" tool enumeration,
    # but that enumeration is REQUIRED by the MGR-05 truthfulness guard
    # (test_general_chat_strip.test_manager_prompt_gc_strip_claims_match_code) —
    # a categorical "all writes stripped" claim would drop the per-tool pins
    # that stop the prose understating the stripped set. So the prose-trim half
    # is intentionally deferred; this tighter ceiling is the enforceable part.
    # Ceiling raised 64_500→66_500 (2026-07-17, verify turn-end incident):
    # the "Scope stuck in verifying (escalated)" recovery recipe (re-consult
    # verify → human-verified manual close via the new update_execution_plan
    # chip-flip surface) — ~1.1k chars of load-bearing deadlock recovery,
    # pinned by evals/test_planner_verify_pins.py (Manager-recovery pins).
    # Ceiling raised 66_500→67_500 (2026-07-21, execution-fastlane canon):
    # net growth after the sole-orchestrator/13-task-cap dedup from the new
    # canonical blocks — Tier 1b (one-sitting build), the CANON-VERBATIM
    # brief-Inputs rule, CANON-LIGHT-REVIEW, the 4+-task scope threshold,
    # and SINGLE-SCOPE COLLAPSE — ~0.35k chars over the old ceiling.
    # manager 67_500→69_500 (2026-07-28, pivot-1 T1-T8): Tier 1b names the
    # Builder + effort_hint sizing; the work_mode ceremony dial (Tier 3
    # requires program mode; Planner section program-only); Tier 0 ask-class
    # + review-skip protocol; Brief 2.0 four-part contract markers; the
    # standing-ops off-board rule. All load-bearing ROUTING text — the
    # pivot's core. Regrowth pressure stays: the Phase-2 dedup (known issue
    # I-7) is still the standing trim target.
    # manager 69_500→71_000 (2026-07-28, pivot-2 P2-3): the program-boundary
    # consent block (~2.4k chars — the ask_user_choice(kind=execution_mode)
    # flow with the exact option copy, D6 anti-nag hard rules, the D1
    # never-flip-yourself rule, teaching-error-as-cue, the workstream
    # mental-model line; pinned by evals/test_pivot2_pins.py). Paid down
    # ~1.4k by retiring the dial-flip copy and deduping the tier-ladder
    # restatement in Core Rule 2, the scope-threshold repeats in Workflow
    # step 3 / General-Chat tail, the Workstreams intro, and the stale
    # execution-planning-flag caveat on [verifying] — net +1.37k of new
    # consent routing text over the old ceiling.
    # manager 71_000→71_500 (2026-07-29, pivot-2 review fixes C-1/C-2/C-5/C-6):
    # the explicit-wording branch now matches the backend contract (typed
    # consent has NO application path — run the selector as a one-click
    # confirmation, true skip only in an already-consented program), the
    # anti-nag ONLY-sentence is scoped to execution_mode asks + one line on
    # legitimate informational use, the GC-strip prose names ask_user_choice,
    # the reply-turn block states the "Selected: {label}" plain-user-row
    # arrival (rotated-session robustness), and own_workstream states the
    # suppressed origin turn. Paid down ~0.21k (GC summary sentence,
    # notices-nothing tail, two pointer trims) — net +0.23k over the old
    # ceiling; all growth is consent-routing correctness copy pinned by
    # evals/test_pivot2_pins.py.
    "manager": (_manager(), 71_500),          # ~17.4k tok; 71.2k rendered now
    # office ceiling raised 16.0k→17.5k for the INJ-01 "Untrusted Content"
    # security directive (justified growth); P7 (CTX-02 role-split) trims it.
    "office": (_office(), 17_500),            # ~4k tok now → P7 role-split ↓
    # shared_agent 19_500→20_000 (2026-07-21, execution-fastlane canon): the
    # CANON-ARTIFACT-CAP hard cap (≤3 artifacts) + CANON-LENGTH bounds
    # (≤2-page deliverables / ≤3-line checkpoints / ≤30-line verdicts) +
    # CANON-PLAN-CAPS in PLANNER_WORK_RULES — small net growth (~54 chars
    # over) after the saved-report-file removals.
    # 20_000→20_500 (2026-07-28, pivot-1 C-3): the ask-class exception to
    # submit-for-review (~0.3k chars — any executor can draw an ask task;
    # pinned by evals/test_pivot1_pins.py ask-carveout pin).
    "shared_agent": (SHARED_AGENT_WORK_RULES, 20_500),
    "analyst": (ANALYST_CLAUDE_MD, 32_000),
    # auditor 32_000→32_500 (2026-07-21, execution-fastlane): the
    # conditional-report posture (report file ONLY on FAIL/CONDITIONAL or a
    # brief-requested artifact) stated at each completion flow — ~0.2k chars.
    # 32_500→33_000 (2026-07-28, pivot-1 C-3): inherits the shared rules'
    # ask-class carve-out (~0.3k chars — see shared_agent above).
    "auditor": (AUDITOR_CLAUDE_MD, 33_000),
    # asd ratcheted 60_000→56_000 (2026-07-21, execution-fastlane): the
    # main.py reference collapsed to a ~30-line skeleton + dedups landed
    # (~52.4k rendered now) — keep the guard's regrowth pressure.
    "asd": (AUTOMATION_SCRIPT_DEV_CLAUDE_MD, 56_000),  # ~12.8k tok
    # builder (pivot-1 T1): deliberately LEAN — ~4.5k own chars + the shared
    # rules (~17.6k). The Builder's value is executing, not reading playbook;
    # keep regrowth pressure on it.
    "builder": (BUILDER_CLAUDE_MD, 26_000),
    # ceiling raised 30.0k→33.0k for CTX-06: the MA is the direct-Bash
    # verification agent but did NOT load SHARED_AGENT_WORK_RULES, so it lacked
    # (a) the safety-critical no-blocking-Bash rule (Tier-2 session-churn fix)
    # and (b) the ESCALATED blocker template it cites ("see your shared work
    # rules" was dangling). It now appends the two shared constants it needs
    # (LONG_RUNNING_BASH_RULE + BLOCKED_ESCALATION_TEMPLATE, ~3k chars) rather
    # than the whole ~18k playbook.
    "manager_assistant": (MANAGER_ASSISTANT_CLAUDE_MD, 33_000),
    # WRK-03: dropped from ~40k→~21k when the Planner swapped the full
    # executor-shaped SHARED_AGENT_WORK_RULES for the consult-scoped
    # PLANNER_WORK_RULES (which still carries the no-blocking-Bash safety rule —
    # the Planner has the Bash tool). Ceiling ratcheted 37k→22.5k.
    # Ceiling raised 22.5k→23.0k (2026-07-17) for the verify-mode fan-out
    # sizing guidance (long-verify incident 2026-07-16 follow-up: direct
    # checks for ≤5-task scopes; ≤4 concurrent verification subagents on
    # CPU-capped containers) — ~0.5k chars, pinned by
    # evals/test_planner_verify_pins.py::test_playbook_pins_fanout_sizing.
    # Raised again 23.0k→24.5k (2026-07-17, verify turn-end incident) for the
    # ONE-SHOT session contract (verify §2d + the shared LONG_RUNNING_BASH_RULE
    # one-shot section the Planner inherits via PLANNER_WORK_RULES) — ~1.1k
    # chars pinned by evals/test_planner_verify_pins.py one-shot pins.
    # Raised 24_500→25_500 (2026-07-21, execution-fastlane): CANON-PLAN-CAPS
    # (plan length caps, in the playbook + PLANNER_WORK_RULES), the ≤5-task
    # single-pass materialize default, and the fewest-scopes roadmap rule —
    # ~0.6k chars over the old ceiling.
    "planner": (PLANNER_CLAUDE_MD, 25_500),
}


def test_standing_templates_within_char_budget():
    over = {
        name: (len(text), ceiling)
        for name, (text, ceiling) in _BUDGETS.items()
        if len(text) > ceiling
    }
    assert not over, (
        "standing prompt template(s) over budget — trim, or raise the ceiling "
        f"in the same commit with a rationale: {over}"
    )


def test_budget_guard_is_not_vacuous():
    # The guard must have real headroom pressure: no ceiling may sit more than
    # ~35% above the current rendered size, or it stops catching regrowth.
    slack = {
        name: round(ceiling / max(len(text), 1), 2)
        for name, (text, ceiling) in _BUDGETS.items()
    }
    too_loose = {n: r for n, r in slack.items() if r > 1.35}
    assert not too_loose, (
        f"ceiling(s) too loose to catch regrowth (ratchet down): {too_loose}"
    )


# CTX-11: the STANDING context bill a role actually pays each session is the
# shared office file PLUS that role's own rendered playbook (with the
# capability fragments the writer appends). Pin the per-role total so a section
# added to the office file — which every role loads — or to a role playbook
# fails loudly against a role budget, not just the per-template budgets above.
# allowed_tools drive the CTX-02 Bash fragment append, so they MUST mirror the
# real system-agent configs (backend/app/agents/system_agents.py
# SYSTEM_AGENT_DEFAULTS). ALL FIVE system agents currently ship WITH Bash
# ("platform policy: every agent can run commands"), so each receives the
# BASH_CAPABILITY_RULES fragment. (An earlier version of this eval wrongly gave
# analyst + planner NO Bash and thus understated their real per-role stacks.)
_ROLE_ALLOWED_TOOLS = {
    "analyst": ["Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
    "auditor": ["Read", "Glob", "Grep", "Bash", "Write"],
    "automation-script-developer": [
        "Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    ],
    "manager-assistant": [
        "Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    ],
    "planner": ["Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
}

# office + role-playbook char ceiling per role; ~7% headroom over the current
# rendered size, ratchet down as P7 trims land. EVERY role carries the CTX-02
# Bash fragment (all have Bash).
_ROLE_STACK_CEILINGS = {
    "analyst": 49_000,
    # auditor 49_000→49_800 (2026-07-28, pivot-1 C-3): the shared rules'
    # ask-class carve-out (~0.3k) + the office file's four-part brief
    # contract line (C-5) — the auditor stack was already the tightest.
    "auditor": 49_800,
    "automation-script-developer": 78_000,
    "manager-assistant": 51_000,
    # 40_000→41_000 (2026-07-17, verify turn-end incident): the one-shot
    # session contract added to the shared LONG_RUNNING_BASH_RULE + the
    # Planner playbook's verify §2d (see the per-template rationale above).
    # 41_000→42_000 (2026-07-21, execution-fastlane): CANON-PLAN-CAPS +
    # single-pass-materialize default in the Planner playbook (see the
    # per-template rationale above) — ~0.5k over the old stack ceiling.
    "planner": 42_000,
}


def _role_stack_chars(name: str) -> int:
    from src.config_sync.claude_md_writer import ClaudeMdWriter

    playbook = ClaudeMdWriter._get_agent_claude_md({
        "name": name,
        "agent_type": "system",
        "allowed_tools": _ROLE_ALLOWED_TOOLS[name],
    })
    return len(_office()) + len(playbook)


def test_per_role_standing_stack_within_budget():
    over = {
        name: (_role_stack_chars(name), ceiling)
        for name, ceiling in _ROLE_STACK_CEILINGS.items()
        if _role_stack_chars(name) > ceiling
    }
    assert not over, (
        "per-role standing context (office file + role playbook) over budget — "
        "trim a shared/role section, or raise the ceiling with a rationale: "
        f"{over}"
    )


def test_per_role_stack_guard_is_not_vacuous():
    slack = {
        name: round(ceiling / max(_role_stack_chars(name), 1), 2)
        for name, ceiling in _ROLE_STACK_CEILINGS.items()
    }
    too_loose = {n: r for n, r in slack.items() if r > 1.35}
    assert not too_loose, (
        f"per-role ceiling(s) too loose (ratchet down): {too_loose}"
    )
