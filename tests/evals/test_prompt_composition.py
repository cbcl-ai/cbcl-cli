"""F07 — composed-prompt budgets: what each session context actually loads.

``test_prompt_token_budget`` pins every CLAUDE.md file and tool catalog on its
own. A session pays for their COMPOSITION: the shared office file, its role
file, the per-turn dynamic system prompt (Manager) or task prompt (workers),
and the serialized MCP catalog registered for that context. These guards pin
that composed bill per representative context, rendered from production code
(``tests/evals/_prompt_composition.py``), so a change that moves text between
surfaces is measured where the model reads it.

Characters, not tokens (deterministic; ~chars/4 is only a rough estimate) —
no latency or cost claim follows from these numbers.

Baseline before F07 (2026-09-23, main 649b7198), text = files + dynamic/task
prompt, total = text + serialized catalog:

| context             | office | role file | dynamic/task | catalog | total   |
|---------------------|--------|-----------|--------------|---------|---------|
| Manager GC          | 15,057 | 79,181    |          831 |  19,980 | 115,049 |
| Manager default ws  | 15,057 | 79,181    |        2,238 |  87,243 | 183,719 |
| Manager program ws  | 15,057 | 79,181    |        1,904 |  87,243 | 183,385 |
| Manager program+fl. | 15,057 | 79,181    |        2,324 |  87,243 | 183,805 |
| Worker execute      | 15,057 | 28,364    |       20,161 |  38,390 | 101,972 |
| Worker review       | 15,057 | 34,536    |       17,444 |  36,557 | 103,594 |
| Worker triage (MA)  | 15,057 | 35,779    |        7,887 |  47,082 | 105,805 |

Same ratchet discipline as the per-template budgets: raise a ceiling only in
the same commit as the growth, with a reason; ratchet down when trims land;
the anti-vacuity guard keeps every ceiling within 1.35x of the measured size.
"""

from __future__ import annotations

import pytest

from tests.evals._prompt_composition import (
    MANAGER_CONTEXTS,
    WORKER_PHASES,
    WORKER_SESSIONS,
    compose_manager,
    compose_worker,
    compose_worker_session,
    norm,
)

# context -> (text ceiling, total ceiling). Text = files + dynamic/task
# prompt; total adds the serialized catalog registered for the context.
# F07 phase A (verbatim move) ratcheted every Manager context down except
# program+flows, which carries every module at once; the compaction commit
# stated the Tier-3 approval rules and the FLOW TIER once each, bringing it
# below its pre-F07 baseline too (96,562 -> 96,119 text).
# F07 review (2026-09-23): the consult rules and the "Flow runs" rules
# moved back into the core (a default workstream makes the first specify
# consult; a live run can outlive its flow's listing), +1,852 text on every
# context whose module did not already carry them: General Chat 85,487,
# default 84,524, program 94,615 — all still below their pre-F07 baselines.
# program+flows is unchanged in substance (96,149). General Chat with flows
# is a new representative context (86,427): it loads the redirect flow
# variant instead of the workstream flow module.
# Final review (2026-09-23): text down — P16 stated the Manager output rules
# once (the office contract owns Markdown/length), General Chat names
# get_action_request once, P6/P11/P13/C13 corrected in place, the flow module
# intro trimmed. Totals up by the catalog: P15 gave the Manager/Planner
# create_task its when-not-to-use clause (+~236). Measured text/total:
# General Chat 85,346/105,326, General Chat+flows 86,286/106,266, default
# 84,428/171,907, program 94,524/182,003, program+flows 96,070/183,549.
# R1 added +59 to the program module's research line; R3 then named the
# auto-unblock gates on the Manager's own approvals in the core (+239).
# Measured text/total: General Chat 85,585/105,565, General Chat+flows
# 86,525/106,505, default 84,667/172,146, program 94,822/182,301,
# program+flows 96,368/183,847.
# Current measurement (2026-09-24, integration with all eight packages
# merged; the figures above are the history of each step): General Chat
# 85,559/105,539, General Chat+flows 86,499/106,479, default
# 84,641/172,120, program 94,796/182,275, program+flows 96,342/183,821.
# Fix wave 2 fx-prompts (2026-09-24): the core and catalog growth recorded
# in test_prompt_token_budget.py (Invariant #4 bounce-cap/secret rules,
# stewardship non-targets, create_scope order, configuration limits).
# Measured text/total: General Chat 85,885/106,098, General Chat+flows
# 86,825/107,038, default 84,976/173,079, program 95,148/183,251,
# program+flows 96,694/184,797.
# 2026-09-25 (fx-prompts): totals +150 — the Manager's get_kb_document
# gains `offset` for paged KB documents (+122 catalog); the text is unchanged.
# Measured text/total: General Chat 85,971/106,306, General Chat+flows
# 86,911/107,246, default 85,062/173,287, program 95,234/183,459,
# program+flows 96,780/185,005.
# 2026-09-25 (fw4-comm, R12): totals +100 — inspect_configuration warns that a
# `<persisted-output>` preview is not the result (the CLI's remote-gated
# per-message budget can swap parallel large reads; +97 catalog); the text is
# unchanged. Measured text/total: General Chat 85,967/106,399, General
# Chat+flows 86,902/107,334, default 85,058/173,429, program 95,230/183,601,
# program+flows 96,771/185,142.
# 2026-09-25 (fz-mcp-delivery, U15/L01/L03): totals +1,351 — the four
# Manager large reads (get_task_detail, get_spec, get_execution_plan,
# inspect_configuration) each carry the shared preview warning and the
# `section`/`offset` parameters that continue a shortened read; the text is
# unchanged. Measured text/total: General Chat 85,980/107,763, General
# Chat+flows 86,915/108,698, default 85,071/174,806, program
# 95,243/184,978, program+flows 96,784/186,519.
# 2026-09-25 (fz-mcp-delivery repair, R12/L03): totals +185 (General Chat)
# / +478 (workstreams) — get_spec and get_execution_plan say a complete
# result ends with the `read_receipt` a write-back must pass, and
# update_execution_plan (stripped in General Chat) gains that `read_receipt`
# parameter; each section example names a real part of its own result. The
# text is unchanged. Measured text/total: General Chat 85,980/107,948,
# General Chat+flows 86,915/108,883, default 85,071/175,284, program
# 95,243/185,456, program+flows 96,784/186,997.
# 2026-09-25 (fz-prompts U01): text +302 — Invariant #4 says a person's
# decision on a blocked task is final (a rejected bounce-cap card or a
# `bounce_cap_user_decision` refusal: no retry or other unblock); the
# retry_blocked_task description names that refusal and drops its
# always-reset claim (-23 catalog; the General Chat catalog omits the tool).
# Measured text/total: General Chat 86,269/106,701, General Chat+flows
# 87,204/107,636, default 85,360/173,721, program 95,532/183,893,
# program+flows 97,073/185,434.
# 2026-09-25 (merge of fz-mcp-delivery + fz-prompts): both changes together,
# re-measured. Text/total: General Chat 86,269/108,237, General Chat+flows
# 87,204/109,172, default 85,360/175,550, program 95,532/185,722,
# program+flows 97,073/187,263.
# 2026-09-25 (f2-ai-surfaces items 18/19/26): text +92 (Invariant #4 names the
# current block's refusal and a still-pending bounce_cap_user_decision) and
# +52 in the program module (the stuck-verify read_receipt). Text/total:
# General Chat 86,361/108,329, General Chat+flows 87,296/109,264, default
# 85,452/175,642, program 95,676/185,866, program+flows 97,217/187,407.
# 2026-09-26 (f5-final): text +145 — Invariant #4 says the Manager makes the
# post-rejection retry itself, since only its own refused retry places the
# person's card. Text/total: General Chat 86,506/108,474, General Chat+flows
# 87,441/109,409, default 85,597/175,830, program 95,821/186,054,
# program+flows 97,362/187,595.
# 2026-09-26 (fr-prompts, prompts-2 / agent-image-0): catalog +169 —
# update_execution_plan says each call replaces the whole plan (a partial
# write erased the task breakdown and chips) and that the Manager's
# read_receipt covers only the current turn's reads; program module +91 —
# the stuck-verify chip flip sends the complete plan back in the same turn.
# General Chat omits the tool and the module, so it is unchanged. Text/total:
# default 85,597/175,999, program 95,912/186,314, program+flows
# 97,453/187,855.
# 2026-09-26 (fr-prompts, prompts-3): text +88 — Invariant #4 no longer pairs
# a helper task with retry_blocked_task (the retry is refused while the
# helper is unfinished; its completion auto-promotes the task). Text/total:
# General Chat 86,594/108,562, General Chat+flows 87,529/109,497, default
# 85,685/176,087, program 96,000/186,402, program+flows 97,541/187,943.
_MANAGER_CEILINGS: dict[str, tuple[int, int]] = {
    "general_chat": (86_700, 108_650),
    "general_chat_flows": (87_550, 109_500),
    "default_workstream": (85_700, 176_100),
    "program_workstream": (96_100, 186_500),
    "program_flows": (97_550, 187_950),
}
# F07 phase C: the execute task prompt no longer repeats the session-end
# fact (office file) or the script-handoff core (role file): 20,161 -> 19,897.
# Final review T9 (2026-09-23): the role file is now the task-Agent CLAUDE.md
# a board session really loads (``compose_task_agent_claude_md``) — +491 for
# the retained-configuration note and +4,886 for an Office work policy at its
# 4,000-character cap — and the stdin user turn is measured (+97..+144, P1).
# P10 dropped the executor checkpoint guidance from review/triage (-804).
# Measured text/total: execute 68,888/107,278 (was 63,318/101,708), review
# 71,830/108,387 (was 67,037/103,594), triage 63,440/110,522 (was
# 58,723/105,805). Final review P8 then replaced the MA's Path D section
# (retry_blocked_task is not a triage path), and review/triage now state the
# admitted task output directory (+95) that P10's removed checkpoint example
# used to carry; the shared context ladder names the work policy and the
# Workstream Instructions (+50, C4 follow-up): execute 68,832/107,198,
# review 71,869/108,402, triage 62,329/109,387.
# Current measurement (2026-09-24, integration with all eight packages
# merged): execute 68,966/107,394, review 71,869/108,464, triage
# 62,675/109,795.
# Fix wave 2 fx-prompts (2026-09-24): the office spec pointer (+59), the
# shared rules' own-section pointer, the Auditor's host-posted test-evidence
# rule and the worker catalog's list_script_executions correction; then the
# task-slug naming rule and mode-aware orphan search (C4b-G9) and the two-call
# ask close (C4b-G10). Measured text/total: execute 69,431/107,929, review
# 72,309/108,974.
# fw3-automation (2026-09-25): a missing_credential block names its Office
# Secrets so saving them resumes the task — the shared-rules sentence (+85
# net) and update_status's optional office_secret_names parameter (+201 in
# the served catalog). Measured execute 69,572/108,275.
# 2026-09-25 (fz-mcp-delivery, U15/L01/L03): totals +724 — get_my_brief and
# get_task_detail carry the shared preview warning and the `section`/`offset`
# parameters. Measured total: execute 108,999, review 109,786, triage 110,181.
# 2026-09-25 (fz-credentials-resume, L05): review total 109_100→109_350.
# move_task gains the same optional office_secret_names array, so a
# reviewer's ONE missing_credential block names its secrets (+234 in the
# served catalog); the text is unchanged. Measured review 72,393/109,296.
# 2026-09-25 (merge of fz-mcp-delivery + fz-credentials-resume): both catalog
# additions together, re-measured. Totals: execute 108,999, review 110,020.
_WORKER_CEILINGS: dict[str, tuple[int, int]] = {
    "execute": (69_650, 109_100),
    "review": (72_400, 110_100),
    # Review of P8: the MA's still-blocked Path C rule (+346): 62,675/109,733.
    # 2026-09-25 (fz-prompts U01 repair): the triage steps, the MA's hard rule,
    # triage mode and Path C name the bounce-cap exception (a person decided:
    # synthesis comment only), and the refused bullet says where the decision
    # went (+624): 63,165/110,387. The MA playbook's triage step 3 names the
    # same exception (+96), and the MA's retry_blocked_task definition is 23
    # characters shorter: 63,261/110,445.
    # Merged with fz-mcp-delivery (larger read definitions in the MA
    # catalog) and fz-credentials-resume (move_task office_secret_names),
    # re-measured 63,261/111,403.
    # 2026-09-25 (f2-ai-surfaces items 18/19): the triage step and the MA
    # playbook scope the bounce-cap exception to the current block, and a
    # bounce_cap_user_decision refusal may be pending (+176): 63,437/111,579.
    # Kept (2026-09-26, f5-final): the MA's person-decided line names Resume
    # task (+24): 63,461/111,646.
    # 2026-09-26 (fr-prompts): prompts-0 stops serving retry_blocked_task in
    # triage (the MA catalog loses that definition, total -747), and
    # prompts-4 has both Path C texts (the triage steps and the MA playbook)
    # pass office_secret_names on a missing_credential escalation, so saving
    # the secrets resumes the task (+264): 63,725/111,163. Total ratcheted
    # down.
    "triage": (63_800, 111_250),
}


def _measured() -> dict[str, tuple[int, int, tuple[int, int]]]:
    measured = {}
    for context, ceilings in _MANAGER_CEILINGS.items():
        sizes = compose_manager(context).sizes()
        measured[f"manager:{context}"] = (
            sizes["total"] - sizes["tools"],
            sizes["total"],
            ceilings,
        )
    for phase, ceilings in _WORKER_CEILINGS.items():
        sizes = compose_worker(phase).sizes()
        measured[f"worker:{phase}"] = (
            sizes["total"] - sizes["tools"],
            sizes["total"],
            ceilings,
        )
    return measured


def test_budgets_cover_every_representative_context():
    assert set(_MANAGER_CEILINGS) == set(MANAGER_CONTEXTS)
    assert set(_WORKER_CEILINGS) == set(WORKER_PHASES)


def test_composed_prompts_within_budget():
    over = {
        name: {"text": (text, text_cap), "total": (total, total_cap)}
        for name, (text, total, (text_cap, total_cap)) in _measured().items()
        if text > text_cap or total > total_cap
    }
    assert not over, (
        "composed prompt(s) over budget — trim, or raise the ceiling in the "
        f"same commit with a rationale: {over}"
    )


def test_composition_guard_is_not_vacuous():
    too_loose = {
        name: (round(text_cap / text, 2), round(total_cap / total, 2))
        for name, (text, total, (text_cap, total_cap)) in _measured().items()
        if text_cap / text > 1.35 or total_cap / total > 1.35
    }
    assert not too_loose, f"ceiling(s) too loose (ratchet down): {too_loose}"


@pytest.mark.parametrize("context", sorted(MANAGER_CONTEXTS))
def test_manager_composition_is_the_production_render(context):
    """The measured parts are exactly the writer files + dynamic context."""
    from src._agent_image.mcp_tool_server import select_session_tools
    from tests.evals._prompt_composition import (
        manager_dynamic_context,
        rendered_office_and_manager,
    )

    composed = compose_manager(context)
    office, manager = rendered_office_and_manager()
    assert [name for name, _ in composed.parts] == ["office", "manager", "dynamic"]
    assert composed.text == "\n\n".join(
        (office, manager, manager_dynamic_context(context))
    )
    context_key = MANAGER_CONTEXTS[context][0]
    assert list(composed.tools) == select_session_tools(
        "manager", "", "manager", context_key=context_key
    )


def test_general_chat_catalog_is_the_stripped_surface():
    gc_tools = compose_manager("general_chat").tool_names
    ws_tools = compose_manager("default_workstream").tool_names
    assert "create_task" in ws_tools and "create_task" not in gc_tools
    assert gc_tools < ws_tools


# ── F07 phase C: each lifecycle fact reaches a worker session ONCE ─────────

_EXECUTE_SESSIONS = frozenset(
    name
    for name, (_, status, _o, _t) in WORKER_SESSIONS.items()
    if status == "in_progress"
)
# The ASD's task prompt carries its own two-run script protocol instead of
# the generic resume detail; the Manager Assistant's playbook loads its own
# blocker protocol instead of the shared worker rules.
_NO_RESUME_DETAIL = frozenset({"asd_execute"})
_NO_SHARED_WORK_RULES = frozenset({"ma_execute", "ma_review", "ma_triage"})


def _fact_counts(session: str, fact: str) -> dict[str, int]:
    composed = compose_worker_session(session)
    return {name: norm(body).count(norm(fact)) for name, body in composed.parts}


@pytest.mark.parametrize("session", sorted(WORKER_SESSIONS))
def test_session_end_fact_reaches_every_worker_session_once(session):
    from src._lifecycle_contract import SESSION_END_FACT

    counts = _fact_counts(session, SESSION_END_FACT)
    assert sum(counts.values()) == 1, counts
    assert counts["office"] == 1, counts


@pytest.mark.parametrize("session", sorted(WORKER_SESSIONS))
def test_script_handoff_core_reaches_every_worker_session_once(session):
    from src._lifecycle_contract import SCRIPT_HANDOFF_CORE

    counts = _fact_counts(session, SCRIPT_HANDOFF_CORE)
    assert sum(counts.values()) == 1, counts
    assert counts["role"] == 1, counts


@pytest.mark.parametrize("session", sorted(WORKER_SESSIONS))
def test_script_resume_detail_only_in_generic_execute_prompts(session):
    from src._lifecycle_contract import SCRIPT_HANDOFF_RESUME_FACT

    counts = _fact_counts(session, SCRIPT_HANDOFF_RESUME_FACT)
    expected = int(session in _EXECUTE_SESSIONS - _NO_RESUME_DETAIL)
    assert sum(counts.values()) == expected, counts
    if expected:
        assert counts["task_prompt"] == 1, counts


@pytest.mark.parametrize("session", sorted(WORKER_SESSIONS))
def test_execute_blocker_fact_at_most_once(session):
    from src._lifecycle_contract import EXECUTE_BLOCKER_FACT

    counts = _fact_counts(session, EXECUTE_BLOCKER_FACT)
    expected = int(session not in _NO_SHARED_WORK_RULES)
    assert sum(counts.values()) == expected, counts


def test_worker_sessions_cover_every_lifecycle_session_shape():
    from src._lifecycle_contract import SESSIONS

    modes = {
        (s.task_mode, s.agent == "manager-assistant", s.task_class) for s in SESSIONS
    }
    covered = set()
    for agent, status, overrides, _ in WORKER_SESSIONS.values():
        mode = {"in_progress": "execute", "review": "review", "blocked": "triage"}[
            status
        ]
        covered.add((mode, agent == "manager-assistant", overrides.get("task_class")))
    assert modes <= covered, modes - covered


@pytest.mark.parametrize("context", sorted(MANAGER_CONTEXTS))
def test_manager_reads_each_lifecycle_fact_at_most_once(context):
    """F07: moving procedures into modules must not make a Manager turn read
    a lifecycle-contract fact twice (core file + an injected module)."""
    import src._lifecycle_contract as contract

    composed = compose_manager(context)
    repeated = {}
    for fact in contract.ALL_FACTS:
        counts = {
            name: norm(body).count(norm(fact)) for name, body in composed.parts
        }
        if sum(counts.values()) > 1:
            repeated[fact[:60]] = counts
    assert not repeated, repeated
