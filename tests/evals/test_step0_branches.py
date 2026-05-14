"""Eval: STEP 0 branch selection in the worker prompt.

STEP 0 is the worker's "assess current state" preamble. The branch the
prompt steers the worker into depends on:
  - rework_count > 0 → Branch D (REWORK)
  - registered artifacts present → Branch C (ARTIFACTS PRESENT)
  - recent_activities present, no artifacts → Branch B (PARTIAL WORK LIKELY)
  - none of the above → Branch A (FRESH TASK)

Picking the wrong branch causes duplicate work or skipped recovery.
"""

from __future__ import annotations

from src.orchestrator.worker_prompt import format_task_brief


_BRIEF = {
    "goal": "G", "context": "C", "inputs": "None",
    "output_format": "OF",
    "acceptance_criteria": ["AC1"],
    "allowed_tools": ["Read"],
    "required_skills": [],
    "risks_and_edge_cases": "None",
    "verification_steps": "VS",
}


def _task(**overrides):
    base = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "WR-001.T01",
        "title": "Eval task",
        "status": "ready",
        "rework_count": 0,
        "brief": _BRIEF,
        "workstream_short_code": "WR",
    }
    base.update(overrides)
    return base


def test_branch_a_fresh_task():
    """No rework, no artifacts, no activity → Branch A."""
    prompt = format_task_brief(_task())
    assert "BRANCH A (FRESH TASK)" in prompt
    assert "BRANCH B" not in prompt
    assert "BRANCH C" not in prompt
    assert "BRANCH D" not in prompt


def test_branch_b_partial_work_likely():
    """Activity exists, no artifacts → Branch B."""
    prompt = format_task_brief(_task(recent_activities=[
        {"event_type": "checkpoint", "actor": "x", "content": "started"},
    ]))
    assert "BRANCH B (PARTIAL WORK LIKELY)" in prompt
    assert "BRANCH A" not in prompt


def test_branch_c_artifacts_present():
    """Artifacts present → Branch C (regardless of activity)."""
    prompt = format_task_brief(_task(artifacts=[
        {"file_path": "/workspace/outputs/WR/x.md", "file_title": "X"},
    ]))
    assert "BRANCH C (ARTIFACTS PRESENT)" in prompt
    # Branch C wins over Branch B even when activity is also present.
    prompt_with_both = format_task_brief(_task(
        artifacts=[{"file_path": "/workspace/outputs/WR/x.md"}],
        recent_activities=[
            {"event_type": "checkpoint", "actor": "x", "content": "y"},
        ],
    ))
    assert "BRANCH C (ARTIFACTS PRESENT)" in prompt_with_both


def test_branch_d_rework_wins_over_others():
    """rework_count > 0 → Branch D (regardless of artifacts/activity)."""
    prompt = format_task_brief(_task(
        rework_count=2,
        rework_feedback="please fix X",
        artifacts=[{"file_path": "/workspace/outputs/WR/x.md"}],
    ))
    assert "BRANCH D (REWORK)" in prompt
    # The artifacts section should still render so the worker knows
    # what's already on disk.
    assert "EXISTING DELIVERABLES" in prompt


def test_step0_globs_include_workstream_subdir():
    """STEP 0.3 globs must point at the per-workstream output dir,
    not the legacy flat /workspace/outputs/ root."""
    prompt = format_task_brief(_task(workstream_short_code="WR"))
    # The per-workstream glob path must appear.
    assert "/workspace/outputs/WR/" in prompt
    # The legacy flat path is also referenced (as fallback for
    # pre-separation runs) — that's intentional.


def test_step0_glob_uses_readable_id_slug():
    """Glob patterns must use the readable_id (lowercased, dot→underscore)
    so they match the file-naming convention the worker uses."""
    prompt = format_task_brief(_task(readable_id="WR-001.T07"))
    # readable_slug = "wr-001_t07" (lower + replace . with _)
    assert "wr-001_t07*" in prompt
