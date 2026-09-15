"""Real-model output checks using production prompts and synthetic inputs.

These checks need ANTHROPIC_API_KEY; they exercise text generation, not tool
execution or end-to-end office lifecycle. No real client data is submitted.
"""
import json
import re

import pytest

from src._setup_prompts import INSTRUCTIONS_PROMPT, WORKSTREAM_CONTEXT_PROMPT
from src._agent_image._mcp.tools_manager import get_manager_tools
from tests.evals.live._harness import call_claude, render_production_manager_prompt

pytestmark = pytest.mark.live_eval


def parse_object(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def assert_scannable(text, max_words):
    assert len(text.split()) <= max_words
    # A long unbroken paragraph is a different defect from total length.
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |#{1,6} ))", text)
    assert all(len(block.split()) <= 100 for block in blocks)


@pytest.mark.parametrize("user_request", [
    "Summarize the attached weekly bakery orders. Keep all quantities unchanged. Do not send customer messages.",
    "Build a simple appointment booking prototype in one assignment. Keep customer data local. No payments or deployment.",
    "For our approved program, implement the first milestone: import existing customer records. Keep the original IDs, skip duplicates, and never delete source data.",
])
async def test_assignment_overview_stays_short_and_inputs_remain_exact(user_request):
    schema = next(tool["inputSchema"] for tool in get_manager_tools() if tool["name"] == "create_task")
    system = render_production_manager_prompt("workstream:readability", {
        "office_name": "Readability Test", "workstream_name": "Customer service",
        "workstream_id": "11111111-1111-1111-1111-111111111111",
        "team_roster": "Builder (builder), Analyst (analyst), Auditor (auditor)",
        "work_mode": "program" if "program" in user_request else "default",
    }, eval_json_suffix=(
        "Evaluation: tools are unavailable. Return ONLY the JSON payload for one "
        "create_task call matching this production schema. Do not claim it was created.\n"
        + json.dumps(schema)
    ))
    response = await call_claude(system=system, user=user_request, max_tokens=2500)
    output = parse_object(response.text)
    assert len(output["title"]) <= 72
    assert not re.search(r"\[REQ-|/workspace/|[A-Z]{2}-\d+", output["title"])
    assert_scannable(output["description"], 120)
    assert user_request in output["inputs"]
    assert output["goal"].strip()
    assert output["acceptance_criteria"]
    assert output["verification_steps"].strip()
    assert output["assigned_agent"] != output["reviewer"]


async def test_office_instructions_are_specific_and_do_not_invent_setup():
    response = await call_claude(system=INSTRUCTIONS_PROMPT, user=(
        "Office Vision Brief: Bakery Orders. Help the owner plan weekly orders "
        "and draft customer replies. The owner approves prices and messages "
        "before sending. Keep contact details private. No source files, external "
        "accounts, skills, or service-level targets have been provided."
    ), max_tokens=2200)
    text = parse_object(response.text)["instructions"]
    assert_scannable(text, 700)
    assert len(text) <= 4500
    assert "source/" not in text
    assert "approve" in text.lower() or "approval" in text.lower()


async def test_workstream_context_is_a_short_context_note():
    response = await call_claude(system=WORKSTREAM_CONTEXT_PROMPT, user=(
        "Workstream: Weekly orders. Goal: help the bakery owner prepare next "
        "week's order list. Context: use the owner's uploaded spreadsheet, "
        "preserve quantities, and ask before substituting an unavailable item. "
        "There are no connected accounts or published delivery deadlines."
    ), max_tokens=1500)
    output = parse_object(response.text)
    # The production generator accepts the context_notes JSON key.
    text = output["context_notes"]
    assert isinstance(text, str) and text.strip()
    assert_scannable(text, 400)
