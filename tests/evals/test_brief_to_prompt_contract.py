"""Eval: every required brief field surfaces in the worker prompt.

The Manager builds Briefs via the create_task tool; if a field doesn't
make it into the worker's prompt, the worker can't see it and the
acceptance check fails silently. This test catches drift between the
brief schema and the prompt template.
"""

from __future__ import annotations

import pytest

from src.orchestrator.worker_prompt import format_task_brief


_FULL_BRIEF = {
    "goal": "EVAL-GOAL: distinct token for goal",
    "context": "EVAL-CONTEXT: distinct token for context",
    "inputs": "EVAL-INPUTS: distinct token for inputs",
    "output_format": "EVAL-OUTPUT-FORMAT: distinct token",
    "acceptance_criteria": [
        "EVAL-AC-1: first criterion",
        "EVAL-AC-2: second criterion",
    ],
    "allowed_tools": ["EVAL-TOOL-A", "EVAL-TOOL-B"],
    "required_skills": ["EVAL-SKILL-X"],
    "risks_and_edge_cases": "EVAL-RISKS: distinct token",
    "verification_steps": "EVAL-VERIFY: distinct token",
}


def _task(**overrides):
    base = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "WR-001.T01",
        "title": "Eval task",
        "status": "ready",
        "rework_count": 0,
        "brief": _FULL_BRIEF,
        "workstream_short_code": "WR",
    }
    base.update(overrides)
    return base


def test_all_brief_fields_surface_in_prompt():
    """Every distinct token in _FULL_BRIEF must appear in the rendered prompt."""
    prompt = format_task_brief(_task())

    expected_tokens = [
        "EVAL-GOAL:",
        "EVAL-CONTEXT:",
        "EVAL-INPUTS:",
        "EVAL-OUTPUT-FORMAT:",
        "EVAL-AC-1:",
        "EVAL-AC-2:",
        "EVAL-TOOL-A",
        "EVAL-TOOL-B",
        "EVAL-SKILL-X",
        "EVAL-RISKS:",
        "EVAL-VERIFY:",
    ]
    for tok in expected_tokens:
        assert tok in prompt, (
            f"Missing token '{tok}' from prompt. The brief field that "
            f"carries it is not surfacing — the worker won't see it."
        )


def test_acceptance_criteria_render_as_checklist():
    prompt = format_task_brief(_task())
    # Each AC item must render with a checkbox marker so the worker
    # sees them as discrete items, not a block of prose.
    assert "- [ ] EVAL-AC-1:" in prompt
    assert "- [ ] EVAL-AC-2:" in prompt


def test_missing_brief_fields_degrade_gracefully():
    """A brief with empty optional fields must not crash or render junk."""
    minimal = {
        "goal": "G", "context": "C", "inputs": "None",
        "output_format": "OF",
        "acceptance_criteria": ["AC1"],
        # Intentionally missing: allowed_tools, required_skills,
        # risks_and_edge_cases, verification_steps
    }
    task = _task(brief=minimal)
    prompt = format_task_brief(task)

    # Must not raise. Must keep the task-id header. Falsy fields should
    # render with a sensible placeholder, not "None" stringification of
    # None or KeyError.
    assert "Task UUID" in prompt
    assert "## Goal\nG" in prompt
    assert "## Context\nC" in prompt
    # Empty allowed_tools must not say "None,None" or similar.
    assert "## Allowed Tools" in prompt


def test_rework_feedback_appears_when_present():
    feedback = "EVAL-FEEDBACK: previous submission missed AC-2"
    task = _task(rework_count=1, rework_feedback=feedback)
    prompt = format_task_brief(task)

    assert "REWORK REQUIRED" in prompt
    assert feedback in prompt


def test_rework_feedback_absent_when_count_zero():
    task = _task(rework_count=0)
    prompt = format_task_brief(task)
    assert "REWORK REQUIRED" not in prompt


def test_artifacts_block_appears_when_artifacts_present():
    task = _task(artifacts=[
        {"file_path": "/workspace/outputs/WR/wr_001_t01_report.md",
         "file_title": "Report"},
    ])
    prompt = format_task_brief(task)

    assert "EXISTING DELIVERABLES" in prompt
    assert "wr_001_t01_report.md" in prompt
