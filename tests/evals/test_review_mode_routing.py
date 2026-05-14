"""Eval: review-mode prompts route to the right authority block.

The worker prompt grows different authority sections depending on:
  - executor (status != "review") → no review block
  - non-designated reviewer (status=="review", agent != reviewer) →
    REVIEWER block (post verdict, unassign, no move_task)
  - designated reviewer (status=="review", agent == reviewer) →
    DESIGNATED REVIEWER block (full move_task authority)
  - Manager Assistant in review (Board Operator) → NO review block at
    all; MA's CLAUDE.md drives behaviour separately.
"""

from __future__ import annotations

from src.orchestrator.worker_prompt import build_worker_prompt


_BRIEF = {
    "goal": "G", "context": "C", "inputs": "None",
    "output_format": "OF",
    "acceptance_criteria": ["AC1"],
    "allowed_tools": ["Read"],
    "required_skills": [],
    "risks_and_edge_cases": "None",
    "verification_steps": "VS",
}


def _task(*, status, agent, reviewer):
    return {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "WR-001.T01",
        "title": "Eval task",
        "status": status,
        "rework_count": 0,
        "brief": _BRIEF,
        "workstream_short_code": "WR",
        "assigned_agent": agent,
        "reviewer": reviewer,
    }


def test_executor_gets_no_reviewer_block():
    """Status != review → no reviewer-mode block."""
    prompt = build_worker_prompt(
        _task(status="ready", agent="developer", reviewer="auditor"),
    )
    assert "YOUR ROLE: REVIEWER" not in prompt
    assert "YOUR ROLE: DESIGNATED REVIEWER" not in prompt


def test_non_designated_reviewer_block_for_review_status_no_match():
    """status=review and agent != reviewer → non-designated reviewer block."""
    prompt = build_worker_prompt(
        _task(status="review", agent="developer", reviewer="auditor"),
    )
    # Either flow can fire depending on whether 'reviewer' is set; the
    # current convention is that non-designated reviewer is used when
    # the agent doesn't match.
    # Here agent=developer, reviewer=auditor → developer is NOT the
    # designated reviewer; should NOT get the designated block.
    assert "YOUR ROLE: REVIEWER" in prompt
    assert "YOUR ROLE: DESIGNATED REVIEWER" not in prompt
    # Non-designated must NOT call move_task — verified by the
    # explicit prohibition in the block.
    assert "Do NOT call `update_status`" in prompt or "Do NOT call `move_task`" in prompt


def test_designated_reviewer_block_when_agent_matches_reviewer():
    prompt = build_worker_prompt(
        _task(status="review", agent="auditor", reviewer="auditor"),
    )
    assert "YOUR ROLE: DESIGNATED REVIEWER" in prompt
    # Designated reviewer CAN call move_task.
    assert "move_task" in prompt
    # And must NOT execute work.
    assert "Do NOT execute the task" in prompt


def test_manager_assistant_in_review_gets_no_review_block():
    """Manager Assistant in review-status mode is a Board Operator,
    not a reviewer. It should NOT receive review-mode instructions —
    its CLAUDE.md drives behaviour separately."""
    prompt = build_worker_prompt(
        _task(status="review", agent="manager-assistant", reviewer="auditor"),
    )
    assert "YOUR ROLE: REVIEWER" not in prompt
    assert "YOUR ROLE: DESIGNATED REVIEWER" not in prompt
