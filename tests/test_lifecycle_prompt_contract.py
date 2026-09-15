"""Role-boundary and verification regressions for the complete task lifecycle."""
import pytest

from src._agent_image._mcp.tools_worker import get_worker_subcatalog
from src._content_contracts import REVIEW_VERIFICATION_CONTRACT, WORKER_EXECUTION_CONTRACT
from src.orchestrator.worker_prompt import build_worker_prompt, format_task_brief


def task(**overrides):
    return {
        "task_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "readable_id": "LC-001.T01", "title": "Improve a widget", "status": "ready",
        "assigned_agent": "builder", "reviewer": "auditor", "task_class": "assignment",
        "workstream_context": {"name": "Project", "description": "Project context"},
        "workstream_has_spec": True,
        "artifacts": [{"file_path": "/workspace/outputs/LC/widget.html"}],
        "brief": {"goal": "Deliver the requested widget", "inputs": "Use assigned project /workspace/widget",
                  "acceptance_criteria": ["Keyboard completes the flow", "The result matches the design"],
                  "verification_steps": "Execution checks: build and test.\nIndependent review: inspect UI.",
                  "output_format": "Interactive HTML widget"},
        **overrides,
    }


@pytest.mark.parametrize("status", ["review", "blocked"])
def test_non_execution_phases_exclude_executor_instructions_but_keep_context(status):
    prompt = build_worker_prompt(task(status=status))
    assert WORKER_EXECUTION_CONTRACT not in prompt
    for forbidden in ("NON-NEGOTIABLE EXECUTION RULES", "Completion fence", "COMPLETED.json",
                      "BRANCH A", "BRANCH B", "BRANCH C", "BRANCH D", "How to Submit Your Work",
                      "After `execute_script` — End Your Session", "If You Need Clarification or Hit a Real Blocker"):
        assert forbidden not in prompt
    for retained in ("Keyboard completes the flow", "The result matches the design",
                     "/workspace/outputs/LC/widget.html", "workstreams/project/CLAUDE.md",
                     "workstreams/project/spec.md"):
        assert retained in prompt
    assert "Do not execute the original brief" in prompt


@pytest.mark.parametrize("status", ["backlog", "done", "archived", "unknown"])
def test_non_runnable_states_have_no_execution_or_review_authority(status):
    prompt = build_worker_prompt(task(status=status))
    assert "not admitted" in prompt
    assert "Stop this stale assignment" in prompt
    assert "move_task" not in prompt
    assert "update_status" not in prompt
    assert "Review Process" not in prompt


def test_auditor_review_of_manager_assistant_execution_uses_reviewer_identity():
    prompt = build_worker_prompt(task(status="review", assigned_agent="manager-assistant", reviewer="auditor"))
    assert "YOUR ROLE: DESIGNATED REVIEWER" in prompt
    assert prompt.count(REVIEW_VERIFICATION_CONTRACT) == 1


def test_manager_assistant_review_retains_board_operator_routing():
    prompt = build_worker_prompt(task(status="review", assigned_agent="builder", reviewer="manager-assistant"))
    assert "YOUR ROLE: DESIGNATED REVIEWER" not in prompt
    assert prompt.count(REVIEW_VERIFICATION_CONTRACT) == 1
    assert "Board Operator may assign a qualified reviewer first" in prompt


@pytest.mark.parametrize("task_class", ["assignment", "program", "op", "ask"])
def test_all_execution_classes_keep_requirements_and_single_verification_contract(task_class):
    prompt = build_worker_prompt(task(task_class=task_class))
    assert prompt.count(WORKER_EXECUTION_CONTRACT) == 1
    assert REVIEW_VERIFICATION_CONTRACT not in prompt
    assert "Keyboard completes the flow" in prompt
    assert "Preserve mandatory repository checks" in prompt
    assert "Independent review checks belong to" in prompt
    if task_class == "ask":
        assert "COMPLETED.json" not in prompt
        assert "How to Close This Ask Task" in prompt
    else:
        assert "COMPLETED.json" in prompt
        assert "How to Submit Your Work" in prompt


def test_review_evidence_reuse_cannot_hide_unverified_or_unsafe_work():
    prompt = " ".join(build_worker_prompt(task(status="review")).split())
    for contract in (
        "passing test that misses the requirement does not justify approval",
        "exact delivered revision, relevant environment and input scope",
        "self-written PASS, missing output or stale revision is insufficient",
        "Explicit independent checks and high-risk verification still apply",
        "Do not repeat production writes", "mark it PARTIAL", "all indices once",
        "failed/partial required criterion cannot be waived",
    ):
        assert contract in prompt


@pytest.mark.parametrize("mode", ["review", "triage"])
@pytest.mark.parametrize("agent", ["manager-assistant", "auditor"])
def test_non_executor_tool_catalog_cannot_submit_or_start_user_handoff(mode, agent):
    names = {tool["name"] for tool in get_worker_subcatalog(mode, agent)}
    assert "update_status" not in names
    assert "request_user_action" not in names
    assert "get_my_brief" in names


def test_new_review_tool_requires_indexed_evidence_for_each_criterion():
    move = next(tool for tool in get_worker_subcatalog("review", "auditor") if tool["name"] == "move_task")
    verdict = move["inputSchema"]["properties"]["verdict"]
    assert set(verdict["required"]) == {"overall", "rationale", "criteria"}
    entry = verdict["properties"]["criteria"]["items"]
    assert set(entry["required"]) == {"criterion_index", "name", "status", "evidence"}
    assert entry["properties"]["criterion_index"]["minimum"] == 1


def test_artifact_guidance_does_not_force_code_or_html_into_markdown():
    prompt = format_task_brief(task())
    assert "Interactive HTML widget" in prompt
    assert "required format" in prompt
    assert "edit product source in its assigned project" in prompt
    assert "Use .md only for Markdown" in prompt


def test_legacy_blank_criteria_keep_original_indices_without_empty_checkboxes():
    value = task()
    value["brief"]["acceptance_criteria"] = ["First", " ", "Third"]
    prompt = format_task_brief(value)
    assert "- [ ] 1. First" in prompt
    assert "- [ ] 3. Third" in prompt
    assert "- [ ] 2." not in prompt


def test_retry_permission_is_not_proof_of_resolution_or_permission_to_repeat_writes():
    prompt = " ".join(format_task_brief(task(blocked_bounce_count=1)).split())
    assert "does not prove the original issue is resolved" in prompt
    assert "never repeat a completed external write" in prompt


def test_review_feedback_remains_evidence_without_executor_rework_commands():
    prompt = build_worker_prompt(task(status="review", rework_feedback="Fix keyboard flow", rework_count=1))
    assert "Fix keyboard flow" in prompt
    assert "Prior review findings" in prompt
    assert "do not fix it yourself" in prompt
    assert "Address ALL feedback points above before resubmitting" not in prompt


def test_review_does_not_require_an_unrequested_report_or_drop_criteria_for_length():
    from src.config_sync.claude_md_content import AUDITOR_CLAUDE_MD
    prompt = " ".join(build_worker_prompt(task(status="review")).split())
    assert "Preserve every criterion" in prompt
    assert "FAIL/CONDITIONAL alone does not require a file" in prompt
    assert "only when the brief requests an audit artifact" in " ".join(AUDITOR_CLAUDE_MD.split())
