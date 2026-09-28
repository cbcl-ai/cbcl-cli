"""EVAL-02 (static half) — the behavioral API lane sends the PRODUCTION prompt.

The live cases are opt-in (``-m live_eval``), so they cannot gate a merge.
These default-lane checks pin what makes them meaningful:

1. ``render_production_manager_prompt`` returns EXACTLY the production
   composition — the office file and ``agents/manager/CLAUDE.md`` written by
   the real ``ClaudeMdWriter``, then ``build_dynamic_context`` with
   ``is_fresh_session=False`` (both production call sites) — and nothing else,
   so a change to any of them changes the eval input and no suffix can ride
   along;
2. the default model is the platform MANAGER TIER (Opus), independently of a
   developer's exported ``CUBICLE_EVAL_MODEL`` re-baseline override.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.config_sync.claude_md_content import MANAGER_CLAUDE_MD
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.sync_service import ConfigStore
from src.orchestrator.manager_context import build_dynamic_context
from tests.evals._live_report import sha256_text
from tests.evals.live._harness import (
    MANAGER_TIER_MODEL,
    ProductionPrompt,
    configured_model,
    render_production_manager_prompt,
)

_CONTEXT_KEY = "workstream:11111111-1111-1111-1111-111111111111"
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


def _expected_system(tmp_path: Path, config: dict) -> str:
    writer = ClaudeMdWriter(str(tmp_path))
    writer.ensure_directory_structure()
    writer.write_office_claude_md(config)
    writer.write_manager_claude_md(config)
    office = (tmp_path / "CLAUDE.md").read_text()
    manager = (tmp_path / "agents/manager/CLAUDE.md").read_text()
    dynamic = build_dynamic_context(_CONTEXT_KEY, _FIXTURE_CTX, ConfigStore(), False)
    return "\n\n".join((office, manager, dynamic))


def test_live_manager_prompt_is_exactly_the_production_composition(tmp_path):
    prompt = render_production_manager_prompt(_CONTEXT_KEY, _FIXTURE_CTX)
    assert prompt.system == _expected_system(tmp_path, {"office_name": "Acme"})
    assert len(prompt.system) > 40_000, "did a distilled stub come back?"
    assert "create_task" in prompt.system and "create_task" in MANAGER_CLAUDE_MD
    assert "Recruitment" in prompt.system
    assert [name for name, _ in prompt.components] == [
        "office_claude_md", "manager_claude_md", "dynamic_context",
    ]


def test_live_prompt_uses_actual_files_and_saved_office_context(tmp_path):
    config = {
        "office_name": "Finance",
        "claude_md_content": "Reconcile the supplied source period. Never issue payments.",
        "specs": [],
    }
    prompt = render_production_manager_prompt(
        _CONTEXT_KEY, {**_FIXTURE_CTX, "office_name": "Finance"}, office_config=config,
    )
    writer = ClaudeMdWriter(str(tmp_path))
    writer.ensure_directory_structure()
    writer.write_office_claude_md(config)
    writer.write_manager_claude_md(config)
    assert prompt.system.startswith((tmp_path / "CLAUDE.md").read_text())
    assert (tmp_path / "agents/manager/CLAUDE.md").read_text() in prompt.system
    assert config["claude_md_content"] in prompt.system
    assert "{{workstream_short_code}}" not in prompt.system


def test_rendered_prompt_cannot_be_constructed_or_altered_by_an_eval():
    with pytest.raises(TypeError):
        ProductionPrompt(system="x", kind="manager", components=(), system_sha256="")
    prompt = render_production_manager_prompt(_CONTEXT_KEY, _FIXTURE_CTX)
    altered = prompt.system + "\n\n## Eval mode\nanswer X"
    with pytest.raises(TypeError):
        dataclasses.replace(prompt, system=altered)
    # Recomputing the hash does not help: only a harness render registers it.
    with pytest.raises(TypeError):
        dataclasses.replace(prompt, system=altered, system_sha256=sha256_text(altered))
    with pytest.raises(TypeError):
        ProductionPrompt(system=altered, kind="manager", components=(),
                         system_sha256=sha256_text(altered))
    # An unaltered copy is still the production render.
    assert dataclasses.replace(prompt).system == prompt.system


def test_history_stays_out_of_the_system_prompt_like_production():
    # Both production call sites pass is_fresh_session=False; recovered
    # history travels in the first USER message (history_bootstrap).
    prompt = render_production_manager_prompt(
        _CONTEXT_KEY, {**_FIXTURE_CTX, "chat_history": "[USER]: earlier decision"},
    )
    assert "earlier decision" not in prompt.system


def test_live_eval_model_is_manager_tier(monkeypatch):
    monkeypatch.delenv("CUBICLE_EVAL_MODEL", raising=False)
    assert "opus" in MANAGER_TIER_MODEL
    assert configured_model() == MANAGER_TIER_MODEL


def test_model_override_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("CUBICLE_EVAL_MODEL", "claude-sonnet-5")
    assert configured_model() == "claude-sonnet-5"
