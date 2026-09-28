"""API lane: the Manager routes review to an agent other than the executor.

The production rule lives in the shipped playbook and the ``create_task``
schema description ("MUST differ from assigned_agent"). Nothing in this case
restates it: the model sees the production-rendered Manager prompt, the
production-selected tool catalog, and the user's message. The first non-read
tool call is the decision under test and is never executed.

Skipped (reported NOT EVALUATED) without ANTHROPIC_API_KEY — see conftest.py.
"""

from __future__ import annotations

import pytest

from tests.evals.live._checks import DESTRUCTIVE_MANAGER_TOOLS, declared
from tests.evals.live._harness import decide_as_manager
from tests.evals.live._stub_office import SYSTEM_ROSTER, RosterAgent, StubOffice

pytestmark = pytest.mark.live_eval

REQUEST = "Add a /healthz endpoint to our FastAPI app."


def _office() -> StubOffice:
    return StubOffice(
        office_name="Acme Web",
        workstream_name="Backend",
        workstream_description="FastAPI backend work.",
        workstream_goals="Ship the API.",
        roster=SYSTEM_ROSTER + (
            RosterAgent(
                "python-developer", "Python Developer",
                "Backend engineering — owns the FastAPI service code and its tests.",
                avatar_emoji="🐍",
            ),
        ),
    )


@pytest.mark.eval_case(
    id="manager.review_routing", version=2, lane="api", role="manager",
    critical=True,
    declared=declared(
        allowed_tools="manager:workstream",
        initial_state=(
            "Default-mode 'Backend' workstream in office Acme Web; system roster plus "
            "a python-developer Profile; no tasks, files or spec."
        ),
        forbidden_effects=DESTRUCTIVE_MANAGER_TOOLS,
    ),
)
async def test_executor_and_reviewer_are_different_roster_profiles(eval_trial):
    office = _office()
    decision = await decide_as_manager(office, REQUEST)
    assert decision.kind == "tool_call" and decision.tool_name == "create_task", (
        f"expected a create_task decision for a clear build request; got "
        f"{decision.summary()}"
    )
    payload = decision.tool_input or {}
    assigned = payload.get("assigned_agent")
    reviewer = payload.get("reviewer")
    assignable = office.assignable_names()
    assert assigned in assignable, f"assignee {assigned!r} is not an assignable profile"
    assert reviewer in assignable, f"reviewer {reviewer!r} is not an assignable profile"
    assert assigned != reviewer, (
        f"the same profile executes and reviews ({assigned!r}): {decision.summary()}"
    )
