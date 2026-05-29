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
    # Model is stamped by the assembly, not dictated as a literal here.
    assert "claude-opus-4-7" not in ROSTER_PROMPT


def test_vision_prompt_has_no_gaps_section() -> None:
    assert "Critical Gaps" not in SYNTHESIZE_VISION_PROMPT


def test_instructions_prompt_forbids_placeholders() -> None:
    assert "To be refined" not in INSTRUCTIONS_PROMPT


def test_improve_prompt_drops_removed_fields() -> None:
    assert "roster_rationale" not in IMPROVE_CONFIG_PROMPT
    assert "proposed_workstreams" not in IMPROVE_CONFIG_PROMPT


def test_generation_runs_on_opus_and_agents_use_worker_model() -> None:
    from src._setup_cli import _DEFAULT_GENERATION_MODEL
    from src.orchestrator._model_defaults import (
        FALLBACK_MANAGER_MODEL,
        FALLBACK_WORKER_MODEL,
    )

    # Generated agents are stamped with the canonical (Opus) worker tier.
    assert "opus" in FALLBACK_WORKER_MODEL

    # The design pass defaults to the Opus tier (unless an operator set a
    # per-install override via CBCL_GENERATION_MODEL).
    if not os.environ.get("CBCL_GENERATION_MODEL", "").strip():
        assert _DEFAULT_GENERATION_MODEL == FALLBACK_MANAGER_MODEL
        assert "opus" in _DEFAULT_GENERATION_MODEL
