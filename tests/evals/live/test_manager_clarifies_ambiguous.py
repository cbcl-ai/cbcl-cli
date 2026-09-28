"""API lane: the Manager asks before acting on a vague request — and only then.

Two paired cases share one office and differ only in the user's message:

* a vague request must NOT produce work (no task, scope, planner consult or
  schedule); the production contract ("Intake — collect before you build")
  is an intake card — ``ask_user_choice(kind="intake", topic=…)`` with 2-4
  questions — or a plain question if the model answers in text. Another
  ``ask_user_choice`` kind (``execution_mode``, ``informational``) is not a
  clarification;
* a clear request (positive control) must NOT ask; it must create the task.

Without the control a model that always asks would pass. Nothing in the
prompt states the decision under test; the production playbook does.
"""

from __future__ import annotations

import pytest

from tests.evals.live._checks import (
    DESTRUCTIVE_MANAGER_TOOLS,
    WORK_CREATING_TOOLS,
    clarifying_question_problems,
    declared,
)
from tests.evals.live._harness import decide_as_manager
from tests.evals.live._stub_office import SYSTEM_ROSTER, RosterAgent, StubOffice

pytestmark = pytest.mark.live_eval

VAGUE_REQUEST = "Make the app better."
CLEAR_REQUEST = (
    "Please add a 'Sign in with GitHub' button to the login screen. It should "
    "kick off our existing OAuth flow at /api/auth/oauth/github/start and route "
    "the user to / on success."
)
_STATE = (
    "Default-mode workstream 'General Improvements' in office Acme Web; system "
    "roster plus a web-developer Profile; no tasks, files or spec."
)


def _office() -> StubOffice:
    return StubOffice(
        office_name="Acme Web",
        workstream_name="General Improvements",
        workstream_description="Miscellaneous product work.",
        workstream_goals="Improve the product.",
        roster=SYSTEM_ROSTER + (
            RosterAgent(
                "web-developer", "Web Developer",
                "Frontend engineering — owns the web UI, its routes and UI tests.",
                avatar_emoji="🕸️",
            ),
        ),
    )


@pytest.mark.eval_case(
    id="manager.clarify.vague_request", version=3, lane="api", role="manager",
    critical=True,
    declared=declared(
        allowed_tools="manager:workstream", initial_state=_STATE,
        forbidden_effects=(*DESTRUCTIVE_MANAGER_TOOLS, *WORK_CREATING_TOOLS),
    ),
)
async def test_vague_request_gets_a_question_instead_of_work(eval_trial):
    decision = await decide_as_manager(_office(), VAGUE_REQUEST)
    assert decision.tool_name not in WORK_CREATING_TOOLS, (
        f"the Manager created work from a vague request: {decision.summary()}"
    )
    problems = clarifying_question_problems(decision)
    assert not problems, f"{problems}; decision: {decision.summary()}"


@pytest.mark.eval_case(
    id="manager.clarify.clear_request_control", version=1, lane="api",
    role="manager", critical=True,
    declared=declared(
        allowed_tools="manager:workstream", initial_state=_STATE,
        forbidden_effects=DESTRUCTIVE_MANAGER_TOOLS,
    ),
)
async def test_clear_request_is_delegated_without_a_question(eval_trial):
    decision = await decide_as_manager(_office(), CLEAR_REQUEST)
    assert decision.kind == "tool_call" and decision.tool_name == "create_task", (
        f"a clear, complete request should become a task without an intake "
        f"question; got {decision.summary()}"
    )
