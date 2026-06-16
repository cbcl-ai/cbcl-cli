"""T5.2.7 — pin the system-agent roster across every render site.

There are FIVE system agents (incl. the Planner). The setup-wizard prompts +
list_agents description used to hand-write the roster and drifted (said "four",
omitted planner from the reserved-name guards → apply-time UNIQUE collision).
These pins fail loudly if a site drops a system agent or still says "four".
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


def test_roster_has_five_agents_including_planner() -> None:
    assert len(SYSTEM_AGENT_SLUGS) == 5
    assert "planner" in SYSTEM_AGENT_SLUGS


def test_reserved_name_guards_list_planner() -> None:
    # Both wizard slug guards must name `planner` so a generated roster can't
    # collide with the Planner system agent on UNIQUE(office_id, name).
    for prompt in (ROSTER_PROMPT, AGENT_FROM_DESCRIPTION_PROMPT):
        assert "planner" in prompt.lower(), (
            "reserved-name guard omits 'planner' — apply-time collision risk"
        )


def test_no_setup_prompt_says_four_system_agents() -> None:
    for prompt in (OFFICE_BUILD_FRAMING, INSTRUCTIONS_PROMPT, ROSTER_PROMPT):
        assert not re.search(r"\bfour\b.{0,20}system agent", prompt, re.I), (
            "a setup prompt still says 'four system agents'"
        )
        assert not re.search(r"\bFOUR\b.{0,30}SYSTEM AGENT", prompt), (
            "a setup prompt still says 'FOUR SYSTEM AGENTS'"
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


def test_list_agents_says_five_and_consult_only_planner() -> None:
    desc = ""
    for t in get_manager_tools():
        if t["name"] == "list_agents":
            desc = str(t)
            break
    assert "five" in desc.lower()
    assert "Planner" in desc


def test_roster_matches_backend_system_agent_defaults() -> None:
    # F-5.2.7-C: make the module docstring's cross-repo-parity claim TRUE —
    # the communicator's canonical slug set must equal the backend's
    # SYSTEM_AGENT_DEFAULTS (the actual source of the office's system agents).
    from app.agents.system_agents import SYSTEM_AGENT_DEFAULTS

    backend_slugs = {a["name"] for a in SYSTEM_AGENT_DEFAULTS}
    assert set(SYSTEM_AGENT_SLUGS) == backend_slugs, (
        "communicator roster ↔ backend SYSTEM_AGENT_DEFAULTS drift"
    )


def test_runtime_reserved_name_guard_matches_canonical_roster() -> None:
    # F-5.2.7-C: the runtime apply-time guard derives its own slug set from
    # SYSTEM_AGENT_CLAUDE_MD; pin it == the canonical roster so the two
    # same-purpose sets can't diverge (e.g. a 6th agent added to one only).
    from src.setup_generator import SYSTEM_AGENT_SLUGS as RUNTIME_SLUGS

    assert set(RUNTIME_SLUGS) == set(SYSTEM_AGENT_SLUGS)
