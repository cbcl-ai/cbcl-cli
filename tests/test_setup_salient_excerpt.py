"""The agent-detail / skill generation prompts get the WHOLE office
instructions (C4d-G2).

GEN-15 replaced a blind ``[:1200]`` prefix with a salient-section excerpt,
but its keywords lagged contract v2: the "Domain Knowledge" hard
constraints and the "Source map" never reached the agent and skill
writers, and its 1,800-character cap cut mid-word without a marker. A
fitted draft is at most the 16,000-character save cap, so it is passed
whole; only a longer ``over_limit`` draft is cut, at a line boundary with
an explicit marker.
"""
from __future__ import annotations

import re

from src._setup_prompts import AGENT_DETAIL_PROMPT
from src.setup_generator import _office_instructions_for_prompt

_CONTRACT_SHAPED = """# Office Rules

## Mission
Estimate renovation costs for residential clients in Lisbon.

## Domain Knowledge
Structural work is never estimated remotely; it needs a site visit.

## Conventions
Quotes in EUR, VAT shown separately.

## Quality bar
Every estimate lists its assumptions.

## Source map
- `uploads/price-list-2026.xlsx` — current unit prices.
"""


def test_every_contract_section_reaches_the_prompt() -> None:
    out = _office_instructions_for_prompt(_CONTRACT_SHAPED)
    for header in (
        "## Mission",
        "## Domain Knowledge",
        "## Conventions",
        "## Quality bar",
        "## Source map",
    ):
        assert header in out, header
    assert "never estimated remotely" in out
    assert "uploads/price-list-2026.xlsx" in out


def test_a_fitted_draft_is_passed_whole() -> None:
    long_conventions = "## Conventions\n" + ("- Refuse vague line items.\n" * 400)
    assert len(long_conventions) > 1800
    assert _office_instructions_for_prompt(long_conventions) == long_conventions.strip()


def test_an_over_limit_draft_is_cut_at_a_line_with_a_marker() -> None:
    lines = [f"- rule number {i} about quoting" for i in range(2000)]
    big = "## Conventions\n" + "\n".join(lines)
    out = _office_instructions_for_prompt(big, max_chars=800)
    assert len(out) <= 800
    body, marker = out.rsplit("\n", 1)
    assert marker.startswith("[office instructions truncated here:")
    assert re.search(r"(\d+) more characters were not included\]$", marker)
    # The kept text ends on a whole line, never mid-word.
    assert body.splitlines()[-1] in lines


def test_a_single_huge_line_is_cut_at_a_space() -> None:
    big = " ".join(["word"] * 5000)
    out = _office_instructions_for_prompt(big, max_chars=500)
    body = out.split("\n[office instructions truncated here:")[0]
    assert body.endswith("word")
    assert len(out) <= 500


def test_empty_instructions_stay_empty() -> None:
    assert _office_instructions_for_prompt("") == ""


def test_agent_detail_prompt_describes_the_instructions_as_complete() -> None:
    """R17: the system prompt must not call its input an excerpt.

    The agent writer receives the whole office instructions, so telling it
    the input is an excerpt invites it to treat Domain Knowledge or the
    Source map as possibly incomplete.
    """
    assert "excerpt" not in AGENT_DETAIL_PROMPT.lower()
    assert "office instructions (complete" in AGENT_DETAIL_PROMPT.lower()
