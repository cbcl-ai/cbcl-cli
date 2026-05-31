"""Prompt + model invariants for the authoritative office-generation redesign.

These lock in the philosophy flip (the generator DECIDES and BUILDS rather
than proposing/flagging) and the removal of workstream/suggestion machinery,
so structural drift fails CI instead of silently reintroducing
"propose, don't decide" behaviour.
"""
from __future__ import annotations

import os

from src._setup_prompts import (
    IMPROVE_CONFIG_PROMPT,
    INSTRUCTIONS_PROMPT,
    OFFICE_BUILD_FRAMING,
    ROSTER_PROMPT,
    SYNTHESIZE_VISION_PROMPT,
)


def test_framing_is_authoritative() -> None:
    assert "principal architect" in OFFICE_BUILD_FRAMING
    assert "DECIDE and BUILD" in OFFICE_BUILD_FRAMING
    # The old defer-don't-decide directive must be gone.
    assert "do NOT silently smooth it over" not in OFFICE_BUILD_FRAMING


def test_roster_prompt_has_no_suggestion_or_workstream_machinery() -> None:
    for banned in (
        "proposed_because",
        "proposed_workstreams",
        "roster_rationale",
        "Workstream proposals",
        "propose what the user missed",
    ):
        assert banned not in ROSTER_PROMPT, banned
    # No concrete dated id in the prompt — model is a tier alias.
    assert "claude-opus-4-7" not in ROSTER_PROMPT


def test_roster_prompt_asks_ai_to_pick_a_model_tier() -> None:
    """Req #5: the roster prompt must guide the AI to pick a best-fit
    tier per agent (opus/sonnet/haiku), not force one tier."""
    assert "best-fit tier" in ROSTER_PROMPT.lower() or "best fit" in ROSTER_PROMPT.lower()
    for tier in ("opus", "sonnet", "haiku"):
        assert tier in ROSTER_PROMPT, tier


def test_vision_prompt_has_no_gaps_section() -> None:
    assert "Critical Gaps" not in SYNTHESIZE_VISION_PROMPT


def test_instructions_prompt_forbids_placeholders() -> None:
    assert "To be refined" not in INSTRUCTIONS_PROMPT


def test_improve_prompt_drops_removed_fields() -> None:
    assert "roster_rationale" not in IMPROVE_CONFIG_PROMPT
    assert "proposed_workstreams" not in IMPROVE_CONFIG_PROMPT


def test_improve_prompt_preserves_model_tier() -> None:
    """Req #5: an improve pass must not silently reset a deliberate
    per-agent tier — the prompt instructs preservation, and the code
    backfills from the prior config when the AI omits it."""
    p = IMPROVE_CONFIG_PROMPT.lower()
    assert "model" in p and "tier" in p


def test_model_defaults_are_tier_aliases() -> None:
    """Phase 2: defaults are the CLI's bare family aliases (resolved to
    the latest model in that tier at run time), not dated ids."""
    from src.orchestrator._model_defaults import (
        FALLBACK_MANAGER_MODEL,
        FALLBACK_WIZARD_MODEL,
        FALLBACK_WORKER_MODEL,
    )

    assert FALLBACK_WORKER_MODEL == "opus"
    assert FALLBACK_MANAGER_MODEL == "opus"
    assert FALLBACK_WIZARD_MODEL == "sonnet"


def test_generation_runs_on_opus() -> None:
    """The office-design pass runs on the Opus tier (user-confirmed),
    unless an operator overrides via CBCL_GENERATION_MODEL."""
    from src._setup_cli import _DEFAULT_GENERATION_MODEL
    from src.orchestrator._model_defaults import FALLBACK_MANAGER_MODEL

    if not os.environ.get("CBCL_GENERATION_MODEL", "").strip():
        assert _DEFAULT_GENERATION_MODEL == FALLBACK_MANAGER_MODEL
        assert "opus" in _DEFAULT_GENERATION_MODEL


def test_normalize_model_tier_honours_valid_and_falls_back() -> None:
    """Req #5: the validator keeps a valid AI-chosen tier and falls back
    to opus on anything else."""
    from src.setup_generator import _normalize_model_tier

    assert _normalize_model_tier("opus") == "opus"
    assert _normalize_model_tier("sonnet") == "sonnet"
    assert _normalize_model_tier("haiku") == "haiku"
    assert _normalize_model_tier("SONNET") == "sonnet"   # case-insensitive
    assert _normalize_model_tier("opus[1m]") == "opus"   # strips 1m tag
    # bad / missing / concrete → opus fallback
    assert _normalize_model_tier(None) == "opus"
    assert _normalize_model_tier("") == "opus"
    assert _normalize_model_tier("gpt-4") == "opus"
    assert _normalize_model_tier("claude-opus-4-7") == "opus"
