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
    DATA_CURATOR_CLAUDE_MD,
    FLOW_ARCHITECT_CLAUDE_MD,
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
    # manager 74_200→74_400 (2026-08-19, I-7 review): System Invariant #4
    # named only escalate_blocker / request_clarification as the approvals
    # that auto-unblock a task; the code's _AUTO_UNBLOCK_REQUEST_TYPES also
    # carries setup_office_secret, and approving any OTHER type leaves the
    # task blocked. An incomplete entry in a section titled "current platform
    # truths (read EVERY turn)" is worse than a long one — it teaches the
    # Manager to expect a task to stay blocked when the platform will move it.
    # +109 chars, and NOT paid for by a trim: a paragraph-similarity sweep of
    # the whole playbook found a maximum Jaccard overlap of 0.26 between any
    # two paragraphs, i.e. no duplicate prose left to cut. That measurement
    # also retires I-7's premise ("3-5x duplication of every load-bearing
    # rule") — true when it was written, not true of this file today.
    # manager 71_500→71_350 RATCHETED DOWN (2026-07-29, AI-quality review —
    # Manager-surface fixes): the review both ADDED (~2.9k — the "Your voice"
    # reply canon, composite-request classification, the Tier-0/1/2
    # course-correction recipe, program-completion step 6, the milestone
    # short_key linking rule, the cost floor, cross-office KB reuse,
    # queue-depth roster note; pinned by evals/test_aiq_manager_pins.py) and
    # CUT MORE (~3.1k — the Turn Lifecycle section deflated to the
    # synthetic-turns-carry-their-own-instructions rule, the re-grown per-type
    # auto-decide mini-table removed per T5.3.1, the Blocked-tasks subsection
    # deduped against System Invariant #4). Net -180 rendered; ceiling set to
    # rendered + ~300 to keep regrowth pressure.
    # 2026-07-31 (pivot-3 P2-2/P2-7, ceiling unchanged): the Standing
    # Operations block (schedules-never-tracker-tasks routing, the autonomy
    # frame + draft-mode outbound, the digest offer), the four
    # assignment-schedule tools' allowlist lines, and the event-thread op
    # line (~2.2k of new routing copy) were FULLY trim-funded: Auth-example /
    # script-rationale / detection-list compression, the async-trigger and
    # split-reroute paragraphs tightened, gap-awareness + archive/delete +
    # cancel sections deflated. Rendered ~71.3k — deliberately near the
    # ceiling; pins in evals/test_pivot3_pins.py.
    # 2026-07-31 (pivot-3 review F10, ceiling unchanged): the time-vs-event
    # standing-work reconcile line (~0.2k) was trim-funded from the Inbound
    # Events paragraph (litmus compressed, the one-way-channel tail
    # tightened — the pinned sentences survive verbatim).
    # 2026-08-03 (pivot-4 flow-intake T19, ceiling unchanged): the new
    # "## Flows & intake" section (~2.0k — flow selection per turn,
    # derive-first, card mechanics, topics→records, amend-over-reask, the
    # define_flow consent rule, re-read-never-assume-staleness; pinned by
    # evals/test_flow_intake_pins.py) + the 3-tool allowlist/GC-strip
    # growth (~0.2k) were FULLY trim-funded: System Invariants #1-#3
    # compressed (register_script / source-edit / notify_manager bodies),
    # the Context-Locking mid-turn paragraph and [Script:] callback
    # paragraph tightened (mini-IDE line dropped), inactivity-timeout
    # bullets folded to one sentence, Compaction guidance deflated to one
    # PRESERVE/DROP paragraph, script-delegation + why-non-negotiable
    # compressed. Rendered 71,341 — 9 under the ceiling; every pinned
    # sentence survives verbatim (442 eval-family tests green).
    # manager 71_350→71_450 (2026-08-03, program review #19): the PRIMARY
    # intake recipe ("Intake — collect before you build") now teaches the
    # call shape WITH the backend-REQUIRED `topic` param — ~50 chars of
    # refusal-round-trip prevention the 9-char headroom could not absorb;
    # pinned by evals/test_flow_intake_pins.py (bounded-slice pin) +
    # evals/test_pivot3_pins.py.
    # manager 71_450→74_200 (2026-08-05, Flow Studio FS-P2.T9): the FLOW
    # TIER checked FIRST in the right-sizing ladder (~0.7k — the whole
    # point of runnable flows: deterministic engine runs beat hand-routed
    # ladders for registered work), the "## Flow runs" operate-never-
    # design section (~1.5k — start/stop/get surface, the never-edit-
    # definitions rule, one-run-per-workstream, amend-via-flow_run_id),
    # the runnable-vs-prose split in "## Flows & intake", the GC-strip
    # line, and the 3 allowlist lines. Pinned by
    # evals/test_flow_studio_pins.py; ceiling = rendered (73.9k) + ~300.
    "manager": (_manager(), 74_400),          # ~18.6k tok; 74.3k rendered now
    # office ceiling raised 16.0k→17.5k for the INJ-01 "Untrusted Content"
    # security directive (justified growth); P7 (CTX-02 role-split) trims it.
    # office 17_500→15_000 RATCHETED DOWN (2026-07-29, AI-quality review):
    # the ceiling sat ~26% above rendered — no regrowth pressure. Rendered is
    # ~14.2k after adding Output Style rule 5 (write for a non-technical
    # reader — plain language, say what the result MEANS, evidence after the
    # answer; pinned by evals/test_aiq_worker_pins.py).
    "office": (_office(), 15_000),            # ~3.6k tok; 14.2k rendered now
    # shared_agent 19_500→20_000 (2026-07-21, execution-fastlane canon): the
    # CANON-ARTIFACT-CAP hard cap (≤3 artifacts) + CANON-LENGTH bounds
    # (≤2-page deliverables / ≤3-line checkpoints / ≤30-line verdicts) +
    # CANON-PLAN-CAPS in PLANNER_WORK_RULES — small net growth (~54 chars
    # over) after the saved-report-file removals.
    # 20_000→20_500 (2026-07-28, pivot-1 C-3): the ask-class exception to
    # submit-for-review (~0.3k chars — any executor can draw an ask task;
    # pinned by evals/test_pivot1_pins.py ask-carveout pin).
    # 2026-07-29 (AI-quality review, ceiling unchanged): the ~1.5k of trims —
    # "When You Are a Reviewer" collapsed to a pointer at the task-prompt
    # DESIGNATED REVIEWER block (its near-duplicate dangerously lacked the
    # rework-cap escalation branch) + the script-STOP five-signal list
    # compressed — funded the fat-build .py carve-out and the
    # published-collections KB line (both pinned by
    # evals/test_aiq_worker_pins.py). Rendered ~19.1k.
    # 2026-07-31 (pivot-3 review F9b, ceiling unchanged): the Outbound
    # DRAFT MODE worker bullet (~0.4k — draft rides request_clarification,
    # send EXACTLY the approved draft; pinned by evals/test_pivot3_pins.py)
    # was trim-funded (~0.5k: prior-work dedup line, save_file fallback
    # dedup vs Tool Error Handling #5, the readable_id convenience tail) so
    # the tight auditor STACK ceiling holds too. Rendered ~19.4k.
    "shared_agent": (SHARED_AGENT_WORK_RULES, 20_500),
    "analyst": (ANALYST_CLAUDE_MD, 32_000),
    # auditor 32_000→32_500 (2026-07-21, execution-fastlane): the
    # conditional-report posture (report file ONLY on FAIL/CONDITIONAL or a
    # brief-requested artifact) stated at each completion flow — ~0.2k chars.
    # 32_500→33_000 (2026-07-28, pivot-1 C-3): inherits the shared rules'
    # ask-class carve-out (~0.3k chars — see shared_agent above).
    # 2026-07-29 (AI-quality review, ceiling unchanged): the depth dial
    # (right-size to the brief's Verification Steps — smoke checks stay
    # smoke-sized) + the fat-build product-source exception to the hidden-
    # script FAIL landed inside the headroom the inherited shared-rules trims
    # freed. Rendered ~32.4k; pins in evals/test_aiq_worker_pins.py.
    "auditor": (AUDITOR_CLAUDE_MD, 33_000),
    # asd ratcheted 60_000→56_000 (2026-07-21, execution-fastlane): the
    # main.py reference collapsed to a ~30-line skeleton + dedups landed
    # (~52.4k rendered now) — keep the guard's regrowth pressure.
    "asd": (AUTOMATION_SCRIPT_DEV_CLAUDE_MD, 56_000),  # ~12.8k tok
    # builder (pivot-1 T1): deliberately LEAN — ~4.5k own chars + the shared
    # rules (~17.6k). The Builder's value is executing, not reading playbook;
    # keep regrowth pressure on it.
    # 2026-07-29 (AI-quality review, ceiling unchanged): three delivery
    # sections landed in the free headroom — "Deliver it like a product, not
    # a repo" (non-technical reader, RUN.md, zero-setup tech), "Where a
    # multi-file build lives" (ONE project dir, ONE registered artifact),
    # "Verify with commands, not confidence" (exit-0 evidence + honest
    # not-verified list + never simulate a deploy; replaces the old "Verify
    # before you submit"). Rendered ~24.8k; pins in
    # evals/test_aiq_worker_pins.py.
    "builder": (BUILDER_CLAUDE_MD, 26_000),
    # ceiling raised 30.0k→33.0k for CTX-06: the MA is the direct-Bash
    # verification agent but did NOT load SHARED_AGENT_WORK_RULES, so it lacked
    # (a) the safety-critical no-blocking-Bash rule (Tier-2 session-churn fix)
    # and (b) the ESCALATED blocker template it cites ("see your shared work
    # rules" was dangling). It now appends the two shared constants it needs
    # (LONG_RUNNING_BASH_RULE + BLOCKED_ESCALATION_TEMPLATE, ~3k chars) rather
    # than the whole ~18k playbook.
    # 2026-07-29 (AI-quality review, ceiling unchanged): Action S (the MA-run
    # SMOKE review the Manager's Tier-1b flow promises) + the tool-error rule
    # + the Role-1 artifact-boundary pointer were funded by in-playbook trims
    # (last-resort-fallback bullet, infra-outage intro, board-overview intro,
    # triage-step redundancy). Rendered ~32.9k — deliberately near the
    # ceiling; pins in evals/test_aiq_worker_pins.py.
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
    # Raised 25_500→28_000 (2026-07-29, pivot-2 AI-quality review): the
    # materialize two-entry-state branch (single-pass compressed planning),
    # the single 6+/open-questions two-pass threshold, milestone
    # judgeability (approver-checkable endpoints), chip quality (observable
    # evidence, ≥1 per covered REQ — the verify gate's teeth), the
    # final-milestone no-deferred rule, the write-for-the-approver spec
    # bullet + verbatim-request/References alignment with update_spec, the
    # expert-boundary task-sizing bar, and the evidence-shaped coverage_map
    # example — ~2.3k chars of load-bearing planning-quality rules (pinned
    # by evals/test_aiq_planner_pins.py), partly offset by deduping the
    # specify-mode bullet against the "Specify first" section.
    "planner": (PLANNER_CLAUDE_MD, 28_000),
    # flow_architect + data_curator added 2026-08-26 (eval-coverage review —
    # the same omission class the builder entry records above: the budget
    # guard was missing the SEVENTH and EIGHTH system agents entirely, so
    # the two FS-P3 playbooks could grow with zero regrowth pressure).
    # Rendered today: flow_architect 11,272 / data_curator 7,548; ceilings
    # set just above per the file's ratchet methodology.
    "flow_architect": (FLOW_ARCHITECT_CLAUDE_MD, 11_800),
    "data_curator": (DATA_CURATOR_CLAUDE_MD, 8_000),
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
# SYSTEM_AGENT_DEFAULTS). ALL EIGHT system agents (incl. the Builder,
# pivot-1 T1, and the Flow Architect + Data Curator, Flow Studio FS-P3)
# currently ship WITH Bash ("platform policy: every agent can run
# commands"), so each receives the BASH_CAPABILITY_RULES fragment. (An earlier
# version of this eval wrongly gave analyst + planner NO Bash — and omitted
# the builder stack entirely — understating the real per-role stacks; a later
# version repeated the omission for the two FS-P3 agents, fixed 2026-08-26.)
_ROLE_ALLOWED_TOOLS = {
    "analyst": ["Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
    "auditor": ["Read", "Glob", "Grep", "Bash", "Write"],
    "automation-script-developer": [
        "Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    ],
    "builder": [
        "Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    ],
    "manager-assistant": [
        "Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch",
    ],
    "planner": ["Read", "Write", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"],
    # FS-P3 (mirrors SYSTEM_AGENT_DEFAULTS): the Architect writes templates
    # via the filesystem (Write); the Curator's writes go through the gated
    # collection tools, so its CLI toolset carries NO Write.
    "flow-architect": ["Read", "Write", "Bash", "Glob", "Grep"],
    "data-curator": ["Read", "Glob", "Grep", "Bash"],
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
    # builder added 2026-07-29 (AI-quality review housekeeping — the stack
    # guard was missing the SIXTH system agent entirely): office file +
    # builder playbook + Bash fragment, ~41.9k rendered; ~4% headroom.
    "builder": 43_500,
    "manager-assistant": 51_000,
    # 40_000→41_000 (2026-07-17, verify turn-end incident): the one-shot
    # session contract added to the shared LONG_RUNNING_BASH_RULE + the
    # Planner playbook's verify §2d (see the per-template rationale above).
    # 41_000→42_000 (2026-07-21, execution-fastlane): CANON-PLAN-CAPS +
    # single-pass-materialize default in the Planner playbook (see the
    # per-template rationale above) — ~0.5k over the old stack ceiling.
    # 42_000→45_000 (2026-07-29, pivot-2 AI-quality review): the planner
    # playbook grew ~2.3k (materialize entry states, milestone judgeability,
    # chip quality, final-milestone rule, approver-oriented spec, sizing
    # bar — see the per-template rationale above); the old pin sat 3 chars
    # under (41,997/42,000), so the whole growth lands on the stack.
    # Rendered ~44.7k now; ~300 headroom keeps regrowth pressure.
    "planner": 45_000,
    # flow-architect + data-curator added 2026-08-26 (eval-coverage review —
    # the builder-omission precedent above, again): office file + role
    # playbook + Bash fragment. Rendered stacks today: flow-architect
    # ~28.3k, data-curator ~24.6k; ~4% headroom keeps regrowth pressure.
    "flow-architect": 29_500,
    "data-curator": 25_500,
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


# ── The MCP tool catalog is a standing prompt surface too ──────────────────
#
# Added 2026-08-26 (eval-coverage review): tool DESCRIPTIONS are prompts —
# every role's serialized catalog is delivered to EVERY session for that role
# via --mcp-config, exactly like the CLAUDE.md templates above, yet nothing
# pinned its size (test_tool_catalog_drift.py pins NAME sets only; the
# description evals pin CONTENT claims). The Manager catalog serializes to
# ~66k chars (~16.5k tok) — comparable to the whole ratcheted Manager
# playbook — and had grown release over release unnoticed (the BP-02 class).
#
# Measured deterministically: sum of json.dumps(tool, ensure_ascii=False,
# sort_keys=True) over the role's catalog. Same ratchet discipline as
# _BUDGETS: raise a ceiling only in the same commit as the growth, with a
# rationale; ratchet down when trims land.


def _catalog_chars(tools: list[dict]) -> int:
    import json

    return sum(
        len(json.dumps(t, ensure_ascii=False, sort_keys=True)) for t in tools
    )


def _catalog_budgets() -> dict[str, tuple[int, int]]:
    from src._agent_image._mcp.tools_data_curator import get_data_curator_tools
    from src._agent_image._mcp.tools_flow_architect import (
        get_flow_architect_tools,
    )
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_planner import get_planner_tools
    from src._agent_image._mcp.tools_worker import get_worker_tools

    # role -> (serialized chars, char ceiling). Rendered today (2026-08-26):
    # manager 66,135 / worker pool 43,332 / planner 28,642 /
    # flow_architect 10,781 / data_curator 7,677. The worker POOL is the
    # superset every sub-catalog filters from, so pinning it covers the
    # executor/reviewer/MA surfaces.
    return {
        "manager": (_catalog_chars(get_manager_tools()), 68_000),
        "worker_pool": (_catalog_chars(get_worker_tools()), 45_000),
        "planner": (_catalog_chars(get_planner_tools()), 30_000),
        "flow_architect": (_catalog_chars(get_flow_architect_tools()), 11_500),
        "data_curator": (_catalog_chars(get_data_curator_tools()), 8_200),
    }


def test_tool_catalogs_within_char_budget():
    over = {
        name: (chars, ceiling)
        for name, (chars, ceiling) in _catalog_budgets().items()
        if chars > ceiling
    }
    assert not over, (
        "tool catalog(s) over budget — trim descriptions, or raise the "
        f"ceiling in the same commit with a rationale: {over}"
    )


def test_tool_catalog_guard_is_not_vacuous():
    slack = {
        name: round(ceiling / max(chars, 1), 2)
        for name, (chars, ceiling) in _catalog_budgets().items()
    }
    too_loose = {n: r for n, r in slack.items() if r > 1.35}
    assert not too_loose, (
        f"catalog ceiling(s) too loose to catch regrowth (ratchet down): "
        f"{too_loose}"
    )


# One description must not silently absorb the whole role budget: the largest
# tool today is ask_user_choice at ~11.6k chars (it carries five card kinds'
# parameter contracts). Ceiling just above — a tool that outgrows it either
# gets trimmed or splits its contract, deliberately.
_SINGLE_TOOL_CEILING = 12_200


def test_no_single_tool_dominates_the_catalog():
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from src._agent_image._mcp.tools_worker import get_worker_tools
    import json

    over = {
        t["name"]: len(json.dumps(t, ensure_ascii=False, sort_keys=True))
        for t in get_manager_tools() + get_worker_tools()
        if len(json.dumps(t, ensure_ascii=False, sort_keys=True))
        > _SINGLE_TOOL_CEILING
    }
    assert not over, (
        "tool description(s) over the single-tool ceiling — trim, split, or "
        f"raise _SINGLE_TOOL_CEILING with a rationale: {over}"
    )
