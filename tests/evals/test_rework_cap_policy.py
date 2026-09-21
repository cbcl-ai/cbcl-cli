"""Reviewer policy: fixable failures return for rework without a count limit.

T160 exposed the old instruction to leave capped failures in Review. Keep the
real rendered prompts aligned while preserving human and runtime recovery holds.
"""
from __future__ import annotations

import pytest

from src.config_sync.claude_md_content import (
    AUDITOR_CLAUDE_MD,
    MANAGER_ASSISTANT_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
)
from src.orchestrator.worker_prompt import build_worker_prompt
from src._agent_image._mcp.tools_worker import get_worker_tools
from src._agent_image._mcp.transforms import transform_params


def _reviewer_prompt(rework_count: int = 2, reviewer: str = "auditor") -> str:
    return build_worker_prompt({
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "RC-001.T05",
        "title": "x", "status": "review", "rework_count": rework_count,
        "recent_activities": [], "artifacts": [], "reviewer": reviewer,
        "assigned_agent": "dev",
        "brief": {
            "goal": "g", "context": "c", "inputs": "i",
            "output_format": "short", "acceptance_criteria": ["a"],
            "allowed_tools": [], "required_skills": [],
            "risks_and_edge_cases": "none", "verification_steps": "v",
        },
    })


@pytest.mark.parametrize("rework_count", [0, 1, 2, 3, 20])
@pytest.mark.parametrize("reviewer", ["auditor", "custom-reviewer"])
def test_rendered_reviewer_returns_fixable_failures_at_any_count(rework_count, reviewer):
    prompt = _reviewer_prompt(rework_count, reviewer)
    fail_branch = prompt.split("**If FAIL (critical issues):**", 1)[1].split(
        "**Lessons are captured", 1
    )[0]
    assert 'new_status = "ready"' in fail_branch
    assert 'overall: "fail"' in fail_branch
    assert "required_fixes" in fail_branch
    assert "any number of rework cycles" in fail_branch
    assert 'new_status = "blocked"' not in fail_branch
    assert "Rework has no count limit" in prompt
    assert "Never rubber-stamp approve" in prompt
    if rework_count:
        assert f"Rework #{rework_count}" in prompt


@pytest.mark.parametrize("surface", [
    AUDITOR_CLAUDE_MD, MANAGER_ASSISTANT_CLAUDE_MD, MANAGER_CLAUDE_MD,
])
def test_static_review_surfaces_have_no_count_stop_rule(surface):
    text = " ".join(surface.split())
    assert "Rework has no count limit" in text
    assert "`rework_count` is history, not a stopping rule" in text
    assert "genuine blocker" in text.lower() or "genuine workstream blocker" in text.lower()
    assert "rework cap (default" not in text
    assert "At the rework cap" not in text
    assert "Leave the task in `review`" not in text


def test_backend_auditor_default_cannot_reintroduce_a_count_limit():
    from tests.backend_boundary import import_backend

    prompt = import_backend("app.agents.system_agents").AUDITOR_DEFAULT_PROMPT
    assert "Rework has no" in prompt and "count limit" in prompt
    assert "regardless of" in prompt and "rework count" in prompt
    assert "At the rework cap" not in prompt


def test_real_blocker_and_existing_human_decisions_remain_protected():
    prompt = _reviewer_prompt(20)
    genuine_blocker = prompt.split("**Genuine blockers are separate", 1)[1]
    assert 'new_status = "blocked"' in genuine_blocker
    assert "ESCALATED (<blocker_class>):" in genuine_blocker
    assert "actual human-only decisions" in genuine_blocker
    ma = " ".join(MANAGER_ASSISTANT_CLAUDE_MD.split())
    assert "Do not bypass an existing pending human request" in ma
    assert "legacy rework-cap request" in ma
    assert "NEVER auto-unblock a blocked task" in ma


def test_legacy_tool_flag_is_compatible_without_instructing_new_cap_escalations():
    tool = next(t for t in get_worker_tools() if t["name"] == "escalate_blocker")
    field = tool["inputSchema"]["properties"]["rework_cap"]
    assert field["type"] == "boolean"
    assert "Legacy compatibility field; leave false/unset" in field["description"]
    assert "Rework has no count limit" in field["description"]
    assert "2 failed rework cycles" not in field["description"]
    # Old in-flight callers retain their user-only marker; the prompt change
    # does not silently downgrade a durable human decision into an auto-action.
    params = transform_params("propose_action", "escalate_blocker", {
        "blocker_summary": "Legacy human decision", "blocker_class": "unknown",
        "justification": "Previously submitted", "rework_cap": True,
    })
    assert params["payload"]["rework_cap"] is True


def test_no_reviewer_surface_instructs_silent_auto_approve():
    # Negative guard across every reviewer-facing surface — the reviewer block,
    # the MA playbook, AND the Auditor playbook. (The Auditor playbook currently
    # has no "auto-approv" mention, so the scan is a no-op there today; it
    # future-proofs against one being added. Its POSITIVE coverage is the test
    # above. The Manager review section is covered by T5.2.3's backend test.)
    for surface in (_reviewer_prompt(), MANAGER_ASSISTANT_CLAUDE_MD,
                    AUDITOR_CLAUDE_MD):
        low = surface.lower()
        idx = 0
        while (idx := low.find("auto-approv", idx)) != -1:
            window = low[max(0, idx - 30): idx]
            assert any(neg in window for neg in ("never", "not ", "n't", "silent", "worse")), (
                f"'auto-approve' used as an instruction near: ...{low[idx-30:idx+20]}..."
            )
            idx += 1
