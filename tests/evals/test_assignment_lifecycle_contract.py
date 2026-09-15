"""Task intake, planning and repair instructions agree with lifecycle authority.

These pins guard specific regressions found in the T93 lifecycle audit:
invented minimum task duration, implicit review downgrades, spec approval
bypasses, redundant scope audits and nonexistent brief-edit arguments.
"""
from __future__ import annotations

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
from src.config_sync.claude_md_templates._system_agents._planner import PLANNER_CLAUDE_MD
from src.orchestrator.planner_prompt import build_planner_prompt


def _normal(text: str) -> str:
    return " ".join(text.split())


def _tools(factory=get_manager_tools) -> dict:
    return {tool["name"]: tool for tool in factory()}


def test_short_assignment_neither_expands_scope_nor_lowers_quality():
    manager = _normal(MANAGER_CLAUDE_MD)
    assert "A focused 15-minute request is a valid standalone assignment" in manager
    assert "never enlarge it to meet a minimum duration" in manager
    assert "Cover every required outcome" in manager
    for retired in (
        "a task finishable in <20 min",
        "15-minute brief merges",
        "speed beats reviewability",
        "under 5 acceptance criteria",
    ):
        assert retired not in manager


def test_ask_class_cannot_be_used_as_a_review_bypass():
    props = _tools()["create_task"]["inputSchema"]["properties"]
    desc = props["task_class"]["description"]
    assert "bounded informational" in desc
    assert "never use for fixes, publishing, security certification" in desc
    assert "persistent state changes" in desc
    assert "Never use ask-class to skip required review" in _normal(MANAGER_CLAUDE_MD)


def test_program_single_pass_keeps_approved_spec_and_scope_verification():
    manager = _normal(MANAGER_CLAUDE_MD)
    planner = _normal(PLANNER_CLAUDE_MD)
    assert "with an APPROVED spec" in manager
    assert "collapses planning passes, never the spec or approval gate" in manager
    assert "EVERY program scope needs a short execution plan with evidence chips" in planner
    assert "including a one-task milestone" in planner
    assert "A scope's last task starts Planner verification, not automatic approval" in manager
    assert "1-2 task scope does NOT need an execution plan" not in planner
    assert set(_tools(get_planner_tools)).isdisjoint({
        "create_scope", "activate_scope", "archive_scope",
    })


@pytest.mark.parametrize("factory", [get_manager_tools, get_planner_tools])
def test_brief_repair_schema_is_partial_and_preserves_field_types(factory):
    tools = _tools(factory)
    create = tools["create_task"]["inputSchema"]["properties"]
    update = tools["update_task"]["inputSchema"]["properties"]
    brief = update["brief"]
    assert brief["type"] == "object"
    assert brief["minProperties"] == 1
    assert brief["additionalProperties"] is False
    assert "required" not in brief  # omitted brief fields remain unchanged
    assert set(brief["properties"]) == {
        "goal", "context", "inputs", "output_format", "acceptance_criteria",
        "allowed_tools", "required_skills", "reference_doc_ids",
        "risks_and_edge_cases", "verification_steps",
    }
    for field, schema in brief["properties"].items():
        assert schema == {key: value for key, value in create[field].items() if key != "description"}
        assert field not in update  # never advertise silently ignored top-level fields
    assert update["spec_revision"]["type"] == "integer"
    assert update["spec_revision"]["minimum"] == 1
    assert "omitted preserves" in update["spec_revision"]["description"]


def test_brief_repair_does_not_rewrite_running_work_or_fake_materialize_updates():
    prompt = build_planner_prompt({
        "planner_consult": {"mode": "materialize", "scope_id": "scope-1"},
    })
    assert "update_task(brief={...}, spec_revision=<approved revision>)" in prompt
    assert "only in backlog/ready/blocked" in prompt
    assert "Never rewrite in_progress/review work" in prompt
    assert "never-executed task in this consult's scope" in prompt
    assert "previously executed blocked work needs Manager repair" in prompt
    planner = _normal(PLANNER_CLAUDE_MD)
    assert "A `create_task` retry does not replace a complete brief" in planner
    assert "Brief repair alone does not resume blocked work" in planner


def test_task_sources_and_verification_responsibilities_are_explicit():
    for template in (MANAGER_CLAUDE_MD, PLANNER_CLAUDE_MD):
        text = _normal(template)
        for heading in ("Execution checks", "Independent review", "Evidence handoff"):
            assert heading in text
        assert "exact revision and relevant environment/inputs" in text
        assert "high-risk checks" in text
    inputs = _tools()["create_task"]["inputSchema"]["properties"]["inputs"]["description"]
    assert "VERBATIM once" in inputs
    assert "requirement, data, example, setup-only" in inputs
    assert "Never invent sources or treat examples as extra requirements" in inputs


def test_scope_verification_checks_integration_without_replaying_every_task_audit():
    prompt = build_planner_prompt({
        "planner_consult": {"mode": "verify", "scope_id": "scope-1"},
    })
    for text in (_normal(prompt), _normal(PLANNER_CLAUDE_MD)):
        assert "cross-task interfaces" in text
        assert "changed/high-risk behavior" in text
        assert "exact revision and relevant environment/inputs" in text
        assert "worker's PASS claim is insufficient" in text or 'worker\'s "PASS" claim is insufficient' in text
        assert "Do not replay every task audit" in text
        assert "LAST act of YOUR main session" in text


def test_native_command_execution_is_not_a_phantom_session_boundary():
    planner = _normal(PLANNER_CLAUDE_MD)
    assert "`execute_script` is the worker's LAST act" in planner
    assert "git pushes and bounded CI checks do NOT themselves end a session" in planner
    assert "Never split solely because a tool uses a subprocess" in planner


def test_manager_template_still_renders_after_nested_brief_examples():
    rendered = MANAGER_CLAUDE_MD.format(
        office_name="Test office", manager_tool_allowlist="get_board, create_task",
    )
    assert "update_task(brief={...})" in rendered
