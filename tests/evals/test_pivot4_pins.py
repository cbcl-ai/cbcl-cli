"""Pivot-4 pins — the role-shaped generation contract (P2-1, D4.5) and the
system-agent playbook comprehensiveness pass (P2-5).

P2-1 locked the hiring pipeline: every agent-generation surface (wizard,
/agents/generate, generate-field) must emit ROLE-shaped agents — a 2-4
sentence ownership statement (reason named: context / keys / review
separation / cost tier), SOPs as SKILLS (thin prompts), and a role-shape
preset (doer = opus+ultracode / specialist = opus+xhigh / responder =
sonnet with NO effort) — with the seniority register BANNED and the six
system-agent governance charters quoted in the shared framing (parity
with the backend strings, the P1-5 source of truth).

P2-5 stamped the six playbooks against pivots 3-4: governance-consistent
role openers, scheduled-assignment (op) awareness where a role touches
standing operations, and no dead references. Each change is pinned here
so the next rewrite can't silently regress it.
"""
from __future__ import annotations

import re

import pytest

from src._setup_prompts import (
    AGENT_DETAIL_PROMPT,
    AGENT_FROM_DESCRIPTION_PROMPT,
    IMPROVE_CONFIG_PROMPT,
    OFFICE_BUILD_FRAMING,
    ROSTER_PROMPT,
)
from src.setup_generator import (
    AGENT_INSTRUCTIONS_GEN_PROMPT,
    AGENT_SYSTEM_PROMPT_GEN_PROMPT,
    OFFICE_INSTRUCTIONS_PROMPT,
    _normalize_agent_effort,
)


def _norm(text: str) -> str:
    """Collapse whitespace so wrapped prompt lines compare as prose."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# P2-1 · Governance parity — the six charters are quoted VERBATIM in the
# framing (cross-repo pin against the backend strings, monorepo-only —
# same posture as test_system_agent_roster_parity / the aiq claims file).
# ---------------------------------------------------------------------------


def test_framing_quotes_all_six_governance_descriptions_verbatim():
    app = pytest.importorskip(
        "app.agents.system_agents",
        reason="backend package required — monorepo layout only",
    )
    framing = _norm(OFFICE_BUILD_FRAMING)
    for agent in app.SYSTEM_AGENT_DEFAULTS:
        blurb = _norm(agent["role_description"])
        assert blurb in framing, (
            f"OFFICE_BUILD_FRAMING no longer quotes the governance charter "
            f"for {agent['name']!r} verbatim — the generation contract must "
            f"teach what the built-ins own with the REAL strings (D4.5); "
            f"requote and repin in the same commit."
        )


def test_framing_governance_quotes_keep_the_function_label_prefix():
    # The backend format contract (pivot-4 D4.4) opens every charter with
    # "<Function label> — "; the framing must carry the labels so generation
    # reasons in governance language, not the old capability blurbs.
    for label in (
        "Research standards —",
        "Change control —",
        "Quality control —",
        "Execution —",
        "Chief of staff —",
        "Contracts —",
    ):
        assert label in _norm(OFFICE_BUILD_FRAMING), (
            f"framing lost the governance function label {label!r}"
        )


# ---------------------------------------------------------------------------
# P2-1 · The seniority ban — named in the contracts, and NOT used outside
# the ban blocks (the NEGATIVE pin).
# ---------------------------------------------------------------------------

_BAN_MARKER = "seniority register"
_BANNED_TERMS = ('"senior"', '"expert"', '"world-class"', '"10+ years"',
                 '"highly skilled"')


def test_seniority_ban_is_named_in_the_agent_authoring_contracts():
    # The ban must be explicit — the exact register named — in the shared
    # wizard framing AND the standalone generate-field system-prompt
    # contract (the one agent-authoring surface that does not compose the
    # framing).
    for name, prompt in (
        ("OFFICE_BUILD_FRAMING", OFFICE_BUILD_FRAMING),
        ("AGENT_SYSTEM_PROMPT_GEN_PROMPT", AGENT_SYSTEM_PROMPT_GEN_PROMPT),
    ):
        assert "BANNED — the seniority register" in prompt, (
            f"{name} lost the seniority-ban block"
        )
        for term in _BANNED_TERMS:
            assert term in prompt, f"{name} ban no longer names {term}"


_SENIORITY_RE = re.compile(
    r"\bsenior\b|\bexpert\b|world-class|10\+\s*years|highly skilled",
    re.IGNORECASE,
)

# Every prompt that AUTHORS agent-facing identity content. (The vision /
# instructions / skill prompts describe the office, not agents, and some
# legitimately open with "You are an expert ... author" self-address —
# out of scope for the register ban.)
_AGENT_AUTHORING_PROMPTS = {
    "OFFICE_BUILD_FRAMING": OFFICE_BUILD_FRAMING,
    "ROSTER_PROMPT": ROSTER_PROMPT,
    "AGENT_DETAIL_PROMPT": AGENT_DETAIL_PROMPT,
    "AGENT_FROM_DESCRIPTION_PROMPT": AGENT_FROM_DESCRIPTION_PROMPT,
    "IMPROVE_CONFIG_PROMPT": IMPROVE_CONFIG_PROMPT,
    "AGENT_SYSTEM_PROMPT_GEN_PROMPT": AGENT_SYSTEM_PROMPT_GEN_PROMPT,
    "AGENT_INSTRUCTIONS_GEN_PROMPT": AGENT_INSTRUCTIONS_GEN_PROMPT,
}


def _strip_ban_blocks(prompt: str) -> str:
    """Drop paragraphs that STATE the ban (they legitimately name the
    banned terms); everything else must be register-clean."""
    kept = [
        p for p in prompt.split("\n\n")
        if _BAN_MARKER not in p.lower() and "BANNED" not in p
    ]
    return "\n\n".join(kept)


def test_negative_no_seniority_register_outside_the_ban_blocks():
    offenders: dict[str, list[str]] = {}
    for name, prompt in _AGENT_AUTHORING_PROMPTS.items():
        hits = _SENIORITY_RE.findall(_strip_ban_blocks(prompt))
        if hits:
            offenders[name] = hits
    assert not offenders, (
        "seniority-speak leaked back into an agent-authoring prompt "
        f"(outside the ban block): {offenders}"
    )


def test_gold_examples_are_ownership_shaped_not_resumes():
    # The roster + improve gold examples model the register generation will
    # copy — they must name ownership + boundary + the seat's reason.
    roster_gold = _norm(ROSTER_PROMPT.split("GOLD EXAMPLE", 1)[1])
    assert "Owns the nightly reconciliation" in roster_gold
    assert "Earns its seat" in roster_gold
    improve_gold = _norm(IMPROVE_CONFIG_PROMPT.split("## Gold example", 1)[1])
    assert "Owns the screening gate" in improve_gold
    assert "review separation" in improve_gold


# ---------------------------------------------------------------------------
# P2-1 · SOPs as skills — method content ships as SKILLS; prompts stay thin.
# ---------------------------------------------------------------------------


def test_sops_as_skills_instruction_present_on_every_authoring_surface():
    assert "SOPs live in SKILLS" in ROSTER_PROMPT
    assert (
        "ships as SKILLS with real playbook content" in _norm(ROSTER_PROMPT)
    )
    # The shared wizard/agents-page contract (composed into both detail
    # prompts): the system prompt is thin, the METHOD lives in skills.
    for name, prompt in (
        ("AGENT_DETAIL_PROMPT", AGENT_DETAIL_PROMPT),
        ("AGENT_FROM_DESCRIPTION_PROMPT", AGENT_FROM_DESCRIPTION_PROMPT),
    ):
        n = _norm(prompt)
        assert "lives in the agent's SKILLS, never here" in n, name
        assert "not a second SOP home" in n or "OFFICE WIRING" in n, name
    # The generate-field pair.
    assert "lives in the agent's SKILLS, never here" in _norm(
        AGENT_SYSTEM_PROMPT_GEN_PROMPT
    )
    assert "SOPs live in SKILLS" in AGENT_INSTRUCTIONS_GEN_PROMPT
    assert "not a second home for SOP prose" in _norm(
        AGENT_INSTRUCTIONS_GEN_PROMPT
    )


def test_system_prompt_contract_is_thin_by_design():
    # 250-450-word six-part identity essays are the OLD shape; the pivot-4
    # contract is 120-250 words of ownership + boundaries + skill pointer.
    for name, prompt in (
        ("AGENT_DETAIL_PROMPT", AGENT_DETAIL_PROMPT),
        ("AGENT_SYSTEM_PROMPT_GEN_PROMPT", AGENT_SYSTEM_PROMPT_GEN_PROMPT),
    ):
        assert "120-250 words" in prompt, name
        assert "250-450" not in prompt, f"{name} kept the fat-prompt bound"
        assert "Method pointer" in prompt, name
        assert "THIN by design" in prompt, name


def test_claude_md_contract_is_wiring_with_how_you_work():
    # The claude_md outline routes method through skills BY SLUG; the old
    # "Standard Operating Procedure" restatement section is gone on both
    # authoring surfaces.
    n = _norm(AGENT_DETAIL_PROMPT)
    assert "### How You Work" in AGENT_DETAIL_PROMPT
    assert "Standard Operating Procedure" not in AGENT_DETAIL_PROMPT
    assert "the skill carries the sop" in n.lower()
    assert "### How You Work" in AGENT_INSTRUCTIONS_GEN_PROMPT
    assert "Standard Operating Procedure" not in AGENT_INSTRUCTIONS_GEN_PROMPT


# ---------------------------------------------------------------------------
# P2-1 · The role-shape preset table + the Sonnet-no-effort rule.
# ---------------------------------------------------------------------------


def test_preset_table_with_sonnet_no_effort_rule():
    for name, prompt in (
        ("ROSTER_PROMPT", ROSTER_PROMPT),
        ("AGENT_FROM_DESCRIPTION_PROMPT", AGENT_FROM_DESCRIPTION_PROMPT),
    ):
        n = _norm(prompt)
        assert "doer" in n and "specialist" in n and "responder" in n, name
        assert "ultracode" in n and "xhigh" in n, name
        # The load-bearing rule, verbatim on both surfaces:
        assert "NEVER emit an ``effort`` for a non-Opus model" in n, name
        assert "effort is Opus-only" in n, name
    # The improve pass must preserve the pair, never invent one.
    ni = _norm(IMPROVE_CONFIG_PROMPT)
    assert "NEVER emit an ``effort`` for a non-Opus model" in ni


def test_roster_preset_mapping_pairs_shape_model_effort():
    n = _norm(ROSTER_PROMPT)
    # doer → opus + ultracode; specialist → opus + xhigh; responder →
    # sonnet + omitted key (row order pins the mapping, not just presence).
    doer = n.index("**doer**")
    spec = n.index("**specialist**")
    resp = n.index("**responder**")
    assert "ultracode" in n[doer:spec]
    assert "xhigh" in n[spec:resp]
    assert "OMIT the key entirely" in n[resp:resp + 220]


def test_ownership_statement_contract_and_small_rosters():
    nf = _norm(OFFICE_BUILD_FRAMING)
    # The four seat-reasons, named as the roster-discipline menu.
    for reason in ("CONTEXT", "KEYS", "REVIEW SEPARATION", "COST TIER"):
        assert reason in nf, f"framing lost seat-reason {reason}"
    assert "2-4 custom agents is typical" in nf
    assert "an agent is a ROLE, not a résumé" in nf.lower() or (
        "an agent is a ROLE" in nf
    )
    # role_description = the 2-4 sentence ownership statement, reason named.
    for name, prompt in (
        ("ROSTER_PROMPT", ROSTER_PROMPT),
        ("AGENT_FROM_DESCRIPTION_PROMPT", AGENT_FROM_DESCRIPTION_PROMPT),
    ):
        n = _norm(prompt)
        assert "OWNERSHIP STATEMENT" in n, name
        assert "2-4 sentences" in n, name
        assert "name which" in n, name


def test_instructions_prompts_speak_governance_language():
    # The office-instructions handoff conventions (both generators) teach
    # routing in the governance vocabulary, not the old capability blurbs.
    from src._setup_prompts import INSTRUCTIONS_PROMPT

    for name, prompt in (
        ("INSTRUCTIONS_PROMPT", INSTRUCTIONS_PROMPT),
        ("OFFICE_INSTRUCTIONS_PROMPT", OFFICE_INSTRUCTIONS_PROMPT),
    ):
        n = _norm(prompt).lower()
        for label in ("quality control", "change control", "chief of staff"):
            assert label in n, f"{name} lost governance label {label!r}"


# ---------------------------------------------------------------------------
# P2-1 · The mechanical guard — _normalize_agent_effort enforces the pair.
# ---------------------------------------------------------------------------


def test_effort_normalizer_enforces_the_preset_rules():
    # Valid opus pairs survive (and canonicalise).
    a = {"model": "opus", "effort": "ultracode"}
    _normalize_agent_effort(a)
    assert a["effort"] == "ultracode"
    a = {"model": "opus", "effort": " XHIGH "}
    _normalize_agent_effort(a)
    assert a["effort"] == "xhigh"
    # A responder NEVER carries the key — sonnet/haiku efforts are dropped.
    for model in ("sonnet", "haiku"):
        a = {"model": model, "effort": "xhigh"}
        _normalize_agent_effort(a)
        assert "effort" not in a, model
    # Off-preset and junk values are dropped even on opus.
    for junk in ("max", "high", "", None, 3, {"x": 1}):
        a = {"model": "opus", "effort": junk}
        _normalize_agent_effort(a)
        assert "effort" not in a, repr(junk)
    # Missing key is a no-op.
    a = {"model": "sonnet"}
    _normalize_agent_effort(a)
    assert a == {"model": "sonnet"}


# ---------------------------------------------------------------------------
# P2-5 · Playbook comprehensiveness pins (one per surgical fix).
# ---------------------------------------------------------------------------

from src.config_sync.claude_md_templates._system_agents import (  # noqa: E402
    ANALYST_CLAUDE_MD,
    AUDITOR_CLAUDE_MD,
    AUTOMATION_SCRIPT_DEV_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    PLANNER_CLAUDE_MD,
)


def test_ma_playbook_opens_with_the_chief_of_staff_tier():
    n = _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    assert "chief of staff" in n
    assert "fast, economical tier" in n


def test_ma_playbook_knows_op_task_mechanics():
    n = _norm(MANAGER_ASSISTANT_CLAUDE_MD)
    # Blocked op instance = a stalled schedule (overlap-skip) — escalate.
    assert "A blocked `op` instance stalls its whole schedule" in n
    assert "overlap-skip mints no new runs" in n
    # Review side: repeated cross-run failure is schedule evidence.
    assert "a failure repeating across runs is schedule evidence" in n
    assert "fixes the standing brief" in n


def test_auditor_playbook_reviews_op_instances_per_run():
    n = _norm(AUDITOR_CLAUDE_MD)
    assert "standing-operation instances" in n
    assert "review THIS run against its brief" in n
    assert "fixes the standing brief, not just this instance" in n


def test_asd_playbook_owns_change_control_and_defers_judgment_work():
    n = _norm(AUTOMATION_SCRIPT_DEV_CLAUDE_MD)
    assert "change-control gate" in n
    assert "the only role that builds and installs" in n
    # The pivot-3 shape the script decision tree was missing:
    assert "A script can't think" in n
    assert "scheduled ASSIGNMENT" in n
    assert "`schedule_assignment`" in AUTOMATION_SCRIPT_DEV_CLAUDE_MD


def test_analyst_playbook_carries_the_research_standard_and_schedule_route():
    n = _norm(ANALYST_CLAUDE_MD)
    assert "citable, triangulated" in n
    # Recurring-with-judgment routes to a scheduled assignment, not a script.
    assert "scheduled ASSIGNMENT" in n
    assert "`schedule_assignment`" in ANALYST_CLAUDE_MD


def test_planner_playbook_routes_cadence_work_to_schedules():
    n = _norm(PLANNER_CLAUDE_MD)
    assert "Recurring work is a SCHEDULE, not a task list" in n
    assert "never author N repeating tasks to simulate a cadence" in n
    # The Planner cannot create schedules itself (catalog-excluded) — the
    # playbook must say so, matching tools_planner's exclusion set.
    assert "not in your toolset" in n
    from src._agent_image._mcp.tools_planner import get_planner_tools

    assert "schedule_assignment" not in {
        t["name"] for t in get_planner_tools()
    }


# ---------------------------------------------------------------------------
# P2-4 · Hire-on-the-fly (D4.7) — the hire consent card.
# ---------------------------------------------------------------------------

from src._agent_image._mcp.tools_manager import get_manager_tools  # noqa: E402
from src._agent_image._mcp.transforms import transform_params  # noqa: E402
from src.config_sync.claude_md_templates._manager import (  # noqa: E402
    MANAGER_CLAUDE_MD,
)

_MANAGER_NORM = _norm(MANAGER_CLAUDE_MD)


def _ask_user_choice_tool() -> dict:
    for tool in get_manager_tools():
        if tool["name"] == "ask_user_choice":
            return tool
    raise AssertionError("ask_user_choice not found in the Manager catalog")


def test_hire_kind_in_schema_with_proposed_agent_contract():
    """The kind extension changes NO tool counts (same tool — the drift
    suite pins the catalogs); what it adds is the 'hire_agent' enum value
    and the ``proposed_agent`` profile object the backend validates."""
    schema = _ask_user_choice_tool()["inputSchema"]
    assert "hire_agent" in schema["properties"]["kind"]["enum"]
    profile = schema["properties"]["proposed_agent"]
    assert profile["type"] == "object"
    assert set(profile["required"]) == {
        "name", "display_name", "ownership", "preset", "reason",
    }
    props = profile["properties"]
    assert props["name"]["pattern"] == "^[a-z][a-z0-9-]{1,63}$"
    assert props["preset"]["enum"] == ["doer", "specialist", "responder"]
    assert props["skill_names"]["maxItems"] == 2
    # The ownership contract names the governance prefix.
    assert "<Function label> — " in props["ownership"]["description"]


def test_hire_tool_description_pins_the_consent_posture():
    desc = " ".join(_ask_user_choice_tool()["description"].split())
    assert "only when the roster audit finds NO fitting profile" in desc
    assert "'hire' then 'not_now'" in desc
    assert "the BACKEND generate and create the agent — NEVER you" in desc


def test_hire_profile_survives_the_transform_whitelist():
    """The ask transform whitelists params — without ``proposed_agent``
    in the tuple every hire ask would reach the backend profile-less
    and be refused (the intake 'questions' lesson, re-learned)."""
    out = transform_params(
        "ask_user_choice",
        None,
        {
            "question": "Hire?",
            "kind": "hire_agent",
            "options": [],
            "proposed_agent": {"name": "lead-qualifier"},
            "junk": "stripped",
        },
    )
    assert out["proposed_agent"] == {"name": "lead-qualifier"}
    assert "junk" not in out


def test_playbook_hire_branch_is_the_audit_failure_branch():
    """The roster-first audit's failure branch proposes the hire — with
    the consent card, never by creating the profile or asking in prose."""
    assert 'ask_user_choice(kind="hire_agent")' in _MANAGER_NORM
    assert "NEVER create the profile yourself" in _MANAGER_NORM
    assert "The card IS the ask — NEVER ask for permission in prose" in (
        _MANAGER_NORM
    )
    # The proposal carries ownership + preset + the audit-failure reason.
    branch = _MANAGER_NORM.split("Hiring — when the roster audit", 1)[1]
    for token in (
        "`ownership`", "`preset`", "`reason`", "`skill_names`",
        "`hire` + `not_now`",
    ):
        assert token in branch, f"hire branch lost {token}"
    assert "`doer`" in branch and "`responder`" in branch


def test_playbook_anti_sprawl_pinned_both_directions():
    assert (
        "NEVER propose a hire when an existing profile fits" in _MANAGER_NORM
    )
    assert (
        "never silently struggle with a misfit either" in _MANAGER_NORM
    )


def test_playbook_declined_hire_uses_closest_profile():
    assert (
        "Declined (`not_now`) → use the closest existing profile and SAY so"
        in _MANAGER_NORM
    )
