"""Render actual task/profile/authoring paths for business and development work."""
import pytest
import json
from unittest.mock import MagicMock

from src._content_contracts import CAPABILITY_RELEVANCE_CONTRACT, VERIFICATION_EVIDENCE_CONTRACT
from src._setup_prompts import INSTRUCTIONS_PROMPT, STANDALONE_SKILL_PROMPT, WORKSTREAM_CONTEXT_PROMPT
from src.config_sync.claude_md_writer import ClaudeMdWriter
from src.config_sync.claude_md_templates._manager import MANAGER_CLAUDE_MD
from src.orchestrator.worker_prompt import build_worker_prompt
from src.setup_generator import AGENT_SYSTEM_PROMPT_GEN_PROMPT, AGENT_INSTRUCTIONS_GEN_PROMPT, OFFICE_INSTRUCTIONS_PROMPT


CASES = [
    ("recruitment", "Use the uploaded candidate records and rubric v2. Do not contact candidates.", "Every finding cites the relevant record and job criterion."),
    ("finance", "Reconcile the supplied September invoices. Do not issue payments.", "Totals reconcile to the source snapshot; list unmatched records."),
    ("marketing", "Draft the supplied campaign brief using approved claims. Do not publish.", "Every factual claim cites its source; the final layout is readable."),
    ("development", "Use the assigned repository; required CI must pass for the submitted commit.", "Changed behavior and required browser coverage pass."),
]


@pytest.mark.parametrize("domain,inputs,criterion", CASES)
@pytest.mark.parametrize("status", ["ready", "review"])
def test_composed_worker_preserves_domain_contract_and_review_independence(domain, inputs, criterion, status):
    profile = {"name": f"{domain}-worker", "agent_type": "custom", "allowed_tools": ["Read", "Bash"], "system_prompt": f"Own {domain} deliverables."}
    task = {"task_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "status": status,
            "assigned_agent": profile["name"], "reviewer": "auditor", "task_class": "assignment",
            "workstream_context": {"name": domain},
            "brief": {"goal": f"Complete {domain} assignment", "inputs": inputs,
                      "acceptance_criteria": [criterion], "verification_steps": "Execution checks: verify sources. Independent review: inspect final output and critical claims."}}
    prompt = build_worker_prompt(task)
    stack = ClaudeMdWriter._get_agent_claude_md(profile) + "\n" + prompt
    assert inputs in prompt and criterion in prompt
    assert prompt.count(CAPABILITY_RELEVANCE_CONTRACT) == 1
    assert prompt.count(VERIFICATION_EVIDENCE_CONTRACT) == 1
    assert "current and evidence SHAs" not in stack
    assert "Git is Direct" not in stack
    assert "git push https://" not in stack
    assert "configured mandatory checks" in prompt or status == "review"
    if status == "ready":
        assert "An error or capacity refusal is not an accepted run or handoff" in prompt
    if status == "review":
        assert "Do not repeat production writes" in prompt
        assert "failed/partial required criterion cannot be waived" in " ".join(prompt.split())
        assert "How to Submit Your Work" not in prompt
        assert "produce it directly" not in prompt
        assert "Inspect the existing deliverables" in prompt


def test_ask_keeps_direct_answer_closure_without_an_invented_review_round():
    prompt = build_worker_prompt({"task_id": "ask-id", "status": "ready", "task_class": "ask",
        "assigned_agent": "manager-assistant", "brief": {"goal": "Identify the reporting period",
        "inputs": "Read the supplied report header", "acceptance_criteria": ["Answer cites header"],
        "verification_steps": "Check the header once"}})
    assert "there is no review round" in prompt
    assert "How to Close This Ask Task" in prompt
    assert "How to Submit Your Work" not in prompt


def test_authoring_surfaces_share_relevance_without_rewriting_saved_office_policy():
    for prompt in (INSTRUCTIONS_PROMPT, STANDALONE_SKILL_PROMPT, WORKSTREAM_CONTEXT_PROMPT,
                   AGENT_SYSTEM_PROMPT_GEN_PROMPT, AGENT_INSTRUCTIONS_GEN_PROMPT, OFFICE_INSTRUCTIONS_PROMPT):
        assert CAPABILITY_RELEVANCE_CONTRACT in prompt
    assert "configured automation" in MANAGER_CLAUDE_MD
    assert "Do not presume a repository or service" in MANAGER_CLAUDE_MD


def test_connector_metadata_does_not_claim_live_access_or_add_mandatory_tasks():
    playbook = ClaudeMdWriter._get_agent_claude_md({"name": "finance-worker", "agent_type": "custom",
        "connectors": [{"name": "accounting", "mcp_server_name": "accounting", "is_enabled": True}],
        "skills": [{"name": "reconciliation"}]})
    assert "configured MCP connection; confirm required tools/access at use" in playbook
    assert "assigned methods are not extra mandatory tasks" in playbook
    assert "MCP tools available via" not in playbook


def test_live_eval_harness_sends_real_tool_schemas_and_retains_decisions_without_executing(monkeypatch):
    from src._agent_image._mcp.tools_manager import get_manager_tools
    from tests.evals.live import _harness

    tool = next(t for t in get_manager_tools() if t["name"] == "create_task")
    reply = {"content": [{"type": "tool_use", "name": "create_task", "input": {"title": "Reconcile invoices"}}]}
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data))
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(reply).encode()
        return response

    monkeypatch.setattr(_harness.urllib.request, "urlopen", fake_urlopen)
    result = _harness._sync_call("synthetic-key", "synthetic-model", "actual rendered prompt", "request", 100, 0.0, [tool])
    assert captured[0]["tools"][0]["input_schema"] == tool["inputSchema"]
    assert "action" not in captured[0]["tools"][0]
    assert result.tool_calls == reply["content"]
    assert result.text == ""
