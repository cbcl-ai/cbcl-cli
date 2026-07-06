"""GEN-15: the agent-detail / skill generation prompts get the SALIENT office
instruction sections (Mission / Focus, Quality Standards, Conventions), not a
blind [:1200] prefix that truncated mid-Mission and never reached the rest."""
from __future__ import annotations

from src.setup_generator import _salient_instructions_excerpt


_INSTR = """# Office Rules

## Mission / Focus Areas
Recruitment automation for Colombian Python developers.

## Team & Roles
Everyone reports to the Manager. (not salient)

## Quality Standards
Every deliverable ships with tests and a change-summary artifact.

## Conventions
snake_case; write outputs to /workspace/outputs/.
"""


def test_extracts_salient_sections_only() -> None:
    out = _salient_instructions_excerpt(_INSTR)
    assert "## Mission / Focus Areas" in out
    assert "## Quality Standards" in out
    assert "## Conventions" in out
    # Non-salient sections are dropped.
    assert "Team & Roles" not in out
    assert "reports to the Manager" not in out


def test_falls_back_to_prefix_when_no_headers() -> None:
    plain = "x" * 3000
    out = _salient_instructions_excerpt(plain, max_chars=1200)
    assert out == plain[:1200]


def test_respects_max_chars_cap() -> None:
    big = "## Mission\n" + ("m" * 5000)
    out = _salient_instructions_excerpt(big, max_chars=800)
    assert len(out) <= 800


def test_no_false_positive_on_substring_headers() -> None:
    # GEN-15 hardening: "## Permissions" contains "mission" but must NOT be
    # picked (word-boundary match). "## Conventions" (plural) must be picked.
    instr = (
        "## Permissions\nWho can do what — NOT salient, must be dropped.\n\n"
        "## Conventions\nsnake_case; outputs to /workspace/outputs/.\n"
    )
    out = _salient_instructions_excerpt(instr)
    assert "## Conventions" in out
    assert "Permissions" not in out
    assert "NOT salient" not in out
