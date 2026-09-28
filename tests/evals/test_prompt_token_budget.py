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

2026-09-12: worker catalog 45,000→47,500 for the new executor-only human
handoff schema (readiness, expiry, secure script/variable binding), exact
credential names and durable script receipts. Existing descriptions were
trimmed; standing-template, role-stack and Manager catalog ceilings stay
unchanged. This is additional tool authority, not a larger prose allowance.
"""
from __future__ import annotations

import functools

from src.config_sync.claude_md_content import (
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.config_sync.claude_md_templates._system_agents import (
    BUILDER_CLAUDE_MD,
    DATA_CURATOR_CLAUDE_MD,
    FLOW_ARCHITECT_CLAUDE_MD,
    PLANNER_CLAUDE_MD,
)
from tests.evals._system_agent_tools import SYSTEM_AGENT_ALLOWED_TOOLS


@functools.lru_cache(maxsize=1)
def _rendered_office_and_manager() -> tuple[str, str]:
    """X43: measure the files agents actually load.

    The guard used to ``.replace()`` placeholders on the raw constants: it
    kept every ``{{``/``}}`` escape doubled, blanked ``{office_specs_index}``
    (the writer inserts the non-empty no-specs fallback) and replaced a
    placeholder that no longer exists — so it under-measured the office file
    by ~130 chars. Render through ``ClaudeMdWriter`` in a scratch workspace,
    exactly like ``render_production_manager_prompt`` does.
    """
    import tempfile
    from pathlib import Path

    from src.config_sync.claude_md_writer import ClaudeMdWriter

    with tempfile.TemporaryDirectory(prefix="cbcl-budget-") as workspace:
        writer = ClaudeMdWriter(workspace)
        writer.ensure_directory_structure()
        config = {"office_name": "Test Office"}
        writer.write_office_claude_md(config)
        writer.write_manager_claude_md(config)
        root = Path(workspace)
        return (
            (root / "CLAUDE.md").read_text(),
            (root / "agents" / "manager" / "CLAUDE.md").read_text(),
        )


def _manager() -> str:
    return _rendered_office_and_manager()[1]


def _office() -> str:
    return _rendered_office_and_manager()[0]


def _program_procedures() -> str:
    from src.config_sync.claude_md_templates._manager_modules import (
        MANAGER_PROGRAM_PROCEDURES,
    )

    return MANAGER_PROGRAM_PROCEDURES


def _flow_procedures() -> str:
    from src.config_sync.claude_md_templates._manager_modules import (
        MANAGER_FLOW_PROCEDURES,
    )

    return MANAGER_FLOW_PROCEDURES


def _flow_procedures_general_chat() -> str:
    from src.config_sync.claude_md_templates._manager_modules import (
        MANAGER_FLOW_PROCEDURES_GENERAL_CHAT,
    )

    return MANAGER_FLOW_PROCEDURES_GENERAL_CHAT


def _general_chat_procedures() -> str:
    from src.config_sync.claude_md_templates._manager_modules import (
        render_general_chat_procedures,
    )

    return render_general_chat_procedures()


def test_budget_measures_the_writer_rendered_files():
    # X43 regression: no doubled format escapes, and the real specs index.
    from src.config_sync.claude_md_writer import render_office_specs_index

    office, manager = _rendered_office_and_manager()
    assert "{{workstream_short_code}}" in SHARED_OFFICE_CLAUDE_MD
    assert "{workstream_short_code}" in office
    assert "{{" not in office and "{{" not in manager
    assert render_office_specs_index([]) in office


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
    # that auto-unblock a task; the code's AUTO_UNBLOCK_REQUEST_TYPES also
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
    # manager 74_400→75_400 (2026-09-02, office-memory v1 T3.3): the
    # "Memory, Knowledge Base and Office Files" rewrite — the context
    # ladder (brief → workstream memory → office memory → KB on explicit
    # triggers), the `remember` closed trigger list + boundary, and the
    # PROPOSED office-wide consent line — plus the memory allowlist/GC
    # lines. Partially funded by the KB-first removals (the old KB-first
    # bullet, the search_kb-before-planning step, the unconditional
    # cross-office search mandate); net +~0.8k of load-bearing memory
    # routing text, pinned by evals/test_office_memory_pins.py.
    # 2026-09-02 (office-memory final audit, ceiling unchanged): the
    # memory-vs-instructions precedence line ("On conflict, a memory
    # record wins over older office-instructions text — newer,
    # user-approved"; +111 chars inside the remaining headroom, pinned
    # by evals/test_office_memory_pins.py).
    # Configuration stewardship adds ~600 tokens for diagnosis, exact edits, consent and rollout limits.
    # Dynamic Agents v1: distinguish Profile/Agent/attempt and conditional staffing.
    # G5/G7: grouped plan ownership and typed advisory-vs-required acceptance
    # guidance add <0.9k total; no inferred extra approval round.
    # G1: explain the workstream-bound input-read receipt General Chat exclusion.
    # manager 79_400→79_200 RATCHETED DOWN (2026-09-23, X43/F01): measured
    # on the WRITER-RENDERED agents/manager/CLAUDE.md. The shared async-work
    # rule replaced the TERMINAL-at-trigger split paragraph; archive /
    # script-routing / lock-set corrections were trim-funded — rendered
    # 79,124 (main 79,294). Ceiling = rendered + ~75 so the next addition
    # needs a recorded reason (the old ceiling left 276 unexplained chars).
    # manager 79_200→65_700 RATCHETED DOWN (2026-09-23, F07 phase A): the
    # state-dependent procedures moved VERBATIM into procedure modules the
    # dynamic context injects only when relevant (program / flows / General
    # Chat — budgets below). The core gained stubs that keep the ladder's
    # FLOW and Tier-3 places plus the "Procedures loaded when relevant"
    # index. Rendered 65,469. What a session reads per context is pinned by
    # evals/test_prompt_composition.py. 65_700→65_450 (F07 compaction): the
    # Tier-3 stub's module pointer dropped (the index already says it).
    # Rendered 65,377.
    # 65_450→67_300 (2026-09-23, F07 review): two rule sets moved BACK from
    # the modules into the core because a context needs them before its
    # module loads. (1) The consult rules — one consult in flight, and
    # "Keep the user informed" (no re-announcing the Planner engagement;
    # summarise every [Planner] poke) — govern the FIRST specify consult,
    # which a default workstream makes before any program state exists
    # (~0.55k). (2) "### Flow runs" — a run outlives its flow's listing (a
    # flow disabled mid-run keeps running; the context lists active flows
    # only), so operating a live run cannot depend on the flow module
    # (~1.3k). The program and flow modules shrank by the same text
    # (budgets below). Rendered 67,229.
    # 67_300→67_200 (final review P16/P6/P11): the output rules are stated
    # once and Invariant #4 no longer promises a setup_office_secret unblock.
    # Rendered 67,133.
    # 67_200→67_450 (final review R3): Invariant #4, "paths out" and the
    # auto-decide bullet name the gates that can refuse an approval's
    # auto-unblock — the bounce cap on the Manager's own approval — and say
    # to check get_task_detail before reporting a task resumed. Rendered 67,372.
    # manager 67_450→67_800 (2026-09-24, fix wave 2 fx-prompts): Invariant #4
    # states ONE bounce-cap rule (no second agent approval; one concrete
    # change + one retry, or reject to the user — C3b-G2/C4a-G1), the
    # missing_credential secret path (C3e-G2); `research` is program-only
    # (C4a-G4); stewardship names what is not a proposal target (C1-G8).
    # Measured 67,672 (was 67,346).
    # 67_800→68_075 (2026-09-25, fz-prompts U01): Invariant #4 says a
    # person's decision is final — after they reject a bounce-cap card, or on
    # a `bounce_cap_user_decision` refusal, no retry or other unblock; tell
    # the user (+~290). Measured 67,997.
    # 68_075→68_175 (2026-09-25, f2-ai-surfaces items 18/19): the at-cap rule
    # names the current block's refusal, and a bounce_cap_user_decision
    # refusal may still be pending (+92). Measured 68,089.
    # 68_175→68_300 (2026-09-26, f5-final): Invariant #4 says the Manager
    # makes the post-rejection retry itself, since only its own refused retry
    # places the person's card (+145). Measured 68,234.
    # 68_300→68_400 (2026-09-26, fr-prompts, prompts-3): at the cap a helper
    # task resumes the task by auto-promotion, and retry_blocked_task is for a
    # brief edit or reassignment — the backend refuses a retry while the
    # helper is unfinished (+88). Measured 68,322.
    "manager": (_manager(), 68_400),
    # F07 procedure modules (``claude_md_templates/_manager_modules.py``),
    # each rendered into the Manager's dynamic system prompt only on turns
    # whose state needs it. Measured after the verbatim move: program
    # 11,621 / flows 2,927 / General Chat 2,303 (generated tool list). The
    # compaction commit then stated the Tier-3 approval rules once (they
    # were repeated in the `specify` bullet) and the FLOW TIER once (it was
    # repeated in the flow-matching paragraph): program 10,887, flows 2,455.
    # General Chat 2,303→2,369 (+66): its redirect rule now names flow-run
    # actions and flow matches — the flow module's run card is not
    # registered in General Chat, so a match there is redirected, not tried.
    # F07 review (2026-09-23): program 11_000→10_450 (the consult rules moved
    # to the core — rendered 10,407); flows 2_550→1_150 (the "Flow runs"
    # rules moved to the core — rendered 1,113); the General Chat flow
    # variant is new (rendered 519): in General Chat the workstream flow
    # module's run card is not registered, so the variant redirects instead.
    # 10_450→10_500 (final review R1): the research line says where unscoped
    # findings land (a research file the result poke names). Rendered 10,471.
    # 10_500→10_600 (2026-09-25, f2-ai-surfaces item 26): the stuck-verify
    # chip flip passes its read's read_receipt (+52). Rendered 10,549.
    # 10_600→10_700 (2026-09-26, fr-prompts, prompts-2/agent-image-0): the
    # chip flip sends the complete plan back in the same turn — the write
    # replaces the whole plan and the receipt is valid only for that turn
    # (+91). Rendered 10,640.
    "manager_program_procedures": (_program_procedures(), 10_700),
    "manager_flow_procedures": (_flow_procedures(), 1_150),
    "manager_flow_procedures_general_chat": (
        _flow_procedures_general_chat(),
        560,
    ),
    "manager_general_chat_procedures": (_general_chat_procedures(), 2_400),
    # office ceiling raised 16.0k→17.5k for the INJ-01 "Untrusted Content"
    # security directive (justified growth); P7 (CTX-02 role-split) trims it.
    # office 17_500→15_000 RATCHETED DOWN (2026-07-29, AI-quality review):
    # the ceiling sat ~26% above rendered — no regrowth pressure. Rendered is
    # ~14.2k after adding Output Style rule 5 (write for a non-technical
    # reader — plain language, say what the result MEANS, evidence after the
    # answer; pinned by evals/test_aiq_worker_pins.py).
    # Shared versioned identity contract, offset by shorter path guidance.
    # 2026-09-23 (X43, ceiling unchanged): now measured on the WRITER-RENDERED
    # file — the old .replace() measure kept `{{`/`}}` doubled and dropped the
    # no-specs fallback, under-counting by ~130 chars (main rendered 15,024).
    # The F01 lifecycle rewrite (derived move/create/update holder lines,
    # role-scoped headings, session-end fact) was trim-funded; the review
    # fixes (the Planner's update_task field subset, "(task sessions)" on
    # the scripts heading) add 44 — rendered 15,022, still net -2 against
    # the real main measurement.
    # office 15_100→15_075 RATCHETED DOWN (2026-09-23, X43): ceiling =
    # rendered + ~50 so regrowth needs a recorded reason.
    # office 15_075→15_150 (2026-09-24, fix wave 2 fx-prompts C4b-G2): the
    # workstream-spec pointer names where each phase is told to read it
    # (STEP 0.0a in execution, Phase orientation in review/triage) instead
    # of a STEP 0.0 review/triage prompts never render. Measured 15,116.
    "office": (_office(), 15_150),
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
    # 2026-09-22: +700 chars for the newly supported optional operation adapter
    # protocol and three reserved metadata names. Only the specialist receives it.
    # asd 56_700→56_850 (2026-09-24, fx-prompts): the credential rule cites
    # this file's own shell sections (C4b-G7/G11) and the shared delivery
    # rule names the task-slug file prefix (C4b-G9). Measured 56,746.
    # 56_850→57_000 (2026-09-26, f4-scripts): the notify_manager bullet says
    # an over-limit message is not delivered (only a platform notice) and to
    # send a short summary. Measured 56,937.
    # 57_000 kept (2026-09-26, f5-final): the bullet adds that a notify file
    # over 1 MiB is rejected with no notice (+56). Measured 56,993.
    "asd": (AUTOMATION_SCRIPT_DEV_CLAUDE_MD, 57_000),
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
    # 2026-09-02 (prompt-surface sweep): the default-KB process step was
    # rewritten into explicit-trigger contract text (memory-first +
    # Assigned-references gate) — net +~75 chars of load-bearing rule.
    # 33_150→32_000 (final review P8/P9/C5): the Path D section and the
    # infra-outage retry recipe became one short "after an approved
    # escalation" rule (retry_blocked_task is not a triage path); closes are
    # stated per task class. Rendered 31,825 (was 33,008).
    # 32_000→32_250 (review of P8): an approved-but-still-blocked task takes
    # Path C and escalates the remaining gate (named by the backend's
    # "Auto-unblock refused/skipped" comments) instead of an hourly no-op.
    # Rendered 32,171.
    # 32_250→32_400 (2026-09-25, f2-ai-surfaces items 18/19): the bounce-cap
    # exception names the current block, and the refusal may be pending
    # (+137). Rendered 32,335.
    # 32_400 kept (2026-09-26, f5-final): the person-decided line names Resume
    # task (+24). Rendered 32,359.
    # 32_400→32_550 (2026-09-26, fr-prompts, prompts-4): Path C's
    # escalate_blocker passes office_secret_names for missing_credential —
    # credential reconciliation closes only a named escalation (+132).
    # Rendered 32,491.
    "manager_assistant": (MANAGER_ASSISTANT_CLAUDE_MD, 32_550),
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
    # 2026-09-02 (prompt-surface sweep): step 2 "Check existing knowledge"
    # became the explicit-trigger "Check prior work" rule — net +~115
    # chars of load-bearing contract text.
    # Dynamic Agents v1: keep legitimate dependencies, retire profile scarcity.
    # 28_400→28_650 (2026-09-26, fr-prompts, prompts-2): update_execution_plan
    # replaces the whole plan, so a research note or chip flip sends back the
    # plan it read — a partial write erased the skeleton and chips (+176).
    # Rendered 28,556.
    "planner": (PLANNER_CLAUDE_MD, 28_650),
    # flow_architect + data_curator added 2026-08-26 (eval-coverage review —
    # the same omission class the builder entry records above: the budget
    # guard was missing the SEVENTH and EIGHTH system agents entirely, so
    # the two FS-P3 playbooks could grow with zero regrowth pressure).
    # Rendered today: flow_architect 11,272 / data_curator 7,548; ceilings
    # set just above per the file's ratchet methodology.
    # 11_800→11_900 (2026-09-25, f2-ai-surfaces item 26): update_flow_graph
    # passes the read_receipt of its get_flow_graph read (+118). Rendered 11,822.
    "flow_architect": (FLOW_ARCHITECT_CLAUDE_MD, 11_900),
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
# The mirror lives in tests/evals/_system_agent_tools.py, shared with the
# composition scan (test_prompt_references_reality) so both run in the
# backend-less CLI checkout; the parity test below guards it in the monorepo.
_ROLE_ALLOWED_TOOLS = SYSTEM_AGENT_ALLOWED_TOOLS

# office + role-playbook char ceiling per role; ~7% headroom over the current
# rendered size, ratchet down as P7 trims land. EVERY role carries the CTX-02
# Bash fragment (all have Bash).
_ROLE_STACK_CEILINGS = {
    # Shared Profile/Agent/attempt contract is inherited once by each role.
    # 49_350→49_750 (2026-09-24, fx-prompts): office spec pointer (+59), the
    # Analyst's conditional-credential wording (C4b-G11) and the shared
    # task-slug file prefix (C4b-G9). Measured 49,652.
    "analyst": 49_750,
    # auditor 49_000→49_800 (2026-07-28, pivot-1 C-3): the shared rules'
    # ask-class carve-out (~0.3k) + the office file's four-part brief
    # contract line (C-5) — the auditor stack was already the tightest.
    # 49_800→50_050 (2026-09-24, fx-prompts): office spec pointer (+59) and
    # the host-posted script_completed test-evidence rule (C4b-G12).
    # Measured 49,972.
    # 50_050→50_150 (2026-09-25, fw3-automation): the shared rules tell a
    # missing_credential block to name its Office Secrets in
    # office_secret_names, so saving them resumes the task (+85 net after
    # trimming the redundant "class travels" clause). Measured 50,122.
    "auditor": 50_150,
    "automation-script-developer": 78_000,
    # builder added 2026-07-29 (AI-quality review housekeeping — the stack
    # guard was missing the SIXTH system agent entirely): office file +
    # builder playbook + Bash fragment, ~41.9k rendered; ~4% headroom.
    # 43_500→43_800 (2026-09-24, fx-prompts): office spec pointer (+59), the
    # shared rules' own-section pointer (C4b-G7) and the task-slug naming
    # in the shared rules and the Builder playbook (C4b-G9). Measured 43,678.
    # 43_800→43_900 (2026-09-25, fw3-automation): the shared office_secret_names
    # rule (+85) and the hosting block naming a missing Office Secret (+56).
    # Measured 43,819.
    "builder": 43_900,
    # 51_050→49_900 (final review P8): the MA playbook trim. Rendered 49,653.
    # 49_900→50_100 (review of P8): the still-blocked Path C rule. Rendered 49,999.
    # 50_100→50_300 (2026-09-25, f2-ai-surfaces items 18/19): the MA playbook
    # growth above (+137). Rendered 50,222.
    # 50_300 kept (2026-09-26, f5-final): the Resume task mention (+24).
    # Rendered 50,246.
    # 50_300→50_450 (2026-09-26, fr-prompts, prompts-4): the Path C names
    # rule above (+132). Rendered 50,378.
    "manager-assistant": 50_450,
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
    # 45_000→45_300 (2026-09-02, prompt-surface sweep): the Planner
    # playbook's step-2 explicit-trigger rewrite + the office file's
    # reference-library Common Tool Reference reframe (both load-bearing
    # contract text; per-template rationales above).
    # Shared identity + the Planner's mode-aware assignment rules.
    # 46_300→46_450 (2026-09-26, fr-prompts, prompts-2): the playbook's
    # whole-plan write rule (+176; rationale on the template entry above).
    # Rendered 46,365.
    "planner": 46_450,
    # flow-architect + data-curator added 2026-08-26 (eval-coverage review —
    # the builder-omission precedent above, again): office file + role
    # playbook + Bash fragment. Rendered stacks today: flow-architect
    # ~28.3k, data-curator ~24.6k; ~4% headroom keeps regrowth pressure.
    # flow-architect 29_500→29_650 (2026-09-25, f2-ai-surfaces item 26): the
    # read_receipt line (+118). Rendered 29,607.
    "flow-architect": 29_650,
    "data-curator": 25_500,
}


def test_role_allowed_tools_mirror_the_backend_system_agents():
    # X43: the stack budget must measure the Bash fragment each role really
    # receives; a drifted mirror under- or over-states the standing bill.
    from tests.backend_boundary import import_backend

    defaults = {
        agent["name"]: sorted(agent["allowed_tools"])
        for agent in import_backend(
            "app.agents.system_agents"
        ).SYSTEM_AGENT_DEFAULTS
    }
    assert {
        name: sorted(tools) for name, tools in _ROLE_ALLOWED_TOOLS.items()
    } == defaults


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
    # manager catalog 68_000→70_500 (2026-09-02, office-memory v1 T3.1):
    # the two memory tools — recall (~1.3k, re-voiced worker schema) and
    # remember (~2.2k: the closed trigger list, the office_wide→PROPOSED
    # consent shape, and the collections/flows/KB boundary are the
    # tool's whole authority story) — partially offset by the KB
    # description rewrites. Rendered ~69.9k; pins in
    # evals/test_office_memory_pins.py. The worker pool absorbed recall
    # inside its existing headroom (~44.8k of 45_000).
    return {
        # Two bounded Manager-only configuration tools, including typed change-set schema.
        # 2026-09-21: bounded resource declarations on existing create/update
        # task tools (array/null distinction, validation and live-write guard).
        # 2026-09-21 audit: the same reservation schema must also reach
        # scheduled assignment creation/replacement; otherwise recurring
        # work cannot declare its shared external resources. Two nested
        # schemas add ~1k characters, without adding tools or role authority.
        # G5: exact optional plan schema in create/update and scheduled creation/
        # replacement. Conditional automation freshness cannot promise an invalid plan.
        # 2026-09-23 (AI-quality wp-tools, X41/X57): 83_050→87_500. The
        # description linter now walks NESTED parameters; ~70 model-filled
        # fields shipped undescribed (verification_plan.checks[].* in four
        # tools, update_task.brief.* repair fields, propose_configuration
        # changes[].*), plus the run_flow `materials` carrier. Measured
        # 86,443 (was 82,882); descriptions kept terse. Review follow-up:
        # update_task.brief.{inputs,acceptance_criteria,verification_steps}
        # now carry create_task's FULL guidance behind a replacement prefix
        # (+698) — measured 87,141, still under the ceiling.
        # 2026-09-24 (fix wave 2 fx-prompts): 87_500→88_250. create_scope's
        # order routes milestone tasks through materialize (C4a-G6);
        # propose_configuration states the field length limits and the
        # non-targets (C1-G6/C1-G8); decide_action_request says a rejected
        # blocker request re-routes to the user; research is program only.
        # Measured 88,103 (was 87,479).
        # 2026-09-25 (fx-prompts, fx-platform follow-up): 88_250→88_400. The
        # backend pages long KB documents (offset/next_offset); the Manager's
        # get_kb_document gains the `offset` parameter the worker schema
        # already has (+122). Measured 88,225.
        # 2026-09-25 (fz-mcp-delivery, U15/L01/L03): 88_400→89_900. The four
        # large reads (get_task_detail, get_spec, get_execution_plan,
        # inspect_configuration) share the `<persisted-output>` preview
        # warning and the `section`/`offset` parameters that continue a
        # shortened read (+~340 each). Measured 89,735 (was 88,384).
        # 2026-09-25 (fz-mcp-delivery repair, R12/L03): 89_900→90_300.
        # get_spec/get_execution_plan name the `read_receipt` that ends a
        # complete result, update_execution_plan takes it, and each section
        # example names a real part of its tool's result (+478). Measured
        # 90,213.
        # 2026-09-25 (fz-prompts U01, ceiling unchanged, -23 net):
        # retry_blocked_task names the `bounce_cap_user_decision` refusal and
        # no longer claims it always resets blocked_bounce_count.
        # Merge of fz-mcp-delivery + fz-prompts: re-measured 90,190 (ceiling
        # 90_300).
        # 2026-09-26 (fr-prompts, prompts-2/agent-image-0): 90_300→90_500.
        # update_execution_plan says each call replaces the whole plan and
        # that the Manager's read_receipt covers only this turn's reads; the
        # receipt parameter says the same (+169). Measured 90,402.
        "manager": (_catalog_chars(get_manager_tools()), 90_500),
        # 2026-09-22: four real optional operation read/reconcile/cancel schemas
        # and execute_script.operation add ~2.6k chars; no existing prose expansion.
        # G5/G7: typed receipt/status, full evidence identities, plan/Done fields
        # and explicit advisory intent; every callable parameter is described.
        # G2: optional operation.stage adds ~180 chars of timing-only metadata.
        # 2026-09-23 (AI-quality wp-tools): 56_200→57_500 — nested
        # descriptions (verification_plan, execute_script.operation) and the
        # corrected escalate_blocker / add_activity / recall / criterion_index
        # contracts (X04/X41/X63/X66/X67). Measured 57,275 (was 56,155).
        # 2026-09-25 (fw3-automation): 57_500→57_750. update_status gains the
        # optional office_secret_names array a missing_credential block uses
        # so saving the named secrets resumes the task (+201); the
        # bind_script_variable 400 note names the field (+13). Measured 57,681.
        # 2026-09-25 (fz-mcp-delivery): 57_750→58_500. get_my_brief and
        # get_task_detail carry the preview warning and `section`/`offset`
        # (+724). Measured 58,405.
        # 2026-09-25 (fz-credentials-resume, L05): 58_500→58_700. move_task
        # gains the same optional office_secret_names array, so a reviewer's
        # ONE missing_credential block names its secrets (+234). Merged with
        # fz-mcp-delivery, re-measured 58,639.
        "worker_pool": (_catalog_chars(get_worker_tools()), 58_700),
        # 2026-09-23 (AI-quality wp-tools, X41): 32_000→33_800 — the
        # Planner shares create_task/update_task, whose nested
        # verification_plan + brief-repair fields are now described.
        # Measured 33,568 (was 31,907). Review follow-up 33_800→34_500: the
        # Planner repairs materialized briefs, so its update_task.brief
        # inputs / acceptance_criteria / verification_steps carry
        # create_task's full verbatim-request, checkable-criteria and
        # evidence-reuse guidance (+698). Measured 34,266.
        # 34_500→34_300 (final review R2): the Planner's own create_task and
        # the consult search_kb voice (no `recall` / `list_files` it lacks)
        # are shorter than the Manager/worker text. Measured 34,149.
        # 2026-09-25 (fx-prompts): 34_300→34_450. The Planner shares the
        # Manager's get_kb_document, which gains the `offset` parameter for
        # paged KB documents (+139). Measured 34,288.
        # 2026-09-25 (fz-mcp-delivery): 34_450→35_450. The Planner shares the
        # Manager's get_task_detail, get_spec and get_execution_plan, now with
        # the preview warning and `section`/`offset` (+1,086). Measured 35,371.
        # 2026-09-25 (fz-mcp-delivery repair, R12/L03): 35_450→36_200. The
        # read-receipt sentences on get_spec/get_execution_plan and the
        # `read_receipt` parameter of update_spec/update_execution_plan
        # (+739). Measured 36,110.
        # 2026-09-26 (fr-prompts, prompts-2/agent-image-0): 36_200→36_400.
        # The shared update_execution_plan whole-plan rule and the receipt
        # parameter's turn scope, on update_execution_plan and update_spec
        # (+190). Measured 36,300.
        # 36_400→36_300 (2026-09-26, fr-prompts, agent-image-2): the Planner's
        # get_kb_document takes the shorter consult voice (no Brief, memory
        # or board as working context) (-96). Measured 36,204.
        "planner": (_catalog_chars(get_planner_tools()), 36_300),
        # R2: the consult search_kb voice — FA 10,658, DC 7,554 measured.
        # 2026-09-25 (fz-mcp-delivery): 11_000→11_150. get_flow_graph's
        # preview sentence became the shared warning plus `section`/`offset`
        # (+216). Measured 11,024.
        # 2026-09-25 (fz-mcp-delivery repair, R12): 11_150→11_450. get_flow_graph
        # names the `read_receipt` update_flow_graph must pass, which gains
        # that parameter (+364). Measured 11,388.
        "flow_architect": (_catalog_chars(get_flow_architect_tools()), 11_450),
        "data_curator": (_catalog_chars(get_data_curator_tools()), 7_800),
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
