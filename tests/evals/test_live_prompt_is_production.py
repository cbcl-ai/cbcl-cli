"""EVAL-02 (static half) — CI-runnable guard that the LIVE Manager evals
exercise the PRODUCTION prompt, not a hand-written stub.

The live evals themselves are ``@live_eval`` (skipped without ANTHROPIC_API_KEY,
nightly), so they can't gate a merge. This static guard runs in the normal lane
and pins the two properties that make the live evals real:
  1. ``render_production_manager_prompt`` returns the actual ``MANAGER_CLAUDE_MD``
     + ``build_dynamic_context`` composition (so a change to either DOES change
     the live eval input), and
  2. the default model is the platform MANAGER TIER (Opus), not the cheap smoke
     Sonnet.
If someone re-introduces a distilled stub, this fails in CI immediately.
"""
from __future__ import annotations

from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD
from tests.evals.live._harness import (
    DEFAULT_MODEL,
    MANAGER_TIER_MODEL,
    render_production_manager_prompt,
)

_FIXTURE_CTX = {
    "office_name": "Acme",
    "workstream_id": "11111111-1111-1111-1111-111111111111",
    "workstream_name": "Recruitment",
    "workstream_priority": "high",
    "workstream_description": "Hire engineers.",
    "workstream_goals": "Ship the team.",
    "team_roster": "**Manager Assistant** (manager-assistant) — ⚡",
    "board_summary": {},
    "scopes": [],
}


def test_live_manager_prompt_is_the_production_artifact():
    prompt = render_production_manager_prompt(
        "workstream:11111111-1111-1111-1111-111111111111",
        _FIXTURE_CTX,
    )
    # A large, distinctive slice of the real static template must be present —
    # not a 10-line distilled contract.
    assert len(prompt) > 40_000, (
        "live Manager prompt is far smaller than the production template — "
        "did the stub come back?"
    )
    # Pin a stable, load-bearing sentence fragment from the real template so a
    # regression to a stub (which won't contain it) fails here.
    marker = "create_task"
    assert marker in prompt and marker in MANAGER_CLAUDE_MD
    # The dynamic context actually merged (fixture workstream name shows up).
    assert "Recruitment" in prompt


def test_live_eval_model_is_manager_tier():
    # Default must be the Opus/Manager tier, not the cheap smoke model.
    assert DEFAULT_MODEL == MANAGER_TIER_MODEL
    assert "opus" in MANAGER_TIER_MODEL
