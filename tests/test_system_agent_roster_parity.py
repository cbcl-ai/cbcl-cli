"""T5.2.7 — pin the system-agent roster across every render site.

There are EIGHT system agents (incl. the Planner; since pivot-1 T1 the
Builder; since Flow Studio FS-P3 the consult-only Flow Architect + Data
Curator). The setup-wizard prompts + list_agents description used to
hand-write the roster and drifted (said "four", omitted planner from the
reserved-name guards → apply-time UNIQUE collision). These pins fail loudly
if a site drops a system agent or carries a stale count.
"""
from __future__ import annotations

import re

from src._system_agent_roster import SYSTEM_AGENT_SLUGS
from src._setup_prompts import (
    AGENT_FROM_DESCRIPTION_PROMPT,
    INSTRUCTIONS_PROMPT,
    OFFICE_BUILD_FRAMING,
    ROSTER_PROMPT,
)
from src._agent_image._mcp.tools_manager import get_manager_tools


def test_roster_has_eight_agents_including_the_consult_only_three() -> None:
    assert len(SYSTEM_AGENT_SLUGS) == 8
    assert "planner" in SYSTEM_AGENT_SLUGS
    assert "builder" in SYSTEM_AGENT_SLUGS
    assert "flow-architect" in SYSTEM_AGENT_SLUGS
    assert "data-curator" in SYSTEM_AGENT_SLUGS


def test_reserved_name_guards_list_all_eight_slugs() -> None:
    # Both wizard slug guards must name EVERY system-agent slug so a generated
    # roster can't collide on UNIQUE(office_id, name) at apply time. (Pivot-4
    # P2-1 repin: the ROSTER_PROMPT guard had gone stale at FIVE slugs —
    # `builder` was missing — so the per-slug assertion replaces the old
    # planner-only check.)
    for prompt in (ROSTER_PROMPT, AGENT_FROM_DESCRIPTION_PROMPT):
        lowered = prompt.lower()
        for slug in SYSTEM_AGENT_SLUGS:
            assert slug in lowered, (
                f"reserved-name guard omits {slug!r} — apply-time collision risk"
            )


def test_no_setup_prompt_says_four_system_agents() -> None:
    for prompt in (OFFICE_BUILD_FRAMING, INSTRUCTIONS_PROMPT, ROSTER_PROMPT):
        assert not re.search(r"\bfour\b.{0,20}system agent", prompt, re.I), (
            "a setup prompt still says 'four system agents'"
        )
        assert not re.search(r"\bFOUR\b.{0,30}SYSTEM AGENT", prompt), (
            "a setup prompt still says 'FOUR SYSTEM AGENTS'"
        )


def test_generation_prompts_carry_eight_agent_roster_with_builder() -> None:
    """C-6 (extended FS-P3): the two LIVE generation prompts that enumerate
    the system-agent roster must not carry a stale count word
    (three/four/five/six/seven near 'system agents' — the roster is EIGHT),
    must name the Builder with its one-sitting-build handoff, and must name
    the two Flow Studio agents (Flow Architect / Data Curator)."""
    from src.setup_generator import OFFICE_INSTRUCTIONS_PROMPT

    stale_count = re.compile(
        r"\b(three|four|five|six|seven)\b[^.\n]{0,40}\bSYSTEM agents\b", re.I
    )
    for name, prompt in (
        ("OFFICE_INSTRUCTIONS_PROMPT", OFFICE_INSTRUCTIONS_PROMPT),
        ("INSTRUCTIONS_PROMPT", INSTRUCTIONS_PROMPT),
    ):
        assert not stale_count.search(prompt), (
            f"{name} carries a stale system-agent count word"
        )
        assert "Builder" in prompt, f"{name} omits the Builder"
        assert re.search(r"one-sitting build", prompt, re.I), (
            f"{name} omits the Builder one-sitting-build handoff"
        )
        assert re.search(r"flow.architect", prompt, re.I), (
            f"{name} omits the Flow Architect"
        )
        assert re.search(r"data.curator", prompt, re.I), (
            f"{name} omits the Data Curator"
        )


def test_office_build_framing_shows_ma_with_bash_and_planner() -> None:
    # MA must be shown WITH Bash (it's a Board Operator that runs one-shot
    # verifications); the Planner block must be present + consult-only.
    framing = OFFICE_BUILD_FRAMING
    # The manager-assistant bullet lists Bash among its tools.
    ma_idx = framing.index("manager-assistant")
    ma_block = framing[ma_idx : ma_idx + 200]
    assert "Bash" in ma_block, "MA roster entry missing Bash"
    assert "planner" in framing.lower()
    assert "consult" in framing.lower()


def test_list_agents_says_eight_and_names_the_consult_only_agents() -> None:
    desc = ""
    for t in get_manager_tools():
        if t["name"] == "list_agents":
            desc = str(t)
            break
    assert "eight" in desc.lower()
    assert "six" not in desc.lower()  # stale count must not survive
    assert "Planner" in desc
    assert "Builder" in desc
    assert "Flow Architect" in desc
    assert "Data Curator" in desc


def test_roster_matches_backend_system_agent_defaults() -> None:
    # F-5.2.7-C: make the module docstring's cross-repo-parity claim TRUE —
    # the communicator's canonical slug set must equal the backend's
    # SYSTEM_AGENT_DEFAULTS (the actual source of the office's system agents).
    from app.agents.system_agents import SYSTEM_AGENT_DEFAULTS

    backend_slugs = {a["name"] for a in SYSTEM_AGENT_DEFAULTS}
    assert set(SYSTEM_AGENT_SLUGS) == backend_slugs, (
        "communicator roster ↔ backend SYSTEM_AGENT_DEFAULTS drift"
    )


def test_backend_system_agent_model_and_effort_parity() -> None:
    """D4.2 cross-repo pin: the MA is the ONE Sonnet system agent
    (effort None — effort is Opus-only by backend validation, so its
    defaults dict must carry NO effort key the boot resync could
    stamp); the other five run the Opus tier with their pinned efforts
    (BEST-05/SES-05 + pivot-1 T1). Read from the REAL backend module so
    a future defaults.py edit can't silently regress the split."""
    from app.agents.system_agents import SYSTEM_AGENT_DEFAULTS
    from app.ai_models.defaults import model_tier

    expected = {
        "analyst": ("opus", "xhigh"),
        "automation-script-developer": ("opus", "xhigh"),
        "auditor": ("opus", "xhigh"),
        "builder": ("opus", "ultracode"),
        "manager-assistant": ("sonnet", None),
        "planner": ("opus", "ultracode"),
        # Flow Studio FS-P3 (spec §8): both consult-only agents run the
        # Opus tier at plain xhigh — authoring is judgment + writing,
        # not orchestration (no ultracode).
        "data-curator": ("opus", "xhigh"),
        "flow-architect": ("opus", "xhigh"),
    }
    by_name = {a["name"]: a for a in SYSTEM_AGENT_DEFAULTS}
    assert set(by_name) == set(expected)
    for name, (tier, effort) in expected.items():
        agent = by_name[name]
        assert model_tier(agent["model"]) == tier, (
            f"{name} must resolve to the {tier} tier, got model "
            f"{agent['model']!r}"
        )
        assert agent.get("effort") == effort, (
            f"{name} must ship effort {effort!r}, got "
            f"{agent.get('effort')!r}"
        )
    assert "effort" not in by_name["manager-assistant"], (
        "the MA defaults dict must carry no effort key — Sonnet + "
        "effort is an illegal pairing the resync would try to stamp"
    )


def test_runtime_reserved_name_guard_matches_canonical_roster() -> None:
    # F-5.2.7-C: the runtime apply-time guard derives its own slug set from
    # SYSTEM_AGENT_CLAUDE_MD; pin it == the canonical roster so the two
    # same-purpose sets can't diverge (e.g. a 6th agent added to one only).
    from src.setup_generator import SYSTEM_AGENT_SLUGS as RUNTIME_SLUGS

    assert set(RUNTIME_SLUGS) == set(SYSTEM_AGENT_SLUGS)


def test_daemon_roster_formatter_emits_slug_and_planner_note():
    """MGR-02 parity: the daemon-side ConfigStore._format_agent must emit the
    agent SLUG (create_task validates against it) + the Planner consult-only
    annotation — kept in parity with the backend context_builder formatter."""
    from src.config_sync.sync_service import ConfigStore

    cs = ConfigStore()
    dev = cs._format_agent(
        {
            "name": "python-developer",
            "display_name": "Senior Python Developer",
            "avatar_emoji": "👩‍💻",
            "role_description": "Backend dev",
            "model": "opus",
            "allowed_tools": ["Read"],
        }
    )
    assert "(python-developer)" in dev[0]

    planner = "\n".join(
        cs._format_agent(
            {
                "name": "planner",
                "display_name": "Planner",
                "avatar_emoji": "🗺️",
                "role_description": "Planning",
            }
        )
    )
    assert "(planner)" in planner
    assert "consult_planner" in planner


def test_framing_tool_lists_match_system_agent_defaults() -> None:
    """GEN-06: every system-agent 'Tools: ...' line in OFFICE_BUILD_FRAMING must
    equal that agent's real SYSTEM_AGENT_DEFAULTS allowed_tools (the parity eval
    previously pinned only the MA row, so the Analyst/Auditor lines drifted:
    Analyst was missing Bash, Auditor was missing Write)."""
    from app.agents.system_agents import SYSTEM_AGENT_DEFAULTS

    defaults = {d["name"]: set(d["allowed_tools"]) for d in SYSTEM_AGENT_DEFAULTS}

    # Parse each "* **slug** … Tools: A, B, C." bullet from the framing.
    blocks = re.split(r"\*\s+\*\*", OFFICE_BUILD_FRAMING)
    seen: dict[str, set[str]] = {}
    for block in blocks:
        m_slug = re.match(r"([a-z0-9-]+)\*\*", block)
        m_tools = re.search(r"Tools:\s*([A-Za-z0-9,\s]+?)\.", block)
        if not m_slug or not m_tools:
            continue
        slug = m_slug.group(1)
        tools = {t.strip() for t in m_tools.group(1).split(",") if t.strip()}
        seen[slug] = tools

    # Every framing agent WITH a Tools line must match its defaults exactly.
    for slug, framing_tools in seen.items():
        assert slug in defaults, f"framing names unknown system agent {slug}"
        assert framing_tools == defaults[slug], (
            f"{slug} framing tools {sorted(framing_tools)} != "
            f"SYSTEM_AGENT_DEFAULTS {sorted(defaults[slug])}"
        )
    # The tool-bearing system agents must all be covered (Planner is
    # consult-only and intentionally has no Tools line).
    assert {"analyst", "auditor", "automation-script-developer",
            "manager-assistant"} <= set(seen)
