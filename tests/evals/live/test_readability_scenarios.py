"""API lane: readable task cards and concise generated instructions.

Manager cases observe the real ``create_task`` decision (production prompt +
production-selected catalog, nothing appended) and apply the card limits to
it. The approved-program milestone request is a separate ROUTING case: the
production Tier-3 path opens a scope or consults the Planner, so it is not
forced into one unscoped task.

Generator cases send the shipped generation prompt with its production effort
and a synthetic office brief; no tools are involved.
"""

from __future__ import annotations

import re

import pytest

from tests.evals.live._checks import DESTRUCTIVE_MANAGER_TOOLS, TOOL_FREE, declared
from tests.evals.live._harness import decide_as_manager, generate_as_production
from tests.evals.live._stub_office import SYSTEM_ROSTER, StubOffice, uploaded_file

pytestmark = pytest.mark.live_eval

# Each card request names the office files it relies on. The bakery request
# refers to an uploaded export, so the office lists that file (T12): a request
# about a file the office does not have would measure a different decision.
BAKERY_ORDERS = uploaded_file(
    "file-bakery-orders", "uploads/bakery-orders.csv", "bakery-orders.csv", 1536,
)
CARD_REQUESTS = [
    pytest.param(
        "Summarize this week's bakery orders from the uploaded bakery-orders.csv. "
        "Keep all quantities unchanged. Do not send customer messages.",
        (BAKERY_ORDERS,),
        id="bakery_summary",
    ),
    pytest.param(
        "Build a simple appointment booking prototype in one assignment. Keep "
        "customer data local. No payments or deployment.",
        (),
        id="booking_prototype",
    ),
]
MILESTONE_REQUEST = (
    "For our approved program, implement the first milestone: import existing "
    "customer records. Keep the original IDs, skip duplicates, and never delete "
    "source data."
)


def _office(**overrides) -> StubOffice:
    return StubOffice(
        office_name="Readability Test",
        workstream_name="Customer service",
        roster=SYSTEM_ROSTER,
        **overrides,
    )


def assert_scannable(text: str, max_words: int) -> None:
    assert len(text.split()) <= max_words, f"{len(text.split())} words > {max_words}"
    # A long unbroken paragraph is a different defect from total length.
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |#{1,6} ))", text)
    assert all(len(block.split()) <= 100 for block in blocks), "paragraph > 100 words"


@pytest.mark.eval_case(
    id="manager.readability.task_card", version=3, lane="api", role="manager",
    declared=declared(
        allowed_tools="manager:workstream",
        initial_state=(
            "Default-mode 'Customer service' workstream with the system roster; the "
            "request's uploaded file (if any) is registered; no tasks or spec."
        ),
        forbidden_effects=DESTRUCTIVE_MANAGER_TOOLS,
    ),
)
@pytest.mark.parametrize("user_request,files", CARD_REQUESTS)
async def test_task_card_stays_short_and_inputs_remain_exact(user_request, files, eval_trial):
    office = _office(files=files)
    decision = await decide_as_manager(office, user_request)
    assert decision.kind == "tool_call" and decision.tool_name == "create_task", (
        f"expected create_task; got {decision.summary()}"
    )
    output = decision.tool_input or {}
    assert len(output["title"]) <= 72
    assert not re.search(r"\[REQ-|/workspace/|[A-Z]{2}-\d+", output["title"])
    assert_scannable(output.get("description") or "", 120)
    assert user_request in output["inputs"]
    assert output["goal"].strip()
    assert output["acceptance_criteria"]
    assert output["verification_steps"].strip()
    assert output["assigned_agent"] != output["reviewer"]


@pytest.mark.eval_case(
    id="manager.routing.program_milestone", version=1, lane="api", role="manager",
    declared=declared(
        allowed_tools="manager:workstream",
        initial_state=(
            "Program-mode 'Customer service' workstream with an approved spec (rev 1) "
            "whose milestone M1 is planned and has no scope; system roster; no tasks."
        ),
        forbidden_effects=DESTRUCTIVE_MANAGER_TOOLS,
    ),
)
async def test_approved_program_milestone_routes_through_tier3(eval_trial):
    office = _office(
        work_mode="program",
        spec={
            "status": "approved",
            "revision": 1,
            "content": "## Goal & Why\nMove customer service onto the new CRM.",
            "milestones": [{
                "key": "M1", "title": "Import existing customer records",
                "goal": "All existing customers exist in the CRM with original IDs.",
                "order": 1, "status": "planned", "scope_id": None,
            }],
        },
    )
    decision = await decide_as_manager(office, MILESTONE_REQUEST)
    assert decision.kind == "tool_call" and decision.tool_name in {
        "create_scope", "consult_planner",
    }, (
        "an approved-program milestone should open a scope or consult the "
        f"Planner, not become an unscoped task; got {decision.summary()}"
    )


@pytest.mark.eval_case(
    id="generator.office_instructions", version=2, lane="api", role="generator",
    declared=declared(
        allowed_tools=TOOL_FREE,
        initial_state="Tool-free office-instructions generation call; no office state.",
    ),
)
async def test_office_instructions_are_specific_and_do_not_invent_setup(eval_trial):
    output = await generate_as_production("INSTRUCTIONS_PROMPT", (
        "Office Vision Brief: Bakery Orders. Help the owner plan weekly orders "
        "and draft customer replies. The owner approves prices and messages "
        "before sending. Keep contact details private. No source files, external "
        "accounts, skills, or service-level targets have been provided."
    ))
    text = output["instructions"]
    assert_scannable(text, 700)
    assert len(text) <= 4500
    assert "source/" not in text
    assert "approve" in text.lower() or "approval" in text.lower()
    # T28: the brief provides no skills, accounts or service levels, so the
    # instructions must not invent any.
    lowered = text.lower()
    assert "skill.md" not in lowered and ".claude/skills" not in lowered
    for service in (
        "slack", "gmail", "notion", "hubspot", "salesforce", "zapier",
        "google drive", "shopify", "stripe",
    ):
        assert not re.search(rf"\b{service}\b", lowered), service
    assert not re.search(
        r"\bslas?\b|service[- ]level|response time"
        r"|within \d+\s*(?:minutes?|hours?|business days?|days?)",
        lowered,
    ), "invented a service-level target"


@pytest.mark.eval_case(
    id="generator.workstream_context", version=2, lane="api", role="generator",
    declared=declared(
        allowed_tools=TOOL_FREE,
        initial_state="Tool-free workstream-context generation call; no office state.",
    ),
)
async def test_workstream_context_is_a_short_context_note(eval_trial):
    output = await generate_as_production("WORKSTREAM_CONTEXT_PROMPT", (
        "Workstream: Weekly orders. Goal: help the bakery owner prepare next "
        "week's order list. Context: use the owner's uploaded spreadsheet, "
        "preserve quantities, and ask before substituting an unavailable item. "
        "There are no connected accounts or published delivery deadlines."
    ))
    # The production generator accepts the context_notes JSON key.
    text = output["context_notes"]
    assert isinstance(text, str) and text.strip()
    assert_scannable(text, 400)
