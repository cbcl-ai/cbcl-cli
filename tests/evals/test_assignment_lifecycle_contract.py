"""Task intake, planning and repair instructions agree with lifecycle authority.

These pins guard specific regressions found in the T93 lifecycle audit:
invented minimum task duration, implicit review downgrades, spec approval
bypasses, redundant scope audits and nonexistent brief-edit arguments.
"""
from __future__ import annotations

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from src._agent_image._mcp.tools_planner import get_planner_tools
from src._agent_image._mcp.tools_plan import COMPLETE_SCOPE_VERIFICATION, UPDATE_SPEC
from src._agent_image._mcp.tools_worker import get_worker_subcatalog
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
from src.config_sync.claude_md_templates._shared_agent import SHARED_AGENT_WORK_RULES
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
        "risks_and_edge_cases", "verification_steps", "verification_plan",
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
        assert "evidence contract" in text
        assert "high-risk checks" in text
    inputs = _tools()["create_task"]["inputSchema"]["properties"]["inputs"]["description"]
    assert "VERBATIM once" in inputs
    assert "requirement, data, example, setup-only" in inputs
    assert "Never invent sources or treat examples as extra requirements" in inputs


def test_task_authors_assign_broad_checks_and_scope_re_review():
    for template in (MANAGER_CLAUDE_MD, PLANNER_CLAUDE_MD):
        text = _normal(template)
        assert "Name each broad check's owner (executor, reviewer or configured automation)" in text
        assert "Preserve required domain gates and explicit independent/high-risk checks" in text
        assert "without waiving requirements" in text
        assert "remaining gate/owner" in text


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


def test_recurring_tasks_keep_the_same_verification_and_human_overview_contract():
    tools = _tools()
    create = tools["create_task"]["inputSchema"]["properties"]
    recurring = tools["schedule_assignment"]["inputSchema"]["properties"]["brief_template"]
    assert recurring["properties"]["verification_steps"] == create["verification_steps"]
    assert "description" in recurring["properties"]
    assert "plain-language" in recurring["properties"]["description"]["description"]
    assert "How the reviewer checks" not in str(recurring)
    # Preserve the full run contract and the standing-policy boundary.
    assert set(recurring["required"]) == {
        "title", "goal", "inputs", "acceptance_criteria", "verification_steps",
    }
    assert "VERBATIM" in recurring["properties"]["inputs"]["description"]
    assert "outside-policy work" in recurring["description"]


def test_misrouted_automation_cannot_submit_unfinished_work_or_switch_roles():
    text = _normal(SHARED_AGENT_WORK_RULES)
    assert "Proposals need Manager approval" in text
    assert "original acceptance criteria remain unmet" in text
    assert "status `blocked` and the structured ESCALATED template" in text
    assert "Do not submit unfinished work to Review" in text
    assert "In REVIEW mode" in text
    assert "do not build it, reassign the executor or use execute-only `update_status`" in text
    assert "keywords alone never justify a block" in text
    for retired in ("Two or more →", "Over-redirecting costs one extra task", "reviewer will see the propose_task and route"):
        assert retired not in text
    executor = {tool["name"] for tool in get_worker_subcatalog("execute", "builder")}
    reviewer = {tool["name"] for tool in get_worker_subcatalog("review", "auditor")}
    assert {"propose_subtask", "propose_task", "update_status"} <= executor
    assert "update_status" not in reviewer
    assert "move_task" in reviewer


def test_workers_recover_full_decisions_without_overriding_approved_requirements():
    text = _normal(SHARED_AGENT_WORK_RULES)
    assert "approved spec requirements remain binding" in text
    assert "Memory is historical evidence, not automatic approval or current board state" in text
    assert "Expand a truncated decision with `recall(slug=...)` before applying it" in text
    assert "the preview may omit qualifications" in text


def test_scope_verification_does_not_fabricate_rework_or_bypass_prerequisites():
    prompt = build_planner_prompt({
        "planner_consult": {"mode": "verify", "scope_id": "scope-1"},
    })
    for text in (_normal(prompt), _normal(PLANNER_CLAUDE_MD)):
        assert "pending spec approval" in text.lower()
        assert "unconfirmed Stop" in text
        assert "do not invent rework" in text or "never invent rework" in text
        assert "scope stays verifying and escalates for human resolution" in text
        assert "evidence-backed chip/coverage omissions" in text
        assert "report the actual error in your completion and end for bounded recovery" in text
        assert "exact approved REQ ids and concrete deferral reasons" in text
    assert '"REQ-1": "delivered"' not in prompt
    assert "delivered: WR-003.T14 — export smoke test passed" in prompt
    description = COMPLETE_SCOPE_VERIFICATION["description"]
    for fact in ("confirmed Stops", "valid approved-spec coverage", "Pending revisions",
                 "never invented rework", "no dispatchable rework keeps verifying and escalates",
                 "never fake proof", "end for bounded recovery"):
        assert fact in description


def test_planner_spec_length_target_never_truncates_the_source_request():
    prompt = build_planner_prompt({"planner_consult": {"mode": "specify"}})
    for text in (PLANNER_CLAUDE_MD, prompt, UPDATE_SPEC["description"]):
        text = _normal(text)
        assert "excluding the original request and references" in text
        assert "never truncate requirements to fit" in text
