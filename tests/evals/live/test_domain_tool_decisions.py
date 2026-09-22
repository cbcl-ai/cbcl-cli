"""Opt-in real-model tool decisions against actual composed Manager instructions.

Synthetic source descriptions only; returned tool calls are inspected, never
executed against an Office. These are model-adherence checks, not runtime E2E.
"""
import json

import pytest

from src._agent_image._mcp.tools_manager import get_manager_tools
from tests.evals.live._harness import call_claude, render_production_manager_prompt

pytestmark = pytest.mark.live_eval


@pytest.mark.parametrize("domain,user_request,evidence_terms", [
    ("Recruitment", "Prepare a candidate evidence report from uploaded candidates.csv and rubric.txt. Cite each candidate's job-related evidence; do not contact anyone.", ("candidate", "rubric")),
    ("Finance", "Reconcile uploaded invoices.csv for September against supplied ledger.csv. Preserve the source records and list unmatched invoices. Do not issue payments.", ("invoice", "ledger")),
    ("Marketing", "Draft a campaign package from uploaded brief.txt and approved-claims.txt. Substantiate claims, inspect final layout and links; do not publish.", ("claim", "layout")),
])
async def test_file_only_assignment_selects_real_task_tool_without_invented_software_gate(domain, user_request, evidence_terms):
    catalog = get_manager_tools()
    tools = [tool for tool in catalog if tool["name"] in {"create_task", "ask_user_choice"}]
    system = render_production_manager_prompt("workstream:11111111-1111-1111-1111-111111111111", {
        "office_name": domain, "workstream_name": domain,
        "workstream_id": "11111111-1111-1111-1111-111111111111",
        "team_roster": "Analyst (analyst), Builder (builder), Auditor (auditor)",
    })
    response = await call_claude(system=system, user=(
        "Create one new assignment for this request. The named files are uploaded "
        "and available in this Workstream's assigned Inputs; no external service is "
        "required. Do not perform the work yourself.\n" + user_request
    ), tools=tools, max_tokens=2500)
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call["name"] == "create_task"
    output = call["input"]
    assert user_request in output["inputs"]
    assert output["assigned_agent"] != output["reviewer"]
    verification = output["verification_steps"].lower()
    for section in ("execution checks", "independent review", "evidence handoff"):
        assert section in verification
    for term in evidence_terms:
        assert term in json.dumps(output, ensure_ascii=False).lower()
    for invented in ("gitlab", "git commit", "pnpm", "npm test", "pipeline", "commit sha"):
        assert invented not in verification
