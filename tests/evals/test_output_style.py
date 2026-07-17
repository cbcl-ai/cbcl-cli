"""Eval: the AI Output-Style rules + bounded review-verdict template are pinned.

Background (fable/docs/08-design-records/ai-output-readability.md, Pillar A): AI output
was an unstructured wall of text because NO prompt instructed summary-first,
real-Markdown, blank-line-separated, bounded output — and the review verdict had
no template, so it rendered as a run-on blob.

These assertions are the enforcement teeth for that fix (per communicator
CLAUDE.md "Tool descriptions are prompts" review bar): if the Output-Style block
or the verdict template is removed/renamed, CI fails here and documents the rule
as intentional load-bearing prompt content.
"""

from __future__ import annotations

from src.config_sync.claude_md_content import (
    AUDITOR_CLAUDE_MD,
    MANAGER_CLAUDE_MD,
    SHARED_AGENT_WORK_RULES,
    SHARED_OFFICE_CLAUDE_MD,
)
from src.orchestrator.worker_prompt import build_worker_prompt


_BRIEF = {
    "goal": "G",
    "context": "C",
    "inputs": "None",
    "output_format": "OF",
    "acceptance_criteria": ["AC1"],
    "allowed_tools": ["Read"],
    "required_skills": [],
    "risks_and_edge_cases": "None",
    "verification_steps": "VS",
}


def _review_task(*, agent: str = "auditor") -> dict:
    return {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "readable_id": "WR-001.T01",
        "title": "Eval task",
        "status": "review",
        "rework_count": 0,
        "brief": _BRIEF,
        "workstream_short_code": "WR",
        "assigned_agent": agent,
        "reviewer": agent,
    }


# --- The office-wide Output Style block reaches every agent + the Manager ----


def test_shared_office_claude_md_has_output_style_block():
    text = SHARED_OFFICE_CLAUDE_MD
    assert "## Output Style" in text, "office Output Style section missing"
    # The four load-bearing rules (by intent, not exact wording).
    assert "Summary first" in text
    assert "real Markdown" in text or "real Markdown" in text.replace("**", "")
    # The blank-line rule is the direct fix for the run-on-blob symptom.
    assert "blank line between every block" in text.lower()
    # Ad-hoc markers are explicitly banned.
    assert "ad-hoc markers" in text.lower()


def test_shared_agent_rules_have_output_style_block():
    """Custom agents that may not read /workspace/CLAUDE.md still get the rules."""
    assert "## Output Style" in SHARED_AGENT_WORK_RULES
    assert "blank line between every block" in SHARED_AGENT_WORK_RULES.lower()


def test_manager_has_chat_reply_output_style():
    assert "## Output Style (your chat replies" in MANAGER_CLAUDE_MD
    assert "Lead with the outcome" in MANAGER_CLAUDE_MD


def test_office_template_exposes_output_style_slot():
    """Pillar D: the office CLAUDE.md exposes the {office_output_style} slot the
    writer fills from the office's configured output_style."""
    assert "{office_output_style}" in SHARED_OFFICE_CLAUDE_MD


# --- The bounded review-verdict template ------------------------------------


def test_reviewer_prompt_has_bounded_verdict_template():
    prompt = build_worker_prompt(_review_task())
    # The fixed verdict shape: bold verdict line + Criteria + Required fixes.
    assert "**VERDICT:" in prompt, "verdict line token missing from reviewer prompt"
    assert "### Criteria" in prompt
    assert "### Required fixes" in prompt
    # Bounded: long evidence is routed to a saved report file, not inline.
    assert "save_file" in prompt
    assert "NOT inline" in prompt
    # The structured verdict carrier (Pillar C): the reviewer passes a
    # machine-readable verdict object on the move_task call.
    assert "verdict" in prompt
    assert '"overall"' in prompt


def test_auditor_verdict_step_uses_template():
    assert "**VERDICT:" in AUDITOR_CLAUDE_MD
    assert "### Criteria" in AUDITOR_CLAUDE_MD


def test_move_task_tool_exposes_structured_verdict():
    """The reviewer move_task tool carries the structured verdict (Pillar C)."""
    from src._agent_image._mcp.tools_worker import get_worker_tools

    tools = {t["name"]: t for t in get_worker_tools()}
    move = tools.get("move_task")
    assert move is not None, "move_task tool missing from worker pool"
    props = move["inputSchema"]["properties"]
    assert "verdict" in props, "move_task is missing the structured verdict param"
    verdict_props = props["verdict"]["properties"]
    assert "overall" in verdict_props
    assert "criteria" in verdict_props


def test_add_activity_desc_does_not_generically_route_verdict():
    """The add_activity tool description must NOT generically tell reviewers to
    post their verdict via add_activity (that contradicts the single-move_task
    flow + would double-post). It may still mention the rework-cap escalation
    exception. (Per the communicator 'tool descriptions are prompts' bar.)"""
    from src._agent_image._mcp.tools_worker import get_worker_tools

    tools = {t["name"]: t for t in get_worker_tools()}
    desc = tools["add_activity"]["description"]
    # The old, unscoped wording must be gone.
    assert 'Reviewers also use "comment" to post their verdict.' not in desc
    # The verdict belongs on the move_task call.
    assert "move_task" in desc


def test_move_task_transform_forwards_verdict():
    """The move_task transform forwards a dict verdict to the backend."""
    from src._agent_image._mcp.transforms import transform_params

    out = transform_params(
        "move_task",
        "move_task",
        {
            "task_id": "T1",
            "new_status": "done",
            "comment": "**VERDICT: PASS**",
            "verdict": {"overall": "pass", "criteria": []},
        },
    )
    assert out.get("verdict") == {"overall": "pass", "criteria": []}
    # A non-dict / absent verdict must not leak a key.
    out2 = transform_params(
        "move_task", "move_task",
        {"task_id": "T1", "new_status": "ready", "comment": "x"},
    )
    assert "verdict" not in out2
